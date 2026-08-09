import asyncio
import logging
from datetime import date

from dateutil.relativedelta import relativedelta

from mutualfunds.cache import MFInMemoryCache
from mutualfunds.collections_config import MFCollectionsCatalog
from mutualfunds.curation.curated_picks_repository import MFCuratedPicksRepository
from mutualfunds.nav_capping import MFNavHistoryCapper
from mutualfunds.nav_history_repository import MFNavHistoryRepository
from mutualfunds.providers.base import MFDataProvider
from mutualfunds.repository import MFSchemeRepository
from mutualfunds.returns_calculator import MFReturnsCalculator
from mutualfunds.returns_repository import MFReturnsRepository
from productdto.mutualFundDto import (
    CollectionTileDTO,
    ExplorePageDTO,
    NavChartDTO,
    NavPointDTO,
    ReturnsDTO,
    SchemeDetailDTO,
    SchemeSummaryDTO,
)

logger = logging.getLogger("mutualfunds.service")

_PERIOD_MONTHS = {"1m": 1, "6m": 6, "1y": 12, "3y": 36, "5y": 60, "all": None}
_DEFAULT_PERIOD = "6m"


class MutualFundService:
    """
    Router-facing facade. Search/Explore/Collections are DB-only (never
    call mfapi.in); the single-fund NAV chart is live-first with a DB
    fallback - see get_nav_chart(). All dependencies are constructor-
    injected, no module-level/global/static state.
    """

    def __init__(self, scheme_repository: MFSchemeRepository, nav_history_repository: MFNavHistoryRepository,
                 returns_repository: MFReturnsRepository, curated_picks_repository: MFCuratedPicksRepository,
                 cache: MFInMemoryCache, provider: MFDataProvider, returns_calculator: MFReturnsCalculator,
                 collections_catalog: MFCollectionsCatalog):
        self._scheme_repository = scheme_repository
        self._nav_history_repository = nav_history_repository
        self._returns_repository = returns_repository
        self._curated_picks_repository = curated_picks_repository
        self._cache = cache
        self._provider = provider
        self._returns_calculator = returns_calculator
        self._collections_catalog = collections_catalog
        self._nav_capper = MFNavHistoryCapper()

    async def search(self, query: str | None, category: str | None, fund_house: str | None,
                      page: int, page_size: int) -> list[SchemeSummaryDTO]:
        rows = await self._run_sync(self._scheme_repository.search_schemes, query, category, fund_house, page, page_size)
        return [self._row_to_summary(row) for row in rows]

    async def get_categories(self) -> dict:
        categories = await self._run_sync(self._scheme_repository.list_categories)
        fund_houses = await self._run_sync(self._scheme_repository.list_fund_houses)
        return {"categories": categories, "fund_houses": fund_houses}

    async def get_fund_meta(self, scheme_code: int) -> SchemeDetailDTO | None:
        row = await self._run_sync(self._scheme_repository.get_scheme, scheme_code)
        if row is None:
            return None
        return SchemeDetailDTO(
            scheme_code=row["scheme_code"], scheme_name=row["scheme_name"],
            fund_house=row["fund_house"], scheme_category=row["scheme_category"],
            scheme_type=row["scheme_type"], isin_growth=row["isin_growth"],
            isin_div_reinvestment=row["isin_div_reinvestment"],
        )

    async def get_nav_chart(self, scheme_code: int, period: str = _DEFAULT_PERIOD) -> NavChartDTO:
        period = period.lower()
        if period not in _PERIOD_MONTHS:
            period = _DEFAULT_PERIOD

        points, is_live = await self._get_full_nav_series(scheme_code)
        returns = self._returns_calculator.compute_trailing_returns(points)
        sliced = self._slice_period(points, period)
        return NavChartDTO(scheme_code=scheme_code, period=period, points=sliced, returns=returns, is_live=is_live)

    async def get_explore_page(self) -> ExplorePageDTO:
        popular_rows = await self._run_sync(self._curated_picks_repository.get_picks, "popular")
        if not popular_rows:
            popular_rows = await self._run_sync(self._returns_repository.top_by_category, None, "return_3y", 4, 0)

        collections = [
            CollectionTileDTO(key=d.key, title=d.title, icon_hint=d.icon_hint)
            for d in self._collections_catalog.all()
        ]
        return ExplorePageDTO(
            popular_funds=[self._row_to_summary(row) for row in popular_rows[:4]],
            collections=collections,
        )

    async def get_collection(self, key: str, page: int, page_size: int) -> list[SchemeSummaryDTO]:
        definition = self._collections_catalog.get(key)
        if definition is None:
            return []

        if page == 1:
            curated_rows = await self._run_sync(self._curated_picks_repository.get_picks, key)
            if curated_rows:
                return [self._row_to_summary(row) for row in curated_rows[:page_size]]

        offset = max(page - 1, 0) * page_size
        rows = await self._run_sync(
            self._returns_repository.top_by_category, definition.category_filter,
            definition.sort_by, page_size, offset
        )
        return [self._row_to_summary(row) for row in rows]

    async def _get_full_nav_series(self, scheme_code: int) -> tuple[list[NavPointDTO], bool]:
        cached = await self._cache.get_nav_series(scheme_code)
        if cached is not None:
            return cached, True

        try:
            meta, points = await self._provider.fetch_scheme_with_history(scheme_code)
            await self._cache.set_nav_series(scheme_code, points)
            await self._write_through(scheme_code, meta, points)
            return points, True
        except Exception as ex:
            logger.warning(f"[MutualFundService] live mfapi.in fetch failed for {scheme_code}, falling back to DB: {ex}")
            points = await self._run_sync(self._nav_history_repository.get_series, scheme_code, None)
            return points, False

    async def _write_through(self, scheme_code: int, meta: dict, points: list[NavPointDTO]) -> None:
        """
        Persists the live fetch as the DB fallback. `points` (used for THIS
        request's returns/chart slicing) stays uncapped so "5Y"/"All" views
        are accurate; only what gets written to mf_nav_history is capped via
        MFNavHistoryCapper - the same storage-bound policy MFNavBackfillService
        applies - so live-viewed funds don't silently bypass it and store
        decades of daily rows a chart never needs.
        """
        try:
            # mf_nav_history.scheme_code is FK-constrained to mf_schemes, so the
            # scheme row must exist before any NAV rows can be written - a user
            # can open a fund's live chart before the daily master sync has ever
            # inserted that scheme_code, so this can't be skipped/assumed here.
            await self._run_sync(self._scheme_repository.upsert_schemes,
                                  [{"scheme_code": scheme_code, "scheme_name": meta.get("scheme_name") or str(scheme_code)}])
            await self._run_sync(self._scheme_repository.mark_active_and_backfilled, scheme_code, meta)
            capped_points = self._nav_capper.cap(points)
            await self._run_sync(self._nav_history_repository.bulk_insert, scheme_code, capped_points)
            returns = self._returns_calculator.compute_trailing_returns(points)
            await self._run_sync(self._returns_repository.upsert_returns, scheme_code, returns)
        except Exception as ex:
            # Write-through failing must never break the live response the user is waiting on.
            logger.warning(f"[MutualFundService] write-through failed for {scheme_code}: {ex}")

    def _slice_period(self, points: list[NavPointDTO], period: str) -> list[NavPointDTO]:
        months = _PERIOD_MONTHS[period]
        if months is None or not points:
            return points
        latest_date: date = points[-1].nav_date
        cutoff = latest_date - relativedelta(months=months)
        return [point for point in points if point.nav_date >= cutoff]

    def _row_to_summary(self, row: dict) -> SchemeSummaryDTO:
        return SchemeSummaryDTO(
            scheme_code=row["scheme_code"], scheme_name=row.get("scheme_name"),
            fund_house=row.get("fund_house"), scheme_category=row.get("scheme_category"),
            scheme_type=row.get("scheme_type"), latest_nav=row.get("latest_nav"),
            return_3y=row.get("return_3y"),
        )

    async def _run_sync(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)
