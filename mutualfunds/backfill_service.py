import asyncio
import logging
from datetime import date

from mutualfunds.nav_capping import MFNavHistoryCapper
from mutualfunds.nav_history_repository import MFNavHistoryRepository
from mutualfunds.providers.base import MFDataProvider
from mutualfunds.repository import MFSchemeRepository
from mutualfunds.returns_calculator import MFReturnsCalculator
from mutualfunds.returns_repository import MFReturnsRepository

logger = logging.getLogger("mutualfunds.backfill")

_DEFAULT_CONCURRENCY = 10  # matches MFDailyNavSyncService - bounded so a large
                            # batch doesn't hammer mfapi.in (free, unauthenticated,
                            # no published rate limit) all at once


class MFNavBackfillService:
    """
    One-time-per-scheme NAV backfill, gated by a cheap staleness check so
    the database only ever grows to cover schemes that are actually live -
    see MFSchemeRepository.get_schemes_pending_backfill / mark_inactive /
    mark_active_and_backfilled.
    """

    def __init__(self, provider: MFDataProvider, scheme_repository: MFSchemeRepository,
                 nav_history_repository: MFNavHistoryRepository, returns_repository: MFReturnsRepository,
                 returns_calculator: MFReturnsCalculator, staleness_threshold_days: int = 7,
                 nav_capper: MFNavHistoryCapper = None, concurrency: int = _DEFAULT_CONCURRENCY):
        self._provider = provider
        self._scheme_repository = scheme_repository
        self._nav_history_repository = nav_history_repository
        self._returns_repository = returns_repository
        self._returns_calculator = returns_calculator
        self._staleness_threshold_days = staleness_threshold_days
        self._nav_capper = nav_capper or MFNavHistoryCapper()
        self._concurrency = concurrency

    async def backfill_batch(self, batch_size: int) -> None:
        codes = await self._run_sync(self._scheme_repository.get_schemes_pending_backfill, batch_size)
        logger.info(f"[MFNavBackfillService] processing batch of {len(codes)} schemes (concurrency={self._concurrency})")

        semaphore = asyncio.Semaphore(self._concurrency)

        async def _backfill_with_limit(code: int) -> None:
            async with semaphore:
                try:
                    await self.backfill_one(code)
                except Exception as ex:
                    logger.warning(f"[MFNavBackfillService] failed to backfill {code}: {ex}")

        await asyncio.gather(*(_backfill_with_limit(code) for code in codes))

    async def backfill_one(self, scheme_code: int) -> None:
        latest = await self._provider.get_latest_nav(scheme_code)
        if latest is None or self._is_stale(latest[0]):
            await self._run_sync(self._scheme_repository.mark_inactive, scheme_code)
            return

        meta, points = await self._provider.fetch_scheme_with_history(scheme_code)
        capped_points = self._nav_capper.cap(points)

        await self._run_sync(self._nav_history_repository.bulk_insert, scheme_code, capped_points)
        returns = self._returns_calculator.compute_trailing_returns(capped_points)
        await self._run_sync(self._returns_repository.upsert_returns, scheme_code, returns)
        await self._run_sync(self._scheme_repository.mark_active_and_backfilled, scheme_code, meta)

    def _is_stale(self, latest_nav_date: date) -> bool:
        return (date.today() - latest_nav_date).days > self._staleness_threshold_days

    async def _run_sync(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)
