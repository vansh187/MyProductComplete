import asyncio
import logging

from mutualfunds.providers.base import MFDataProvider
from mutualfunds.repository import MFSchemeRepository

logger = logging.getLogger("mutualfunds.sync")

_UPSERT_BATCH_SIZE = 5000


class MFSchemeMasterSyncService:
    """Daily job: refreshes the full mf_schemes catalog (identity only - scheme_code/scheme_name).
    Category/fund_house/is_active are filled in later by MFNavBackfillService."""

    def __init__(self, provider: MFDataProvider, scheme_repository: MFSchemeRepository):
        self._provider = provider
        self._scheme_repository = scheme_repository

    async def sync(self) -> None:
        schemes = await self._provider.get_all_schemes()
        logger.info(f"[MFSchemeMasterSyncService] fetched {len(schemes)} schemes from provider")

        loop = asyncio.get_running_loop()
        for start in range(0, len(schemes), _UPSERT_BATCH_SIZE):
            batch = schemes[start:start + _UPSERT_BATCH_SIZE]
            await loop.run_in_executor(None, self._scheme_repository.upsert_schemes, batch)
        logger.info("[MFSchemeMasterSyncService] sync complete")
