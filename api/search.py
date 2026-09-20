"""
Combined navbar search - GET /api/search?q=&limit= - fans out to the
existing mutual-fund search and the new stock search concurrently and
merges the two into one response, per STOCKS_API_REQUIREMENTS.md section 3.
No new ranking logic: each side keeps its own existing relevance order.

Both branches are isolated with return_exceptions=True - a mutual-fund DB
outage must not blank out stock results and vice versa; either side simply
comes back as an empty list rather than failing the whole request.
"""

import asyncio
import logging

from fastapi import APIRouter, Query, Request

from api.stockModels import CombinedSearchResponse, MutualFundSearchHitResponse, StockSummaryResponse
from mutualfunds.service import MutualFundService
from service.stocksService.StocksService import StocksService

router = APIRouter(prefix="/api/search", tags=["Search"])

logger = logging.getLogger("api.search")

_DEFAULT_LIMIT = 6
_MAX_LIMIT = 25


def _get_stocks_service(request: Request) -> StocksService | None:
    return getattr(request.app.state, "stocks_service", None)


def _get_mutual_fund_service(request: Request) -> MutualFundService | None:
    return getattr(request.app.state, "mutual_fund_service", None)


@router.get("", response_model=CombinedSearchResponse)
async def combined_search(
    request: Request,
    q: str | None = None,
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
):
    if not q or not q.strip():
        return CombinedSearchResponse(stocks=[], mutual_funds=[])

    stocks_service = _get_stocks_service(request)
    mutual_fund_service = _get_mutual_fund_service(request)

    async def _search_stocks() -> list[dict]:
        if stocks_service is None:
            return []
        stock_feed = getattr(request.app.state, "stock_feed", None)
        return await stocks_service.search(q, None, None, 1, limit, stock_feed)

    async def _search_mutual_funds() -> list:
        if mutual_fund_service is None:
            return []
        return await mutual_fund_service.search(q, None, None, 1, limit)

    results = await asyncio.gather(_search_stocks(), _search_mutual_funds(), return_exceptions=True)

    stock_hits = results[0] if not isinstance(results[0], Exception) else []
    if isinstance(results[0], Exception):
        logger.warning(f"[api.search] stock search failed: {results[0]}")

    fund_hits = results[1] if not isinstance(results[1], Exception) else []
    if isinstance(results[1], Exception):
        logger.warning(f"[api.search] mutual fund search failed: {results[1]}")

    return CombinedSearchResponse(
        stocks=[StockSummaryResponse(**hit) for hit in stock_hits],
        mutual_funds=[
            MutualFundSearchHitResponse(
                scheme_code=hit.scheme_code,
                scheme_name=hit.scheme_name,
                fund_house=hit.fund_house,
                latest_nav=hit.latest_nav,
            )
            for hit in fund_hits
        ],
    )
