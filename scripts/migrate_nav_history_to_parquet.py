"""
One-time migration: exports every active scheme's mf_nav_history rows from
Postgres into a per-scheme compressed Parquet file in Supabase Storage
(MFNavHistoryParquetStorage), then verifies row-count parity before the
caller truncates the Postgres table to reclaim space.

Usage: python scripts/migrate_nav_history_to_parquet.py
"""
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from supabase import create_client

from mutualfunds.nav_history_repository import MFNavHistoryRepository
from mutualfunds.nav_history_storage import MFNavHistoryParquetStorage
from mutualfunds.repository import MFSchemeRepository

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("migrate_nav_history")

_CONCURRENCY = 8


class NavHistoryMigrator:
    """Instance-based orchestrator for the one-time Postgres -> Parquet migration."""

    def __init__(self, scheme_repository: MFSchemeRepository, pg_nav_repository: MFNavHistoryRepository,
                 parquet_storage: MFNavHistoryParquetStorage, concurrency: int = _CONCURRENCY):
        self._scheme_repository = scheme_repository
        self._pg_nav_repository = pg_nav_repository
        self._parquet_storage = parquet_storage
        self._concurrency = concurrency
        self.migrated = 0
        self.mismatched: list[int] = []
        self.failed: list[int] = []

    async def migrate_all(self) -> None:
        codes = await self._run_sync(self._get_active_codes)
        logger.info(f"Migrating NAV history for {len(codes)} active schemes (concurrency={self._concurrency})")

        semaphore = asyncio.Semaphore(self._concurrency)
        done = 0

        async def _migrate_one(code: int) -> None:
            nonlocal done
            async with semaphore:
                try:
                    await self._migrate_scheme(code)
                    self.migrated += 1
                except Exception as ex:
                    logger.warning(f"FAILED scheme {code}: {ex}")
                    self.failed.append(code)
                done += 1
                if done % 500 == 0:
                    logger.info(f"Progress: {done}/{len(codes)}")

        await asyncio.gather(*(_migrate_one(code) for code in codes))
        logger.info(
            f"Done. migrated={self.migrated} mismatched={len(self.mismatched)} failed={len(self.failed)}"
        )
        if self.mismatched:
            logger.warning(f"Mismatched schemes (row count differs after write): {self.mismatched}")
        if self.failed:
            logger.warning(f"Failed schemes (need re-run): {self.failed}")

    async def _migrate_scheme(self, scheme_code: int) -> None:
        points = await self._run_sync(self._pg_nav_repository.get_series, scheme_code, None)
        if not points:
            return
        await self._run_sync(self._parquet_storage.bulk_insert, scheme_code, points)
        readback = await self._run_sync(self._parquet_storage.get_series, scheme_code, None)
        if len(readback) != len(points) or readback != points:
            self.mismatched.append(scheme_code)

    def _get_active_codes(self) -> list[int]:
        return self._scheme_repository.get_all_backfilled_scheme_codes()

    async def _run_sync(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)


async def main() -> None:
    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SERVICE_ROLE_KEY"))
    migrator = NavHistoryMigrator(
        scheme_repository=MFSchemeRepository(),
        pg_nav_repository=MFNavHistoryRepository(),
        parquet_storage=MFNavHistoryParquetStorage(client),
    )
    await migrator.migrate_all()


if __name__ == "__main__":
    asyncio.run(main())
