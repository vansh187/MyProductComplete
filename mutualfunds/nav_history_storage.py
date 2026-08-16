import io
import logging
import time
from datetime import date

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from storage3.exceptions import StorageApiError
from supabase import Client

from productdto.mutualFundDto import NavPointDTO

logger = logging.getLogger("mutualfunds.nav_history_storage")

DEFAULT_BUCKET_NAME = "mutual-fund-nav-history"
_NOT_FOUND_STATUSES = {404, "404"}

# Transient failures observed under concurrent load against Supabase Storage
# (mirrors the same class of issue found in MfApiInProvider against
# mfapi.in): httpx.RemoteProtocolError ("Server disconnected without
# sending a response") and other transport-level errors succeed on a bare
# retry moments later, so this is a bounded retry-with-backoff at the same
# layer, not a caller-side concern.
_MAX_ATTEMPTS = 3
_RETRYABLE_EXCEPTIONS = (httpx.TransportError, OSError)
_BACKOFF_BASE_SECONDS = 0.5


class MFNavHistoryParquetStorage:
    """
    Same interface as MFNavHistoryRepository (bulk_insert, append_daily,
    get_series) - a drop-in swap behind MutualFundService/MFNavBackfillService/
    MFDailyNavSyncService's existing constructor injection, no business logic
    touched. Stores one compressed Parquet file per scheme in Supabase
    Storage instead of Postgres rows: Postgres storage was hard-capped on
    the current plan (mf_nav_history alone was ~750MB, 98% of the whole
    database), while Supabase Storage carries a separate, larger/cheaper
    quota - moving the highest-volume, single-scheme-at-a-time-accessed data
    (NAV history) out relieves the DB cap without touching search/explore/
    collections, which never read full history (they read mf_scheme_returns,
    a ~1MB table, and stay in Postgres).

    Parquet has no native row-append, so every write here is a full
    read-merge-write of that one scheme's (small, capped) file - acceptable
    because files are scoped per scheme (capped ~1200-1300 rows each), not
    a rewrite of the whole dataset.
    """

    def __init__(self, supabase_client: Client, bucket_name: str = DEFAULT_BUCKET_NAME):
        self._client = supabase_client
        self._bucket_name = bucket_name

    def bulk_insert(self, scheme_code: int, points: list[NavPointDTO]) -> None:
        """One-time backfill write - overwrites with the (already-capped) full series."""
        self._merge_and_write(scheme_code, points)

    def append_daily(self, scheme_code: int, nav_date: date, nav: float) -> None:
        self._merge_and_write(scheme_code, [NavPointDTO(nav_date=nav_date, nav=nav)])

    def get_series(self, scheme_code: int, since: date | None = None) -> list[NavPointDTO]:
        points = self._read(scheme_code)
        if since is not None:
            points = [point for point in points if point.nav_date >= since]
        return points

    def _merge_and_write(self, scheme_code: int, new_points: list[NavPointDTO]) -> None:
        if not new_points:
            return
        existing = self._read(scheme_code)
        merged_by_date = {point.nav_date: point.nav for point in existing}
        for point in new_points:
            merged_by_date[point.nav_date] = point.nav
        ordered = sorted(merged_by_date.items())

        table = pa.table({
            "nav_date": [d.isoformat() for d, _ in ordered],
            "nav": [nav for _, nav in ordered],
        })
        buffer = io.BytesIO()
        pq.write_table(table, buffer, compression="zstd")

        self._with_retry(
            lambda: self._client.storage.from_(self._bucket_name).upload(
                self._path_for(scheme_code),
                buffer.getvalue(),
                file_options={"upsert": "true", "content-type": "application/octet-stream"},
            )
        )

    def _read(self, scheme_code: int) -> list[NavPointDTO]:
        try:
            raw_bytes = self._with_retry(
                lambda: self._client.storage.from_(self._bucket_name).download(self._path_for(scheme_code))
            )
        except StorageApiError as ex:
            if ex.status in _NOT_FOUND_STATUSES:
                return []  # no file yet - genuinely empty history, not an error
            raise

        table = pq.read_table(io.BytesIO(raw_bytes))
        dates = table.column("nav_date").to_pylist()
        navs = table.column("nav").to_pylist()
        return [NavPointDTO(nav_date=date.fromisoformat(d), nav=float(n)) for d, n in zip(dates, navs)]

    def _with_retry(self, call):
        last_exception = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return call()
            except _RETRYABLE_EXCEPTIONS as ex:
                last_exception = ex
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
        raise last_exception

    def _path_for(self, scheme_code: int) -> str:
        return f"{scheme_code}.parquet"
