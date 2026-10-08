"""
Orchestrates the live option chain: resolves underlying/expiry, lazily
creates and subscribes a per-(underlying, expiry) OptionChainCache, and
returns the (data, errors) tuple convention used across this codebase
(see service/candleService/CandleService.py).
"""

import asyncio
import bisect
import logging
import time

from appconfig import OptionMaster
from service.optionChain.ChainBroadcaster import METRIC_STREAM, ChainBroadcaster, ChainSubscriber
from service.optionChain.OptionChainCache import OptionChainCache
from utils.safe_numbers import safe_float
from utils.streamMetrics import stream_metrics

logger = logging.getLogger(__name__)

# ShoonyaOptionFeed.ensure_subscribed()/release() only update ref-counts and
# queue the WS frame for the feed's sender thread, so they are called inline
# here - no executor hop, no timeout needed (see marketengine/
# WsSubscriptionSender.py).

DEFAULT_RISK_FREE_RATE = 0.065

UNDERLYING_SPOT_TOKENS = {
    "nifty": ("NSE", "26000"),
    "banknifty": ("NSE", "26009"),
    "finnifty": ("NSE", "26037"),
    "sensex": ("BSE", "1"),
}

# NSE index options (Nifty/BankNifty/FinNifty) trade on the NFO segment;
# Sensex (BSE) options trade on the separate BFO segment - a different token
# space, so every option-leg REST/WS call must use the underlying's own
# exchange rather than assuming NFO everywhere.
OPTIONS_EXCHANGE = {
    "nifty": "NFO",
    "banknifty": "NFO",
    "finnifty": "NFO",
    "sensex": "BFO",
}

# A typical option-chain UI shows a window around the money, not all ~200+
# strikes the exchange lists - this also keeps REST-seed calls and WS
# subscriptions bounded (avoids the exchange's undocumented subscription cap).
STRIKES_EACH_SIDE = 20
SEED_BATCH_SIZE = 10
# How long a new chain waits for its REST seed before answering; the rest of
# the seed keeps filling in the background and reaches clients as deltas,
# so one slow quote can't hold back the first screen.
SEED_WAIT_SECS = 1.5


