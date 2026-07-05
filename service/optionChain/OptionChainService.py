"""
Orchestrates the live option chain: resolves underlying/expiry, lazily
creates and subscribes a per-(underlying, expiry) OptionChainCache, and
returns the (data, errors) tuple convention used across this codebase
(see service/candleService/CandleService.py).
"""

import asyncio
import logging

from appconfig import OptionMaster
from service.optionChain.OptionChainCache import OptionChainCache

logger = logging.getLogger(__name__)

# NorenApi.subscribe()/unsubscribe() send a frame over the WS connection and
# busy-wait (plain time.sleep loop) until it's connected - if the feed is down
# or stuck reconnecting, that wait never returns. Bounding it here keeps a
# broker-side WS outage from freezing the whole request (and, with only a
# handful of Uvicorn workers, the whole app) - the feed will simply catch up
# once reconnected since ensure_subscribed()/release() are ref-count based.
FEED_SUBSCRIBE_TIMEOUT_SECS = 3.0

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


class OptionChainService:

    RATE = 0.065

    def __init__(self, feed=None):
        """feed: a ShoonyaOptionFeed instance, or None if live ticks are unavailable
        (e.g. Shoonya not connected) - the service still works off REST seeds only."""
        self._feed = feed
        self._caches: dict[str, OptionChainCache] = {}
        self._token_to_cache_keys: dict[str, set[str]] = {}
        # Per-cache-key consumer count: incremented for every holder (a plain
        # GET request briefly, or an SSE stream for its whole connection) and
        # decremented on release. Only when this hits zero do we actually
        # unsubscribe the underlying tokens and evict the cache - otherwise a
        # second concurrent viewer of the same chain silently loses live
        # updates the moment the first viewer disconnects.
        self._refcounts: dict[str, int] = {}
        if feed is not None:
            feed.on_tick(self._route_tick)

    def set_feed(self, feed) -> None:
        """Attaches the live WS feed once it's available (constructed during app
        startup, after this service's module-level singleton is already created)."""
        self._feed = feed
        feed.on_tick(self._route_tick)

    @staticmethod
    def _cache_key(underlying: str, expiry: str) -> str:
        return f"{underlying.upper()}:{expiry}"

    async def _route_tick(self, instrument_key: str, tick_fields: dict) -> None:
        for key in self._token_to_cache_keys.get(instrument_key, ()):
            cache = self._caches.get(key)
            if cache is not None:
                await cache.apply_tick(instrument_key, tick_fields)

    @staticmethod
    def _window_around_spot(strike_chain: dict, spot: float | None) -> dict:
        """Trims the full master strike ladder to STRIKES_EACH_SIDE on either
        side of the current spot price (or the middle of the chain if spot
        isn't known yet)."""
        if not strike_chain:
            return {}
        sorted_strikes = sorted(strike_chain.keys(), key=float)
        if spot is None:
            mid = len(sorted_strikes) // 2
        else:
            mid = min(range(len(sorted_strikes)), key=lambda i: abs(float(sorted_strikes[i]) - spot))
        lo = max(0, mid - STRIKES_EACH_SIDE)
        hi = min(len(sorted_strikes), mid + STRIKES_EACH_SIDE + 1)
        window = sorted_strikes[lo:hi]
        return {strike: strike_chain[strike] for strike in window}

    async def _get_or_create_cache(self, shoonya, underlying: str, expiry: str, spot: float | None) -> tuple[OptionChainCache | None, dict | None]:
        key = self._cache_key(underlying, expiry)
        cache = self._caches.get(key)
        if cache is not None:
            return cache, None

        full_chain = OptionMaster.get_strike_chain(underlying, expiry)
        if not full_chain:
            return None, {"reason": "no_option_data"}

        strike_chain = self._window_around_spot(full_chain, spot)
        exchange = OPTIONS_EXCHANGE[underlying]

        cache = OptionChainCache(underlying.upper(), expiry, strike_chain, exchange=exchange, rate=self.RATE)
        self._caches[key] = cache

        for token in cache.tokens():
            self._token_to_cache_keys.setdefault(token, set()).add(key)

        if self._feed is not None:
            await self._ensure_subscribed_async(cache.tokens())

        await self._seed_from_rest(shoonya, cache, strike_chain, exchange)

        return cache, None

    async def _ensure_subscribed_async(self, tokens: set[str]) -> None:
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, self._feed.ensure_subscribed, tokens),
                timeout=FEED_SUBSCRIBE_TIMEOUT_SECS,
            )
        except Exception as e:
            logger.warning(f"[OptionChainService] ensure_subscribed timed out/failed: {e}")

    async def _seed_from_rest(self, shoonya, cache: OptionChainCache, strike_chain: dict, exchange: str) -> None:
        loop = asyncio.get_running_loop()
        legs = [
            (strike, leg, info[token_field])
            for strike, info in strike_chain.items()
            for leg, token_field in (("ce", "ce_token"), ("pe", "pe_token"))
            if info.get(token_field)
        ]

        async def _fetch_one(strike: str, leg: str, token: str) -> None:
            try:
                quote = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: shoonya.get_option_quote(exchange, token)),
                    timeout=8.0,
                )
                if quote:
                    cache.seed_leg(strike, leg, quote)
            except Exception:
                pass  # leg simply stays unseeded until its first WS tick arrives

        for i in range(0, len(legs), SEED_BATCH_SIZE):
            batch = legs[i:i + SEED_BATCH_SIZE]
            await asyncio.gather(*[_fetch_one(*leg) for leg in batch])

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
        cache = self._caches.pop(key, None)
        if cache is None:
            return

        for token in cache.tokens():
            keys_for_token = self._token_to_cache_keys.get(token)
            if keys_for_token is not None:
                keys_for_token.discard(key)
                if not keys_for_token:
                    self._token_to_cache_keys.pop(token, None)

        if self._feed is not None:
            await self._release_feed_async(cache.tokens())

    async def _release_feed_async(self, tokens: set[str]) -> None:
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, self._feed.release, tokens),
                timeout=FEED_SUBSCRIBE_TIMEOUT_SECS,
            )
        except Exception as e:
            logger.warning(f"[OptionChainService] feed release timed out/failed: {e}")

    async def release_chain(self, underlying: str, expiry: str) -> None:
        """Called when an SSE stream client (that previously called
        get_cache_for_stream) disconnects."""
        await self._release(underlying, expiry)

    async def _resolve_spot(self, shoonya, underlying: str) -> float | None:
        """shoonya.get_index_quote() is a blocking REST call (plain `requests`
        under the hood) - calling it directly from an async def would freeze
        the entire single-threaded event loop for the duration of that HTTP
        round-trip, stalling every other concurrent request (including any
        other SSE stream already open) until it returns. Offload it to a
        worker thread, same as _seed_from_rest() already does for option-leg
        quotes."""
        exch, spot_token = UNDERLYING_SPOT_TOKENS[underlying]
        loop = asyncio.get_running_loop()
        try:
            spot_quote = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: shoonya.get_index_quote(exch, spot_token)),
                timeout=8.0,
            )
        except Exception:
            return None
        return spot_quote["ltp"] if spot_quote else None

    async def get_chain(self, shoonya, underlying: str, expiry: str | None) -> tuple[dict | None, list[dict]]:
        underlying = underlying.lower()
        if underlying not in UNDERLYING_SPOT_TOKENS:
            return None, [{"reason": "invalid_underlying"}]

        if not OptionMaster.is_valid_underlying(underlying):
            return None, [{"reason": "no_option_data"}]

        resolved_expiry = expiry or OptionMaster.nearest_expiry(underlying)
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

    async def get_cache_for_stream(self, shoonya, underlying: str, expiry: str | None) -> tuple[OptionChainCache | None, str | None, list[dict]]:
        """Resolution/subscription-trigger only (no synchronous fetch) - the SSE
        endpoint waits on the returned cache's wait_for_next() directly."""
        underlying = underlying.lower()
        if underlying not in UNDERLYING_SPOT_TOKENS:
            return None, None, [{"reason": "invalid_underlying"}]

        resolved_expiry = expiry or OptionMaster.nearest_expiry(underlying)
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
