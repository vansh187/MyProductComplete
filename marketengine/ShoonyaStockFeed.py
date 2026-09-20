"""
Equity (cash-market) tick cache, fed by the SAME WebSocket connection
marketengine/ShoonyaOptionFeed.py already owns.

NorenApi only supports one open WebSocket per session (calling
start_websocket() a second time on the same instance tears down the first
socket's callbacks), so this class does NOT open its own connection - it
registers as a raw-frame consumer on the existing ShoonyaOptionFeed via
on_raw_tick() (see marketengine/ShoonyaOptionFeed.py), the same multiplexing
mechanism the option chain and the position tick service already use for
their own (differently-shaped) consumption of ticks. Subscription
ref-counting is likewise delegated to that shared feed's ensure_subscribed/
release, since it already tracks ref-counts per "EXCH|TOKEN" string
generically - nothing about it is NFO/BFO-specific.

Subscription lifecycle: touch() is the only way callers request a token -
it is idempotent (a token already active is never re-subscribed, so polling
the same quote every 3s does not grow the shared feed's ref-count on every
poll) and records a last-access timestamp. evict_idle_loop() periodically
releases tokens nobody has touched()'d within IDLE_TTL_SECS, so a public,
no-auth endpoint that many different symbols get viewed through over a day
can't monotonically grow the shared WebSocket's subscription count forever.

Thread-safety: the only mutable state here (self._ticks, self._active_tokens,
self._last_access) is guarded by self._lock, held only for plain dict
reads/writes and never while calling into the shared feed - so this can
never be part of a lock-ordering cycle with ShoonyaOptionFeed's own lock
(that lock guards a disjoint resource, the subscription ref-count map, and
this class never holds its own lock while calling into the shared feed).
ingest_raw_tick() runs synchronously on NorenApi's single WS thread, same as
every other consumer of ShoonyaOptionFeed - there is exactly one thread that
ever produces ticks, so there is no producer-side race to guard against.

Deliberately instance-based throughout (no static/class methods).
"""

import asyncio
import logging
import threading
import time

from marketengine.touchlineFields import TouchlineFieldParser

logger = logging.getLogger(__name__)

IDLE_TTL_SECS = 90.0
EVICTION_SWEEP_INTERVAL_SECS = 30.0


