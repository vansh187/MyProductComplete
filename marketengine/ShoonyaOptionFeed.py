"""
Persistent WebSocket touchline listener for option-chain tokens.

Reuses an already-connected ShoonyaConnection's underlying NorenApi session
(the WS login frame authenticates with the same OAuth accesstoken already
used for REST - see NorenApi.__on_open_callback / start_websocket, which
formats the websocket URL with self.__access_token) rather than opening a
second broker session.

Dynamic subscription: multiple option-chain caches can reference the same
underlying token (e.g. two clients viewing the same strike), so tokens are
ref-counted - a token is only actually unsubscribed from Shoonya once no
active cache references it anymore.
"""

import asyncio
import inspect
import logging
import threading
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

RECONNECT_DELAY_SECS = 5

TickHandler = Callable[[str, dict], Awaitable[None] | None]


def _safe_float(val, default=None):
    try:
        return float(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _safe_int(val, default=None):
    try:
        return int(float(val)) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


def normalize_touchline_tick(raw: dict) -> dict:
    """
    Normalizes a Shoonya touchline tick ('tk' ack or 'tf' update) into our
    internal field names. Per Shoonya's docs, only t/e/tk are guaranteed on
    'tf' updates - every other field is included only when it changed, so
    callers must merge this into previously-known state rather than
    replacing it outright.
    """
    result = {}
    if "lp" in raw:
        result["ltp"] = _safe_float(raw.get("lp"))
    if "bp1" in raw:
        result["bid"] = _safe_float(raw.get("bp1"))
    if "sp1" in raw:
        result["ask"] = _safe_float(raw.get("sp1"))
    if "v" in raw:
        result["volume"] = _safe_int(raw.get("v"))
    if "oi" in raw:
        result["oi"] = _safe_int(raw.get("oi"))
    if "poi" in raw:
        result["poi"] = _safe_int(raw.get("poi"))
    return result


class ShoonyaOptionFeed:

    def __init__(self, shoonya_connection):
        self._shoonya = shoonya_connection
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._tick_handler: TickHandler | None = None
        self._subscribed_tokens: dict[str, int] = {}  # "EXCH|TOKEN" -> ref count
        self._lock = threading.Lock()
        self._reconnecting = False
        # The specific NorenApi instance the websocket was last opened on.
        # ShoonyaConnection.connect() builds a BRAND NEW NorenApi instance on
        # every reconnect/token-refresh (self._api = _ShoonyaApi(...)), so
        # `self._shoonya._api` can silently become a different object between
        # one start() call and the next - we must close the OLD instance's
        # socket specifically, since calling close_websocket() on the NEW
        # instance (which never opened a socket) does nothing to the orphaned
        # old one.
        self._api_instance = None

    def on_tick(self, handler: TickHandler) -> None:
        """Registers the single callback invoked as handler(instrument_key, tick_fields)."""
        self._tick_handler = handler

    def start(self) -> None:
        """Opens the WebSocket connection. Safe to call again after a reconnect
        (internal WS-level reconnect on the same session, or a fresh broker
        token-refresh that replaced the underlying NorenApi instance) - any
        previously-opened socket is closed first."""
        self._async_loop = asyncio.get_running_loop()
        api = self._shoonya._api
        if api is None:
            logger.warning("[OptionFeed] Cannot start: Shoonya session not connected")
            return

        # A fresh external trigger (broker reconnect) supersedes whatever our
        # own internal reconnect loop was doing for the old session.
        self._reconnecting = False

        # Best-effort close of the specific previous instance's socket/thread
        # before opening a new one - NorenApi.start_websocket() unconditionally
        # creates a new WebSocketApp + daemon thread without closing a prior
        # one, so calling it again (on the same or a new instance) would
        # otherwise leak the old socket/thread.
        if self._api_instance is not None:
            try:
                self._api_instance.close_websocket()
            except Exception:
                pass  # already closed, or never fully opened

        self._api_instance = api

        api.start_websocket(
            subscribe_callback=self._on_tick,
            socket_open_callback=self._on_open,
            socket_close_callback=self._on_close,
            socket_error_callback=self._on_error,
        )

    def ensure_subscribed(self, tokens: set[str]) -> None:
        """tokens: set of 'EXCH|TOKEN' strings. Subscribes only genuinely-new tokens."""
        new_tokens = []
        with self._lock:
            for token in tokens:
                count = self._subscribed_tokens.get(token, 0)
                if count == 0:
                    new_tokens.append(token)
                self._subscribed_tokens[token] = count + 1

        if new_tokens and self._shoonya._api is not None:
            logger.info(f"[OptionFeed] Subscribing to {len(new_tokens)} new tokens")
            self._shoonya._api.subscribe(new_tokens)

    def release(self, tokens: set[str]) -> None:
        """Decrements ref-counts; unsubscribes tokens that drop to zero."""
        to_unsubscribe = []
        with self._lock:
            for token in tokens:
                count = self._subscribed_tokens.get(token, 0)
                if count <= 1:
                    self._subscribed_tokens.pop(token, None)
                    to_unsubscribe.append(token)
                else:
                    self._subscribed_tokens[token] = count - 1

        if to_unsubscribe and self._shoonya._api is not None:
            logger.info(f"[OptionFeed] Unsubscribing {len(to_unsubscribe)} idle tokens")
            self._shoonya._api.unsubscribe(to_unsubscribe)

    def close(self) -> None:
        """Closes the instance the socket was actually opened on, not whatever
        self._shoonya._api happens to be right now - those can differ after a
        token refresh (see the _api_instance comment in __init__)."""
        try:
            if self._api_instance is not None:
                self._api_instance.close_websocket()
        except Exception as e:
            logger.warning(f"[OptionFeed] Error closing websocket: {e}")

    # -- callbacks fired on NorenApi's own daemon WS thread --------------

    def _on_tick(self, raw: dict) -> None:
        try:
            msg_type = raw.get("t")
            if msg_type not in ("tk", "tf"):
                return

            exch = raw.get("e")
            token = raw.get("tk")
            if not exch or not token:
                return
            instrument_key = f"{exch}|{token}"

            tick = normalize_touchline_tick(raw)
            if not tick or self._tick_handler is None or self._async_loop is None:
                return

            result = self._tick_handler(instrument_key, tick)
            if inspect.isawaitable(result):
                future = asyncio.run_coroutine_threadsafe(result, self._async_loop)
                # run_coroutine_threadsafe schedules `result` as a detached
                # Task - nothing ever calls .result() on the returned Future,
                # so an exception raised inside tick processing (e.g. a bad
                # expiry date, a KeyError) would otherwise vanish silently:
                # that option leg would simply stop updating with no log,
                # metric, or alert anywhere. This surfaces it.
                future.add_done_callback(self._log_tick_task_exception)
        except Exception as e:
            logger.warning(f"[OptionFeed] Error processing tick {raw}: {e}")

    @staticmethod
    def _log_tick_task_exception(future: "asyncio.Future") -> None:
        try:
            exc = future.exception()
        except Exception:
            return  # cancelled, or exception() itself unavailable - nothing to log
        if exc is not None:
            logger.error(f"[OptionFeed] Tick handler task failed: {exc!r}", exc_info=exc)

    def _on_open(self) -> None:
        logger.info("[OptionFeed] WebSocket connected")
        self._reconnecting = False
        with self._lock:
            tokens = list(self._subscribed_tokens.keys())
        if tokens and self._shoonya._api is not None:
            logger.info(f"[OptionFeed] Resubscribing {len(tokens)} tokens after (re)connect")
            self._shoonya._api.subscribe(tokens)

    def _on_close(self, *args) -> None:
        logger.warning("[OptionFeed] WebSocket closed - scheduling reconnect")
        try:
            self._schedule_reconnect()
        except Exception as e:
            logger.warning(f"[OptionFeed] Error scheduling reconnect after close: {e}")

    def _on_error(self, *args) -> None:
        logger.warning(f"[OptionFeed] WebSocket error: {args}")
        try:
            self._schedule_reconnect()
        except Exception as e:
            logger.warning(f"[OptionFeed] Error scheduling reconnect after error: {e}")

    def _schedule_reconnect(self) -> None:
        if self._reconnecting or self._async_loop is None:
            return
        self._reconnecting = True
        asyncio.run_coroutine_threadsafe(self._reconnect_loop(), self._async_loop)

    async def _reconnect_loop(self) -> None:
        await asyncio.sleep(RECONNECT_DELAY_SECS)
        try:
            self.start()
        except Exception as e:
            logger.warning(f"[OptionFeed] Reconnect attempt failed: {e}")
            self._reconnecting = False
            self._schedule_reconnect()
