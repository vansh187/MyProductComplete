import asyncio
import logging

from mutualfunds.nav_history_repository import MFNavHistoryRepository
from mutualfunds.providers.base import MFDataProvider
from mutualfunds.repository import MFSchemeRepository
from mutualfunds.returns_calculator import MFReturnsCalculator
from mutualfunds.returns_repository import MFReturnsRepository

logger = logging.getLogger("mutualfunds.daily_sync")

_CONCURRENCY = 10  # bounded, matches the batching style used elsewhere in this repo (TopMoversFetcher)


class MFDailyNavSyncService:
    """
    Daily job: for every already-backfilled (is_active=TRUE) scheme, appends
    just today's NAV (the cheap endpoint) and recomputes returns from the
    now-updated series - the ongoing maintenance cost of the whole database,
    forever, is one cheap call per active scheme per day.
    """

    def __init__(self, provider: MFDataProvider, scheme_repository: MFSchemeRepository,
                 nav_history_repository: MFNavHistoryRepository, returns_repository: MFReturnsRepository,
                 returns_calculator: MFReturnsCalculator):
        self._provider = provider
        self._scheme_repository = scheme_repository
        self._nav_history_repository = nav_history_repository
        self._returns_repository = returns_repository
        self._returns_calculator = returns_calculator

    async def sync_all_backfilled(self) -> None:
        codes = await self._run_sync(self._scheme_repository.get_all_backfilled_scheme_codes)
        logger.info(f"[MFDailyNavSyncService] syncing {len(codes)} active schemes")

        semaphore = asyncio.Semaphore(_CONCURRENCY)

        async def _sync_one(scheme_code: int) -> None:
            async with semaphore:
                try:
                    await self._sync_scheme(scheme_code)
                except Exception as ex:
                    logger.warning(f"[MFDailyNavSyncService] failed for {scheme_code}: {ex}")

        await asyncio.gather(*(_sync_one(code) for code in codes))
        logger.info("[MFDailyNavSyncService] sync complete")

    async def _sync_scheme(self, scheme_code: int) -> None:
        latest = await self._provider.get_latest_nav(scheme_code)
        if latest is None:
            return
        nav_date, nav = latest
        await self._run_sync(self._nav_history_repository.append_daily, scheme_code, nav_date, nav)

        series = await self._run_sync(self._nav_history_repository.get_series, scheme_code, None)
        returns = self._returns_calculator.compute_trailing_returns(series)
        await self._run_sync(self._returns_repository.upsert_returns, scheme_code, returns)

    async def _run_sync(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)
