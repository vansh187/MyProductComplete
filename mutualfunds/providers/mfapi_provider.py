import asyncio
from datetime import date, datetime

import httpx

from mutualfunds.providers.base import MFDataProvider
from productdto.mutualFundDto import NavPointDTO

MFAPI_BASE_URL = "https://api.mfapi.in/mf"

# Transient failures observed in practice under concurrent load against
# mfapi.in: httpx timeouts (str() is empty on these - they carry no message,
# only the exception type), connection resets, and even OS-level DNS
# resolution hiccups (socket.gaierror / OSError [Errno 11001] on Windows)
# when many requests fire at once. All of these succeed on a bare retry a
# moment later, so bounded retry-with-backoff belongs at the HTTP layer,
# benefiting every caller (backfill, daily sync, live nav-chart) instead of
# leaving each one to reimplement it.
_MAX_ATTEMPTS = 3
_RETRYABLE_EXCEPTIONS = (httpx.TransportError, OSError)
_BACKOFF_BASE_SECONDS = 0.5


class MfApiInProvider(MFDataProvider):
    """
    Concrete MFDataProvider backed by mfapi.in - a free, unauthenticated
    public API. No SLA/rate-limit guarantees, so every call here is bounded
    by the injected client's timeout, with a small bounded retry for
    transient network failures (see _RETRYABLE_EXCEPTIONS); callers
    (services) are still responsible for falling back to stored data if a
    call exhausts its retries and still fails.
    """

    def __init__(self, http_client: httpx.AsyncClient):
        self._http_client = http_client

    async def get_all_schemes(self) -> list[dict]:
        response = await self._get_with_retry(MFAPI_BASE_URL)
        payload = response.json()
        return [
            {"scheme_code": int(entry["schemeCode"]), "scheme_name": entry["schemeName"]}
            for entry in payload
        ]

    async def fetch_scheme_with_history(self, scheme_code: int) -> tuple[dict, list[NavPointDTO]]:
        response = await self._get_with_retry(f"{MFAPI_BASE_URL}/{scheme_code}")
        payload = response.json()
        meta = self._normalize_meta(payload.get("meta", {}))
        points = [
            NavPointDTO(nav_date=self._parse_date(row["date"]), nav=float(row["nav"]))
            for row in payload.get("data", [])
            if row.get("nav") not in (None, "", "N.A.")
        ]
        points.sort(key=lambda point: point.nav_date)
        return meta, points

    async def get_latest_nav(self, scheme_code: int) -> tuple[date, float] | None:
        response = await self._get_with_retry(f"{MFAPI_BASE_URL}/{scheme_code}/latest")
        payload = response.json()
        rows = payload.get("data", [])
        if not rows or rows[0].get("nav") in (None, "", "N.A."):
            return None
        return self._parse_date(rows[0]["date"]), float(rows[0]["nav"])

    async def _get_with_retry(self, url: str) -> httpx.Response:
        last_exception = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await self._http_client.get(url)
                response.raise_for_status()
                return response
            except _RETRYABLE_EXCEPTIONS as ex:
                last_exception = ex
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
        raise last_exception

    def _normalize_meta(self, raw_meta: dict) -> dict:
        return {
            "fund_house": raw_meta.get("fund_house"),
            "scheme_type": raw_meta.get("scheme_type"),
            "scheme_category": raw_meta.get("scheme_category"),
            "scheme_name": raw_meta.get("scheme_name"),
            "isin_growth": raw_meta.get("isin_growth"),
            "isin_div_reinvestment": raw_meta.get("isin_div_reinvestment"),
        }

    def _parse_date(self, raw_date: str) -> date:
        return datetime.strptime(raw_date, "%d-%m-%Y").date()
