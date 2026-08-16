from fastapi import APIRouter, HTTPException, Request

from api.mutualFundModels import (
    CategoriesResponse,
    ExplorePageResponse,
    NavChartResponse,
    SchemeDetailResponse,
    SchemeSummaryResponse,
)
from mutualfunds.service import MutualFundService

router = APIRouter(prefix="/api/mutual-funds", tags=["Mutual Funds"])


def _get_service(request: Request) -> MutualFundService:
    service = getattr(request.app.state, "mutual_fund_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Mutual funds module is not initialized.")
    return service


@router.get("/search", response_model=list[SchemeSummaryResponse])
async def search_schemes(request: Request, q: str | None = None, category: str | None = None,
                          fund_house: str | None = None, page: int = 1, page_size: int = 20):
    service = _get_service(request)
    results = await service.search(q, category, fund_house, page, page_size)
    return [SchemeSummaryResponse(**vars(r)) for r in results]


@router.get("/categories", response_model=CategoriesResponse)
async def get_categories(request: Request):
    service = _get_service(request)
    data = await service.get_categories()
    return CategoriesResponse(**data)


@router.get("/explore", response_model=ExplorePageResponse)
async def get_explore_page(request: Request):
    service = _get_service(request)
    page = await service.get_explore_page()
    return ExplorePageResponse(
        popular_funds=[SchemeSummaryResponse(**vars(f)) for f in page.popular_funds],
        collections=[c.__dict__ for c in page.collections],
    )


@router.get("/collections/{key}", response_model=list[SchemeSummaryResponse])
async def get_collection(request: Request, key: str, page: int = 1, page_size: int = 20):
    service = _get_service(request)
    results = await service.get_collection(key, page, page_size)
    return [SchemeSummaryResponse(**vars(r)) for r in results]


@router.get("/{scheme_code}", response_model=SchemeDetailResponse)
async def get_scheme_detail(request: Request, scheme_code: int):
    service = _get_service(request)
    detail = await service.get_fund_meta(scheme_code)
    if detail is None:
        raise HTTPException(status_code=404, detail="Scheme not found.")
    detail_fields = vars(detail)
    detail_fields["holdings"] = "unavailable"
    return SchemeDetailResponse(**detail_fields)


@router.get("/{scheme_code}/nav-chart", response_model=NavChartResponse)
async def get_nav_chart(request: Request, scheme_code: int, period: str = "6m"):
    service = _get_service(request)
    chart = await service.get_nav_chart(scheme_code, period)
    return NavChartResponse(
        scheme_code=chart.scheme_code,
        period=chart.period,
        points=[{"nav_date": p.nav_date, "nav": p.nav} for p in chart.points],
        returns=vars(chart.returns),
        is_live=chart.is_live,
    )
