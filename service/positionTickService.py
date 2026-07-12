"""
Position Tick Service - Bridges the live market-data feed to the Redis
position cache, and manages WS subscriptions for whatever instruments users
currently hold open positions in (independent of whether anyone has that
instrument's option chain open in the UI - see service/optionChain for that
separate consumer of the same feed).
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from database.positionCache import PositionCache
from service.stopOrderTriggerService import stopOrderTriggerService

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

# StopOrderTriggerService.check_and_trigger does blocking DB I/O (a new
# psycopg2 connection + a FOR UPDATE query + commit) - handle_tick is an
# asyncio coroutine invoked on every single live tick, so calling it inline
# would block the entire event loop (every other instrument/user's ticks,
# and apply_tick above) for the duration of that DB round trip. Runs in its
# own worker thread instead, same rationale and pattern as
# ensure_subscribed/release above; a separate pool from _subscribe_executor
# since DB round trips are typically slower than a WS subscribe frame and
# must not starve subscribe/release calls.
STOP_TRIGGER_CHECK_TIMEOUT_SECS = 3.0


class PositionTickService:

    def __init__(self):
        self.position_cache = PositionCache()
        self._feed = None
        self._subscribe_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="position-feed-subscribe"
        )
        self._stop_trigger_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="stop-order-trigger-check"
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

        # Bonus real-time source for StopOrderTriggerService (see
        # service/stopOrderTriggerService.py) - whenever a position's
        # instrument gets a live tick, also check any dormant STOP/STOPLIMIT
        # orders resting on that same symbol. Best-effort and independent of
        # the position-cache update above: a trigger-check failure must never
        # affect the position cache, and a stale/missing option-master
        # mapping just means this particular tick can't drive a trigger check
        # (the trade-price-based check in ExecutionEngine remains the
        # reliable primary source either way). Offloaded to a worker thread
        # (see STOP_TRIGGER_CHECK_TIMEOUT_SECS) since it does blocking DB I/O
        # and must never block this coroutine's event loop.
        try:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                self._stop_trigger_executor, self._check_stop_orders_for_tick, instrument_key, ltp
            )
            await asyncio.wait_for(future, timeout=STOP_TRIGGER_CHECK_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            logger.warning(
                f"[PositionTickService] stop-order trigger check timed out after "
                f"{STOP_TRIGGER_CHECK_TIMEOUT_SECS}s for {instrument_key} - will retry on next tick/trade"
            )
        except Exception as e:
            logger.warning(f"[PositionTickService] stop-order trigger check failed for {instrument_key}: {e}")

    @staticmethod
    def _check_stop_orders_for_tick(instrument_key: str, ltp) -> None:
        if not instrument_key or "|" not in instrument_key:
            return
        _, token = instrument_key.split("|", 1)

        from appconfig.OptionMaster import find_tsym_aliases_by_token

        # Resting orders may have been submitted using either tradingsymbol
        # convention (see find_by_tsym's docstring), and order_book.symbol is
        # matched by exact string equality - both aliases must be checked.
        for tsym in find_tsym_aliases_by_token(token):
            stopOrderTriggerService.check_and_trigger(tsym, ltp)


positionTickService = PositionTickService()
