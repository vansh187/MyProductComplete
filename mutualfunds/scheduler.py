import asyncio
import logging

from mutualfunds.backfill_service import MFNavBackfillService
from mutualfunds.curation_service import MFCurationService
from mutualfunds.daily_sync_service import MFDailyNavSyncService
from mutualfunds.sync_service import MFSchemeMasterSyncService

logger = logging.getLogger("mutualfunds.scheduler")

_BACKFILL_BATCH_SIZE = 4000
_REFRESH_INTERVAL_SECONDS = 24 * 3600


class MutualFundBackgroundJobRunner:
    """Orchestrates the daily job sequence: scheme master sync -> backfill batch -> daily NAV sync -> LLM curation."""

    def __init__(self, master_sync: MFSchemeMasterSyncService, backfill: MFNavBackfillService,
                 daily_sync: MFDailyNavSyncService, curation: MFCurationService):
        self._master_sync = master_sync
        self._backfill = backfill
        self._daily_sync = daily_sync
        self._curation = curation

    async def run_once(self) -> None:
        await self._master_sync.sync()
        await self._backfill.backfill_batch(_BACKFILL_BATCH_SIZE)
        await self._daily_sync.sync_all_backfilled()
        await self._curation.curate_all()


async def schedule_daily_refresh(app=None) -> None:
    """
    Background task entry point (started from app.py lifespan via
    _supervised_background_task, which expects an `async def fn(app)`).
    Runs immediately at startup, unlike OptionMaster's sleep-first loop -
    there is no pre-seeded data file for mutual funds, so the first run
    must populate the database rather than waiting 24h - then repeats daily.
    """
    runner = getattr(app.state, "mutual_fund_job_runner", None)
    if runner is None:
        logger.warning("[MutualFunds] job runner not initialized - skipping background refresh")
        return

    while True:
        try:
            await runner.run_once()
            logger.info("[MutualFunds] daily job sequence complete")
        except Exception as exc:
            logger.error(f"[MutualFunds] daily job sequence failed: {exc}")
        await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)