class OptionChainService:

    def __init__(self, feed=None, rate: float = DEFAULT_RISK_FREE_RATE):
        """feed: a ShoonyaOptionFeed instance, or None if live ticks are unavailable
        (e.g. Shoonya not connected) - the service still works off REST seeds only."""
        self._feed = feed
        self._rate = rate
        self._caches: dict[str, OptionChainCache] = {}
        self._token_to_cache_keys: dict[str, set[str]] = {}
        # Per-cache-key consumer count: incremented for every holder (a plain
        # GET request briefly, or an SSE stream for its whole connection) and
        # decremented on release. Only when this hits zero do we actually
        # unsubscribe the underlying tokens and evict the cache - otherwise a
        # second concurrent viewer of the same chain silently loses live
        # updates the moment the first viewer disconnects.
        self._refcounts: dict[str, int] = {}
        self._broadcasters: dict[str, ChainBroadcaster] = {}
        self._seed_tasks: dict[str, asyncio.Task] = {}
        # Index tick -> chains of that underlying, so spot (and the IV that
        # depends on it) stays live instead of frozen at connect time.
        self._spot_key_to_underlying = {
            f"{exch}|{token}": underlying.upper() for underlying, (exch, token) in UNDERLYING_SPOT_TOKENS.items()
        }
        # Anything with get_tick('EXCH|TOKEN') -> {"ltp": ...} | None (the
        # ShoonyaStockFeed tick cache, where index tokens are pinned).
        self._spot_source = None
        if feed is not None:
            feed.on_tick(self._route_tick)

    def set_feed(self, feed) -> None:
        """Attaches the live WS feed once it's available (constructed during app
        startup, after this service's module-level singleton is already created)."""
        self._feed = feed
        feed.on_tick(self._route_tick)

    def set_spot_source(self, spot_source) -> None:
        """Attaches the index tick cache used to resolve spot without REST."""
        self._spot_source = spot_source

    def _cache_key(self, underlying: str, expiry: str) -> str:
        return f"{underlying.upper()}:{expiry}"

    async def _route_tick(self, instrument_key: str, tick_fields: dict) -> None:
        underlying = self._spot_key_to_underlying.get(instrument_key)
        if underlying is not None:
            self._apply_spot(underlying, tick_fields.get("ltp"))
            return

        cache_keys = self._token_to_cache_keys.get(instrument_key)
        if not cache_keys:
            return
        exch_ts = tick_fields.get("exch_ts")
        if exch_ts:
            stream_metrics.record_exch_to_recv(METRIC_STREAM, time.time() * 1000.0 - exch_ts)
        # tuple() snapshot: a concurrent _release may mutate the set while
        # apply_tick awaits its condition lock.
        for key in tuple(cache_keys):
            cache = self._caches.get(key)
            if cache is not None:
                try:
                    await cache.apply_tick(instrument_key, tick_fields)
                except Exception as e:
                    logger.warning(f"[OptionChainService] tick for {instrument_key} not applied: {e}")

    def _apply_spot(self, underlying: str, ltp) -> None:
        spot = safe_float(ltp)
        if spot is None or spot <= 0:
            return
        for cache in self._caches.values():
            if cache.underlying == underlying:
                cache.set_spot(spot)

    def list_expiries(self, underlying: str) -> list[str]:
        """Still-tradable expiries (YYYY-MM-DD, ascending) from the scrip master."""
        return OptionMaster.upcoming_expiries(underlying)

    def _resolve_expiry(self, underlying: str, expiry: str | None) -> str | None:
        """The requested expiry, or the nearest still-tradable listed expiry
        when none was given (after 15:30 on an expiry day, the next one)."""
        return expiry or OptionMaster.nearest_expiry(underlying)

    def _window_around_spot(self, strike_chain: dict, spot: float | None) -> dict:
        """Trims the full master strike ladder to STRIKES_EACH_SIDE on either
        side of the current spot price (or the middle of the chain if spot
        isn't known yet). Non-numeric strike keys are skipped."""
        numeric = sorted(
            (strike_value, strike)
            for strike, strike_value in ((strike, safe_float(strike)) for strike in strike_chain)
            if strike_value is not None
        )
        if not numeric:
            return {}
        if spot is None:
            mid = len(numeric) // 2
        else:
            mid = bisect.bisect_left(numeric, (spot, ""))
            if mid >= len(numeric) or (mid > 0 and spot - numeric[mid - 1][0] <= numeric[mid][0] - spot):
                mid -= 1
        lo = max(0, mid - STRIKES_EACH_SIDE)
        hi = min(len(numeric), mid + STRIKES_EACH_SIDE + 1)
        return {strike: strike_chain[strike] for _, strike in numeric[lo:hi]}

    async def _get_or_create_cache(self, shoonya, underlying: str, expiry: str, spot: float | None) -> tuple[OptionChainCache | None, dict | None]:
        key = self._cache_key(underlying, expiry)
        cache = self._caches.get(key)
        if cache is not None:
            return cache, None

        full_chain = OptionMaster.get_strike_chain(underlying, expiry)
        if not full_chain:
            return None, {"reason": "no_option_data"}

        strike_chain = self._window_around_spot(full_chain, spot)
        if not strike_chain:
            return None, {"reason": "no_option_data"}
        exchange = OPTIONS_EXCHANGE[underlying]

        cache = OptionChainCache(underlying.upper(), expiry, strike_chain, exchange=exchange, rate=self._rate)
        self._caches[key] = cache

        for token in cache.tokens():
            self._token_to_cache_keys.setdefault(token, set()).add(key)

        self._subscribe_feed(cache.tokens())

        seed_task = asyncio.get_running_loop().create_task(
            self._seed_from_rest(shoonya, cache, strike_chain, exchange), name=f"chain-seed-{key}"
        )
        self._seed_tasks[key] = seed_task
        seed_task.add_done_callback(lambda _task, seed_key=key: self._seed_tasks.pop(seed_key, None))
        await asyncio.wait({seed_task}, timeout=SEED_WAIT_SECS)

        return cache, None

    def _subscribe_feed(self, tokens: set[str]) -> None:
        if self._feed is None:
            return
        try:
            self._feed.ensure_subscribed(tokens)
        except Exception as e:
            logger.warning(f"[OptionChainService] ensure_subscribed failed: {e}")

    async def _seed_from_rest(self, shoonya, cache: OptionChainCache, strike_chain: dict, exchange: str) -> None:
        loop = asyncio.get_running_loop()
        legs = [
            (strike, leg, info[token_field])
            for strike, info in strike_chain.items()
            for leg, token_field in (("ce", "ce_token"), ("pe", "pe_token"))
            if info.get(token_field)
        ]

        # Sliding window rather than lock-step batches: the same cap on
        # in-flight broker calls, but one slow quote no longer holds back the
        # next SEED_BATCH_SIZE legs.
        in_flight = asyncio.Semaphore(SEED_BATCH_SIZE)

        async def _fetch_one(strike: str, leg: str, token: str) -> None:
            try:
                async with in_flight:
                    quote = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: shoonya.get_option_quote(exchange, token)),
                        timeout=8.0,
                    )
                if quote:
                    cache.seed_leg(strike, leg, quote)
            except Exception as exc:
                # The leg stays unseeded until its first WS tick arrives.
                logger.debug(f"[OptionChainService] REST seed for {exchange}|{token} failed: {exc!r}")

        try:
            await asyncio.gather(*[_fetch_one(*leg) for leg in legs])
        except Exception as e:
            logger.warning(f"[OptionChainService] REST seed failed: {e}")

    def _acquire(self, underlying: str, expiry: str) -> None:
        key = self._cache_key(underlying, expiry)
        self._refcounts[key] = self._refcounts.get(key, 0) + 1

    async def _release(self, underlying: str, expiry: str) -> None:
        """Decrements the consumer count; only once it reaches zero do we
        unsubscribe from the feed and evict the cache + its token-index
        entries, so a still-connected concurrent viewer of the same chain
        never gets cut off by someone else's disconnect."""
        key = self._cache_key(underlying, expiry)
        remaining = max(self._refcounts.get(key, 0) - 1, 0)

        if remaining > 0:
            self._refcounts[key] = remaining
            return

        self._refcounts.pop(key, None)
        broadcaster = self._broadcasters.pop(key, None)
        if broadcaster is not None:
            broadcaster.stop()
        seed_task = self._seed_tasks.pop(key, None)
        if seed_task is not None:
            seed_task.cancel()
        cache = self._caches.pop(key, None)
        if cache is None:
            return

        for token in cache.tokens():
            keys_for_token = self._token_to_cache_keys.get(token)
            if keys_for_token is not None:
                keys_for_token.discard(key)
                if not keys_for_token:
                    self._token_to_cache_keys.pop(token, None)

        self._release_feed(cache.tokens())

    def _release_feed(self, tokens: set[str]) -> None:
        if self._feed is None:
            return
        try:
            self._feed.release(tokens)
        except Exception as e:
            logger.warning(f"[OptionChainService] feed release failed: {e}")

    def subscribe_stream(self, underlying: str, expiry: str, fmt: str) -> ChainSubscriber | None:
        """Adds a live SSE client to the chain's broadcaster (created on the
        first client). The chain must already be held via
        get_cache_for_stream(); None if it is not cached."""
        key = self._cache_key(underlying, expiry)
        cache = self._caches.get(key)
        if cache is None:
            return None
        broadcaster = self._broadcasters.get(key)
        if broadcaster is None:
            broadcaster = ChainBroadcaster(cache)
            self._broadcasters[key] = broadcaster
        return broadcaster.subscribe(fmt)

    def unsubscribe_stream(self, underlying: str, expiry: str, subscriber: ChainSubscriber) -> None:
        broadcaster = self._broadcasters.get(self._cache_key(underlying, expiry))
        if broadcaster is not None:
            broadcaster.unsubscribe(subscriber)

    def resync_stream(self, underlying: str, expiry: str, subscriber: ChainSubscriber) -> None:
        broadcaster = self._broadcasters.get(self._cache_key(underlying, expiry))
        if broadcaster is not None:
            broadcaster.resync(subscriber)

    async def release_chain(self, underlying: str, expiry: str) -> None:
        """Called when an SSE stream client (that previously called
        get_cache_for_stream) disconnects."""
        try:
            await self._release(underlying, expiry)
        except Exception as e:
            logger.warning(f"[OptionChainService] release_chain {underlying} {expiry} failed: {e}")

    async def _resolve_spot(self, shoonya, underlying: str) -> float | None:
        """shoonya.get_index_quote() is a blocking REST call (plain `requests`
        under the hood) - calling it directly from an async def would freeze
        the entire single-threaded event loop for the duration of that HTTP
        round-trip, stalling every other concurrent request (including any
        other SSE stream already open) until it returns. Offload it to a
        worker thread, same as _seed_from_rest() already does for option-leg
        quotes."""
        try:
            exch, spot_token = UNDERLYING_SPOT_TOKENS[underlying]
        except KeyError:
            return None

        # Live index tick from the shared WebSocket first: an in-memory read
        # instead of a ~100ms REST round trip on every chain request.
        spot = self._spot_from_tick(f"{exch}|{spot_token}")
        if spot is not None:
            return spot

        try:
            loop = asyncio.get_running_loop()
            spot_quote = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: shoonya.get_index_quote(exch, spot_token)),
                timeout=8.0,
            )
            return spot_quote["ltp"] if spot_quote else None
        except Exception:
            return None

    def _spot_from_tick(self, instrument_key: str) -> float | None:
        if self._spot_source is None:
            return None
        try:
            tick = self._spot_source.get_tick(instrument_key)
            ltp = float(tick.get("ltp") or 0) if tick else 0.0
            return ltp if ltp > 0 else None
        except Exception:
            return None

    async def get_chain(self, shoonya, underlying: str, expiry: str | None) -> tuple[dict | None, list[dict]]:
        try:
            return await self._get_chain(shoonya, underlying, expiry)
        except Exception as e:
            logger.error(f"[OptionChainService] get_chain {underlying} {expiry} failed: {e}", exc_info=True)
            return None, [{"reason": "option_chain_failed"}]

    async def _get_chain(self, shoonya, underlying: str, expiry: str | None) -> tuple[dict | None, list[dict]]:
        underlying = underlying.lower()
        if underlying not in UNDERLYING_SPOT_TOKENS:
            return None, [{"reason": "invalid_underlying"}]

        if not OptionMaster.is_valid_underlying(underlying):
            return None, [{"reason": "no_option_data"}]

        resolved_expiry = self._resolve_expiry(underlying, expiry)
        if not resolved_expiry:
            return None, [{"reason": "no_expiry_available"}]

        spot = await self._resolve_spot(shoonya, underlying)

        cache, error = await self._get_or_create_cache(shoonya, underlying, resolved_expiry, spot)
        if cache is None:
            return None, [error]

        # A plain GET is a one-shot read, not a persistent holder, so it
        # deliberately does NOT touch the refcount (see _acquire/_release):
        # a client polling this endpoint every few seconds must reuse the
        # existing subscription/cache rather than re-subscribing and
        # re-seeding from REST on every single poll. There's no eviction
        # race to guard against here either, since nothing awaits between
        # getting `cache` above and reading its snapshot below - no other
        # coroutine can run `_release` on this key in between.
        if spot is not None:
            cache.set_spot(spot)

        snapshot = cache.get()
        if snapshot is None:
            return None, [{"reason": "no_option_data"}]

        return {
            "symbol": underlying.upper(),
            "exchange": OPTIONS_EXCHANGE[underlying],
            "expiry": resolved_expiry,
            "spot": snapshot["spot"],
            "strikes": snapshot["strikes"],
        }, []

    def peek_cached_chain(self, underlying: str, expiry: str | None) -> tuple[dict | None, str | None]:
        """Best-effort read of whatever this process already has cached for
        (underlying, resolved expiry), without touching Shoonya at all - used
        when the broker session is down so the endpoint can still serve
        yesterday's/last-known data instead of a bare 503. Returns
        (None, None) if no cache exists yet for this key (e.g. right after a
        process restart, before any live session has ever seeded it)."""
        try:
            return self._peek_cached_chain(underlying, expiry)
        except Exception as e:
            logger.warning(f"[OptionChainService] peek_cached_chain {underlying} {expiry} failed: {e}")
            return None, None

    def _peek_cached_chain(self, underlying: str, expiry: str | None) -> tuple[dict | None, str | None]:
        underlying = underlying.lower()
        resolved_expiry = self._resolve_expiry(underlying, expiry)
        if not resolved_expiry:
            return None, None

        cache = self._caches.get(self._cache_key(underlying, resolved_expiry))
        if cache is None:
            return None, None

        snapshot = cache.get()
        if snapshot is None:
            return None, None

        return {
            "symbol": underlying.upper(),
            "exchange": OPTIONS_EXCHANGE.get(underlying, "NFO"),
            "expiry": resolved_expiry,
            "spot": snapshot["spot"],
            "strikes": snapshot["strikes"],
        }, resolved_expiry

    async def get_cache_for_stream(self, shoonya, underlying: str, expiry: str | None) -> tuple[OptionChainCache | None, str | None, list[dict]]:
        """Resolves and holds the chain for a stream's lifetime (release with
        release_chain); the stream then reads it through subscribe_stream()."""
        try:
            return await self._get_cache_for_stream(shoonya, underlying, expiry)
        except Exception as e:
            logger.error(f"[OptionChainService] get_cache_for_stream {underlying} {expiry} failed: {e}", exc_info=True)
            return None, None, [{"reason": "option_chain_failed"}]

    async def _get_cache_for_stream(self, shoonya, underlying: str, expiry: str | None) -> tuple[OptionChainCache | None, str | None, list[dict]]:
        underlying = underlying.lower()
        if underlying not in UNDERLYING_SPOT_TOKENS:
            return None, None, [{"reason": "invalid_underlying"}]

        resolved_expiry = self._resolve_expiry(underlying, expiry)
        if not resolved_expiry:
            return None, None, [{"reason": "no_expiry_available"}]

        spot = await self._resolve_spot(shoonya, underlying)

        cache, error = await self._get_or_create_cache(shoonya, underlying, resolved_expiry, spot)
        if cache is None:
            return None, None, [error]

        # Holds this chain open for as long as the SSE connection lives -
        # released via release_chain() on disconnect (see release_chain()).
        self._acquire(underlying, resolved_expiry)

        if spot is not None:
            cache.set_spot(spot)

        return cache, resolved_expiry, []
