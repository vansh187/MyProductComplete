"""
Position Tick Service - Bridges the live market-data feed to the Redis
position cache, and manages WS subscriptions for whatever instruments users
currently hold open positions in (independent of whether anyone has that
instrument's option chain open in the UI - see service/optionChain for that
separate consumer of the same feed).
"""

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from database.positionCache import PositionCache

logger = logging.getLogger(__name__)

# NorenApi.subscribe()/unsubscribe() send a frame over the WS connection and
# busy-wait (plain time.sleep loop) until it's connected - see
# service/optionChain/OptionChainService.py's identical comment, which bounds
# the same call with asyncio.wait_for(). ensure_subscribed()/release() here
# are called synchronously from PositionService.apply_fill() WHILE HOLDING
# the Redis position lock for (user_id, tsym) - an unbounded hang here during
# a broker feed outage would wedge that lock (and the DB transaction/cursor
# holding it) indefinitely, blocking every subsequent fill for that user on
# that instrument. Bounding it in a worker thread keeps a feed outage from
# ever blocking order settlement - live ticks are a best-effort enhancement,
# never a correctness dependency (positions are fully valid without them).
FEED_SUBSCRIBE_TIMEOUT_SECS = 3.0


class PositionTickService:

    def __init__(self):
        self.position_cache = PositionCache()
        self._feed = None
        self._subscribe_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="position-feed-subscribe"
        )

    def set_feed(self, feed) -> None:
        """Attaches the live WS feed once it's available (constructed during
        app startup - see app.py's lifespan)."""
        self._feed = feed
        feed.on_tick(self.handle_tick)

    def ensure_subscribed(self, exchange: str, token: str) -> None:
        """Subscribes the feed to an instrument a user just opened/added to a
        position in. Ref-counted on the feed side, so this is safe to call on
        every fill without double-subscribing. No-op if the feed isn't up yet
        (e.g. broker session still connecting) - the position simply won't
        get live ticks until it is."""
        if self._feed is None or not exchange or not token:
            return
        try:
            future = self._subscribe_executor.submit(self._feed.ensure_subscribed, {f"{exchange}|{token}"})
            future.result(timeout=FEED_SUBSCRIBE_TIMEOUT_SECS)
        except FutureTimeoutError:
            logger.warning(
                f"[PositionTickService] ensure_subscribed timed out after "
                f"{FEED_SUBSCRIBE_TIMEOUT_SECS}s for {exchange}|{token} - feed likely "
                f"reconnecting, continuing without live ticks for this fill"
            )
        except Exception as e:
            logger.warning(f"[PositionTickService] ensure_subscribed failed for {exchange}|{token}: {e}")

    def release(self, exchange: str, token: str) -> None:
        """Releases the feed subscription once a position closes."""
        if self._feed is None or not exchange or not token:
            return
        try:
            future = self._subscribe_executor.submit(self._feed.release, {f"{exchange}|{token}"})
            future.result(timeout=FEED_SUBSCRIBE_TIMEOUT_SECS)
        except FutureTimeoutError:
            logger.warning(
                f"[PositionTickService] release timed out after "
                f"{FEED_SUBSCRIBE_TIMEOUT_SECS}s for {exchange}|{token} - feed likely "
                f"reconnecting, subscription will be cleaned up once it recovers"
            )
        except Exception as e:
            logger.warning(f"[PositionTickService] release failed for {exchange}|{token}: {e}")

    async def handle_tick(self, instrument_key: str, tick_fields: dict) -> None:
        ltp = tick_fields.get("ltp")
        if ltp is None:
            return
        try:
            await self.position_cache.apply_tick(instrument_key, ltp)
        except Exception as e:
            logger.warning(f"[PositionTickService] apply_tick failed for {instrument_key}: {e}")


positionTickService = PositionTickService()