class ShoonyaStockFeed:

    def __init__(self, shared_feed, field_parser: TouchlineFieldParser | None = None):
        """shared_feed: an object exposing on_raw_tick(handler), ensure_subscribed(tokens),
        release(tokens) - i.e. a marketengine.ShoonyaOptionFeed.ShoonyaOptionFeed instance."""
        self._shared_feed = shared_feed
        self._field_parser = field_parser or TouchlineFieldParser()
        self._ticks: dict[str, dict] = {}
        self._active_tokens: set[str] = set()
        self._last_access: dict[str, float] = {}
        self._lock = threading.Lock()
        self._shared_feed.on_raw_tick(self.ingest_raw_tick)

    def touch(self, instrument_key: str) -> None:
        """Marks 'EXCH|TOKEN' as actively viewed right now. Subscribes it on
        the shared feed only the first time it becomes active (idempotent -
        safe to call on every poll of a quote endpoint) and refreshes its
        idle-eviction deadline."""
        if not instrument_key:
            return
        newly_active = False
        with self._lock:
            self._last_access[instrument_key] = time.monotonic()
            if instrument_key not in self._active_tokens:
                self._active_tokens.add(instrument_key)
                newly_active = True

        if newly_active:
            try:
                self._shared_feed.ensure_subscribed({instrument_key})
            except Exception as exc:
                logger.warning(f"[StockFeed] ensure_subscribed failed for {instrument_key}: {exc}")

    def get_tick(self, instrument_key: str) -> dict | None:
        """Returns the last-known merged tick fields for 'EXCH|TOKEN', or None
        if nothing has arrived yet for that token. Never raises."""
        with self._lock:
            tick = self._ticks.get(instrument_key)
            return dict(tick) if tick else None

    async def evict_idle_loop(self) -> None:
        """Background task (started from app.py lifespan): periodically
        releases tokens nobody has touch()'d in IDLE_TTL_SECS, so the shared
        feed's subscription count for this cache tracks *currently viewed*
        symbols instead of growing forever with every distinct symbol ever
        viewed by any visitor to the public quote endpoint."""
        while True:
            await asyncio.sleep(EVICTION_SWEEP_INTERVAL_SECS)
            try:
                self._evict_idle_tokens()
            except Exception as exc:
                logger.warning(f"[StockFeed] Idle eviction sweep failed: {exc}")

    def _evict_idle_tokens(self) -> None:
        now = time.monotonic()
        stale = []
        with self._lock:
            for token, last_seen in list(self._last_access.items()):
                if now - last_seen > IDLE_TTL_SECS:
                    stale.append(token)
            for token in stale:
                self._active_tokens.discard(token)
                self._last_access.pop(token, None)
                self._ticks.pop(token, None)

        if stale:
            logger.info(f"[StockFeed] Releasing {len(stale)} idle tokens")
            try:
                self._shared_feed.release(set(stale))
            except Exception as exc:
                logger.warning(f"[StockFeed] release failed for {stale}: {exc}")

    # -- normalization -----------------------------------------------------

    def _normalize_tick(self, raw: dict) -> dict:
        """Normalizes a Shoonya equity touchline tick ('tk' ack or 'tf' update).
        Only 't'/'e'/'tk' are guaranteed present on 'tf' updates - every other
        field is included only when it changed - so this returns a partial
        dict and callers must merge it into previously-known state rather than
        replacing it outright (see ingest_raw_tick)."""
        parser = self._field_parser
        result = {}
        simple_fields = {
            "lp": "ltp", "o": "open", "h": "high", "l": "low", "c": "close",
            "v": "volume", "ap": "avg_price", "ltt": "last_trade_time",
        }
        for raw_key, out_key in simple_fields.items():
            if raw_key in raw:
                if out_key == "volume":
                    result[out_key] = parser.safe_int(raw.get(raw_key))
                else:
                    result[out_key] = parser.safe_float(raw.get(raw_key))

        result.update(parser.circuit_limits(raw))

        depth_delta = parser.depth_delta(raw)
        if depth_delta is not None:
            result["_depth_delta"] = depth_delta

        return result

    # -- callbacks fired on NorenApi's own daemon WS thread ----------------

    def ingest_raw_tick(self, raw: dict) -> None:
        """Feed a raw Shoonya WS frame straight into the cache. Registered as
        the shared feed's on_raw_tick handler (see __init__ and
        marketengine/ShoonyaOptionFeed.py's on_raw_tick), which fires with
        every field a touchline frame carries (depth, OHLC, volume) - not
        just the ltp/bid/ask/oi/volume subset ShoonyaOptionFeed's normal
        on_tick() forwards to option-chain consumers. Runs synchronously on
        NorenApi's WS thread; the try/except here matches ShoonyaOptionFeed's
        own _on_tick so a malformed frame can never propagate back into the
        broker's WS loop.

        Depth is merged field-by-field via TouchlineFieldParser.apply_depth_delta
        rather than replaced wholesale - a partial 'tf' frame that only
        carries e.g. bp1 must not wipe out the other 4 levels/the ask side
        that a prior full snapshot already populated."""
        try:
            msg_type = raw.get("t")
            if msg_type not in ("tk", "tf"):
                return

            exch = raw.get("e")
            token = raw.get("tk")
            if not exch or not token:
                return
            instrument_key = f"{exch}|{token}"

            fields = self._normalize_tick(raw)
            if not fields:
                return

            depth_delta = fields.pop("_depth_delta", None)

            with self._lock:
                merged = self._ticks.get(instrument_key, {})
                merged.update(fields)
                if depth_delta is not None:
                    depth = merged.get("depth") or self._field_parser.empty_depth()
                    merged["depth"] = self._field_parser.apply_depth_delta(depth, depth_delta)
                self._ticks[instrument_key] = merged
        except Exception as exc:
            logger.warning(f"[StockFeed] Error processing tick {raw}: {exc}")
