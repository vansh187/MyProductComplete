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

from marketengine.WsSubscriptionSender import WsSubscriptionSender

logger = logging.getLogger(__name__)

RECONNECT_DELAY_SECS = 5
WS_JOIN_TIMEOUT_SECS = 2

TickHandler = Callable[[str, dict], Awaitable[None] | None]
OrderUpdateHandler = Callable[[dict], Awaitable[None] | None]
RawTickHandler = Callable[[dict], None]


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

    def __init__(self, shoonya_connection, subscription_sender=None):
        """subscription_sender: anything with submit(label, send) -> bool and
        close() (default: a WsSubscriptionSender thread). ensure_subscribed()/
        release() hand frames to it so they never block the caller."""
        self._shoonya = shoonya_connection
        self._sender = subscription_sender or WsSubscriptionSender(thread_name="optionfeed-ws-subscribe")
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._tick_handlers: list[TickHandler] = []
        self._raw_tick_handlers: list[RawTickHandler] = []
        self._order_update_handlers: list[OrderUpdateHandler] = []
        self._subscribed_tokens: dict[str, int] = {}  # "EXCH|TOKEN" -> ref count
        self._lock = threading.Lock()
        self._reconnecting = False
        # True only between _on_open and the next close/error/restart, so
        # tick caches can refuse to serve prices from a dead socket.
        self._socket_open = False
        # The specific NorenApi instance the websocket was last opened on.
        # ShoonyaConnection.connect() builds a BRAND NEW NorenApi instance on
        # every reconnect/token-refresh (self._api = _ShoonyaApi(...)), so
        # `self._shoonya._api` can silently become a different object between
        # one start() call and the next - we must close the OLD instance's
        # socket specifically, since calling close_websocket() on the NEW
        # instance (which never opened a socket) does nothing to the orphaned
        # old one.
        self._api_instance = None

    @property
    def is_connected(self) -> bool:
        return self._socket_open

    def on_tick(self, handler: TickHandler) -> None:
        """Registers a callback invoked as handler(instrument_key, tick_fields)
        on every tick. Multiple independent consumers (e.g. the option chain
        cache and the position cache) can each register their own handler -
        every registered handler receives every tick."""
        self._tick_handlers.append(handler)

    def on_raw_tick(self, handler: RawTickHandler) -> None:
        """Registers a callback invoked as handler(raw_frame) with the
        unmodified Shoonya WS frame, before _on_tick derives the
        instrument_key/tick_fields pair the normal on_tick() consumers see.
        For consumers that need fields _on_tick's own normalize_touchline_tick
        doesn't forward (e.g. equity OHLC/full depth for the stocks feature -
        see marketengine/ShoonyaStockFeed.py) rather than duplicating a second
        WebSocket connection. Called synchronously on NorenApi's WS thread,
        so handlers must be fast and must never raise (this call site catches
        and logs, but a slow handler would still stall every other consumer's
        ticks)."""
        self._raw_tick_handlers.append(handler)

    def on_order_update(self, handler: OrderUpdateHandler) -> None:
        """Registers a callback invoked as handler(order_update) whenever the
        broker pushes an order status change (fill, reject, cancel, etc.) for
        THIS master account. Per Shoonya's own docs, order updates and price
        ticks share the same WebSocket connection - no separate connection is
        opened for this, it's the same socket start() already manages."""
        self._order_update_handlers.append(handler)

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
        self._socket_open = False

        # Best-effort close of the specific previous instance's socket/thread
        # before opening a new one - NorenApi.start_websocket() unconditionally
        # creates a new WebSocketApp + daemon thread without closing a prior
        # one, so calling it again (on the same or a new instance) would
        # otherwise leak the old socket/thread. start() runs on the event
        # loop, so the join happens on a helper thread, never here.
        # _api_instance is switched BEFORE the previous socket is stopped, so
        # the close callback that stopping fires is recognised as stale by
        # _callback_for and cannot mark the new socket closed or schedule a
        # reconnect of it.
        previous = self._api_instance
        self._api_instance = api
        if previous is not None:
            self._stop_websocket_in_background(previous)

        api.start_websocket(
            subscribe_callback=self._on_tick,
            order_update_callback=self._on_order_update,
            socket_open_callback=self._callback_for(api, self._on_open),
            socket_close_callback=self._callback_for(api, self._on_close),
            socket_error_callback=self._callback_for(api, self._on_error),
        )

    def _callback_for(self, api, handler):
        """Wraps a socket lifecycle callback so events from a superseded
        NorenApi instance (an old socket shutting down after a token refresh)
        are ignored instead of flipping the current socket's state."""
        def _guarded(*args):
            if api is not self._api_instance:
                logger.debug("[OptionFeed] Ignoring %s from a superseded socket", getattr(handler, "__name__", "callback"))
                return
            handler(*args)
        return _guarded

    def _signal_stop_websocket(self, api) -> None:
        """Best-effort, thread-safe request to stop an instance's WS loop.

        NorenApi.close_websocket() is a no-op once __websocket_connected is
        already False - which is exactly the case here, since our own
        reconnect is only ever triggered *after* NorenApi's on_close/on_error
        callback has already flipped that flag. In that case close_websocket()
        never sets its internal stop_event, so the old __ws_run_forever
        thread spins forever (run_forever() fails instantly against the dead
        socket, sleeps 100ms, repeats) until something else sets that event -
        reach into the library's internals directly (best-effort,
        version-tolerant) to force the loop to actually exit regardless of
        that connected-flag check.

        Deliberately does NOT join the thread - this is called from _on_close/
        _on_error, which run ON that same WS thread (invoked synchronously
        from inside NorenApi's run_forever()), and a thread can't join itself.
        See _force_stop_websocket() for the join, done later from a different
        thread/coroutine.
        """
        try:
            api.close_websocket()
        except Exception:
            pass

        stop_event = getattr(api, "_NorenApi__stop_event", None)
        if stop_event is not None:
            stop_event.set()

        ws = getattr(api, "_NorenApi__websocket", None)
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _force_stop_websocket(self, api) -> None:
        """Full stop of a previous NorenApi instance: signal + join (blocks up
        to WS_JOIN_TIMEOUT_SECS). Only for shutdown (close()) - never call on
        the event loop during normal operation; start() uses
        _stop_websocket_in_background instead."""
        self._signal_stop_websocket(api)
        self._join_ws_thread(api)

    def _stop_websocket_in_background(self, api) -> None:
        self._signal_stop_websocket(api)
        thread = getattr(api, "_NorenApi__ws_thread", None)
        if thread is not None and thread.is_alive():
            threading.Thread(
                target=self._join_ws_thread, args=(api,), name="ws-old-join", daemon=True
            ).start()

    def _join_ws_thread(self, api) -> None:
        thread = getattr(api, "_NorenApi__ws_thread", None)
        if thread is None or thread is threading.current_thread() or not thread.is_alive():
            return
        thread.join(timeout=WS_JOIN_TIMEOUT_SECS)
        if thread.is_alive():
            logger.warning("[OptionFeed] Old WS thread did not exit within timeout")

    def ensure_subscribed(self, tokens: set[str]) -> None:
        """tokens: set of 'EXCH|TOKEN' strings. Subscribes only genuinely-new
        tokens. Never blocks and never raises: the ref-count is updated here
        and the frame is queued for the sender thread. If the socket is not
        open, nothing is sent - _on_open subscribes every ref-counted token."""
        try:
            new_tokens = []
            with self._lock:
                for token in tokens:
                    count = self._subscribed_tokens.get(token, 0)
                    if count == 0:
                        new_tokens.append(token)
                    self._subscribed_tokens[token] = count + 1
            if new_tokens:
                self._queue_frame("subscribe", new_tokens)
        except Exception as exc:
            logger.warning(f"[OptionFeed] ensure_subscribed failed: {exc}")

    def release(self, tokens: set[str]) -> None:
        """Decrements ref-counts; unsubscribes tokens that drop to zero.
        Never blocks and never raises (see ensure_subscribed)."""
        try:
            to_unsubscribe = []
            with self._lock:
                for token in tokens:
                    count = self._subscribed_tokens.get(token, 0)
                    if count <= 1:
                        self._subscribed_tokens.pop(token, None)
                        to_unsubscribe.append(token)
                    else:
                        self._subscribed_tokens[token] = count - 1
            if to_unsubscribe:
                self._queue_frame("unsubscribe", to_unsubscribe)
        except Exception as exc:
            logger.warning(f"[OptionFeed] release failed: {exc}")

    def _queue_frame(self, action: str, tokens: list[str]) -> None:
        if not self._socket_open:
            logger.debug("[OptionFeed] Socket not open - %s of %d tokens deferred to next connect", action, len(tokens))
            return
        label = f"{action} {len(tokens)} tokens"
        if self._sender.submit(label, lambda: self._send_frame(action, tokens)):
            logger.info(f"[OptionFeed] Queued {label}")
        else:
            logger.warning(f"[OptionFeed] Could not queue {label} - applied on next reconnect")

    def _send_frame(self, action: str, tokens: list[str]) -> None:
        """Runs on the sender thread. Targets the instance whose socket is
        actually open (never self._shoonya._api, which after a token refresh is
        a newer instance with no socket yet), and re-reads the ref-counts at
        send time so a frame queued before a disconnect cannot resurrect a
        token released while the socket was down."""
        api = self._api_instance
        if api is None or not self._socket_open:
            return
        with self._lock:
            if action == "subscribe":
                live = [t for t in tokens if self._subscribed_tokens.get(t, 0) > 0]
            else:
                live = [t for t in tokens if self._subscribed_tokens.get(t, 0) == 0]
        if not live:
            return
        if action == "subscribe":
            api.subscribe(live)
        else:
            api.unsubscribe(live)

    def close(self) -> None:
        """Closes the instance the socket was actually opened on, not whatever
        self._shoonya._api happens to be right now - those can differ after a
        token refresh (see the _api_instance comment in __init__)."""
        self._socket_open = False
        try:
            self._sender.close()
        except Exception as exc:
            logger.warning(f"[OptionFeed] Subscription sender close failed: {exc}")
        if self._api_instance is not None:
            self._force_stop_websocket(self._api_instance)

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

            # Raw handlers run first, synchronously, and independently of the
            # normal tick_handlers path below - a raw handler raising or
            # running slowly must never prevent option-chain/position-cache
            # ticks from being dispatched, so each is isolated in its own
            # try/except and none of them can block on the asyncio loop (they
            # run inline on this WS thread, not scheduled onto it).
            for raw_handler in self._raw_tick_handlers:
                try:
                    raw_handler(raw)
                except Exception as exc:
                    logger.warning(f"[OptionFeed] raw_tick handler failed: {exc}")

            tick = normalize_touchline_tick(raw)
            if not tick or not self._tick_handlers or self._async_loop is None:
                return

            for handler in self._tick_handlers:
                result = handler(instrument_key, tick)
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

    def _log_tick_task_exception(self, future: "asyncio.Future") -> None:
        try:
            exc = future.exception()
        except Exception:
            return  # cancelled, or exception() itself unavailable - nothing to log
        if exc is not None:
            logger.error(f"[OptionFeed] Tick handler task failed: {exc!r}", exc_info=exc)

    def _on_order_update(self, raw: dict) -> None:
        """Fired on NorenApi's own WS thread for every order status change on
        the master account (fill, reject, cancel, modify ack, etc.) - this is
        the low-latency path for live F&O position tracking (see
        service/orderUpdateService.py), used instead of polling the broker's
        order book. Mirrors _on_tick's dispatch pattern exactly (same
        run_coroutine_threadsafe + detached-task-exception-logging need,
        since handlers may be async and this callback itself must never
        raise back into NorenApi's WS loop)."""
        try:
            if not self._order_update_handlers or self._async_loop is None:
                return

            for handler in self._order_update_handlers:
                result = handler(raw)
                if inspect.isawaitable(result):
                    future = asyncio.run_coroutine_threadsafe(result, self._async_loop)
                    future.add_done_callback(self._log_order_update_task_exception)
        except Exception as e:
            logger.warning(f"[OptionFeed] Error processing order update {raw}: {e}")

    def _log_order_update_task_exception(self, future: "asyncio.Future") -> None:
        try:
            exc = future.exception()
        except Exception:
            return
        if exc is not None:
            logger.error(f"[OptionFeed] Order-update handler task failed: {exc!r}", exc_info=exc)

    def _on_open(self) -> None:
        """Runs on the WS thread right after the broker acks the login frame.
        _socket_open is set BEFORE the snapshot so a concurrent
        ensure_subscribed either lands in the snapshot or queues its own frame
        (a duplicate subscribe is harmless; a missed one is not)."""
        try:
            logger.info("[OptionFeed] WebSocket connected")
            self._reconnecting = False
            self._socket_open = True
            with self._lock:
                tokens = list(self._subscribed_tokens.keys())
            api = self._api_instance
            if tokens and api is not None:
                logger.info(f"[OptionFeed] Resubscribing {len(tokens)} tokens after (re)connect")
                api.subscribe(tokens)
        except Exception as exc:
            logger.warning(f"[OptionFeed] Resubscribe after connect failed: {exc}")

    def _on_close(self, *args) -> None:
        logger.warning("[OptionFeed] WebSocket closed - scheduling reconnect")
        self._socket_open = False
        self._signal_stop_current_instance()
        try:
            self._schedule_reconnect()
        except Exception as e:
            logger.warning(f"[OptionFeed] Error scheduling reconnect after close: {e}")

    def _on_error(self, *args) -> None:
        logger.warning(f"[OptionFeed] WebSocket error: {args}")
        self._socket_open = False
        self._signal_stop_current_instance()
        try:
            self._schedule_reconnect()
        except Exception as e:
            logger.warning(f"[OptionFeed] Error scheduling reconnect after error: {e}")

    def _signal_stop_current_instance(self) -> None:
        """Tells the dead connection's internal retry loop to stop right now,
        instead of leaving it to independently hammer the broker every 100ms
        (see NorenApi.__ws_run_forever) for the full RECONNECT_DELAY_SECS
        until our own start() eventually gets around to it. Two unsynchronized
        retry loops racing against a broker that's rejecting every attempt is
        exactly what turns one bad connection into a connection leak."""
        if self._api_instance is not None:
            try:
                self._signal_stop_websocket(self._api_instance)
            except Exception as e:
                logger.warning(f"[OptionFeed] Error signaling old WS to stop: {e}")

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
