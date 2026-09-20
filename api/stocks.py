"""
Public (no-auth) market-data endpoints backing the frontend's "Explore
Stocks" feature - see STOCKS_API_REQUIREMENTS.md. Same access convention as
/api/mutual-funds/*: reads are public, only trading actions elsewhere in the
app require the JWT bearer auth.

Every handler here is wrapped so a symbol-master miss is a clean 404, a
Shoonya outage is a clean 503, and any other failure still comes back as
`{"detail": "..."}` (this API's established error shape) rather than an
unhandled exception reaching the client as an opaque 500.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from api.stockModels import (
    ExploreResponse,
    FacetsResponse,
    ChartResponse,
    StockQuoteResponse,
    StockSummaryResponse,
)
from service.stocksService.exceptions import MarketDataUnavailableError
from service.stocksService.StocksService import StocksService

router = APIRouter(prefix="/api/stocks", tags=["Stocks"])

logger = logging.getLogger("api.stocks")

_VALID_CHART_PERIODS = {"1d", "1w", "1m", "6m", "1y", "5y"}


def _get_service(request: Request) -> StocksService:
    service = getattr(request.app.state, "stocks_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Stocks module is not initialized.")
    return service


def _get_shoonya(request: Request):
    return getattr(request.app.state, "shoonya", None)


def _get_stock_feed(request: Request):
    return getattr(request.app.state, "stock_feed", None)


@router.get("/explore", response_model=ExploreResponse)
async def get_explore(request: Request):
    service = _get_service(request)
    try:
        page = await service.get_explore(_get_shoonya(request))
    except Exception as exc:
        logger.error(f"[api.stocks] /explore failed: {exc}")
        raise HTTPException(status_code=503, detail="Explore data is temporarily unavailable.")
    return ExploreResponse(**page)


@router.get("/facets", response_model=FacetsResponse)
async def get_facets(request: Request):
    service = _get_service(request)
    try:
        facets = await service.get_facets()
    except Exception as exc:
        logger.error(f"[api.stocks] /facets failed: {exc}")
        raise HTTPException(status_code=503, detail="Facets are temporarily unavailable.")
    return FacetsResponse(**facets)


@router.get("/search", response_model=list[StockSummaryResponse])
async def search_stocks(
    request: Request,
    q: str | None = None,
    exchange: str | None = None,
    sector: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    service = _get_service(request)
    try:
        results = await service.search(q, exchange, sector, page, page_size, _get_stock_feed(request))
    except Exception as exc:
        logger.error(f"[api.stocks] /search failed: {exc}")
        raise HTTPException(status_code=503, detail="Stock search is temporarily unavailable.")
    return [StockSummaryResponse(**r) for r in results]


@router.get("/{exchange}/{symbol}/quote", response_model=StockQuoteResponse)
async def get_stock_quote(request: Request, exchange: str, symbol: str):
    service = _get_service(request)
    try:
        quote = await service.get_quote(_get_shoonya(request), _get_stock_feed(request), exchange, symbol)
    except MarketDataUnavailableError:
        raise HTTPException(status_code=503, detail="Live quote data is temporarily unavailable.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[api.stocks] quote failed for {exchange}/{symbol}: {exc}")
        raise HTTPException(status_code=503, detail="Live quote data is temporarily unavailable.")

    if quote is None:
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found on {exchange}.")
    return StockQuoteResponse(**quote)


@router.get("/{exchange}/{symbol}/chart", response_model=ChartResponse)
async def get_stock_chart(
    request: Request,
    exchange: str,
    symbol: str,
    period: str = Query("1d"),
):
    service = _get_service(request)
    normalized_period = period.lower() if period.lower() in _VALID_CHART_PERIODS else "1d"
    try:
        chart = await service.get_chart(_get_shoonya(request), exchange, symbol, normalized_period)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[api.stocks] chart failed for {exchange}/{symbol}: {exc}")
        raise HTTPException(status_code=503, detail="Chart data is temporarily unavailable.")

    if chart is None:
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found on {exchange}.")
    return ChartResponse(**chart)
