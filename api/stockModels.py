"""
Pydantic response models for the Stocks API (api/stocks.py, api/search.py).
Dedicated module, same convention as api/mutualFundModels.py being split out
from api/mutualFunds.py.
"""

from typing import Optional

from pydantic import BaseModel


class StockSummaryResponse(BaseModel):
    symbol: str
    exchange: str
    name: str
    ltp: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    volume: int = 0
    sector: Optional[str] = None


class CollectionTileResponse(BaseModel):
    key: str
    title: str
    icon_hint: str


class ExploreResponse(BaseModel):
    market_status: str
    trending: list[StockSummaryResponse]
    top_gainers: list[StockSummaryResponse]
    top_losers: list[StockSummaryResponse]
    most_active: list[StockSummaryResponse]
    collections: list[CollectionTileResponse]


class FacetsResponse(BaseModel):
    exchanges: list[str]
    sectors: list[str]


class DepthLevelResponse(BaseModel):
    price: float = 0.0
    qty: int = 0
    orders: int = 0


class DepthResponse(BaseModel):
    bids: list[DepthLevelResponse]
    asks: list[DepthLevelResponse]


class StockQuoteResponse(BaseModel):
    symbol: str
    exchange: str
    name: str
    ltp: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    avg_price: float = 0.0
    upper_circuit: float = 0.0
    lower_circuit: float = 0.0
    week_52_high: float = 0.0
    week_52_low: float = 0.0
    market_cap: float = 0.0
    pe_ratio: float = 0.0
    depth: DepthResponse
    is_market_open: bool
    last_updated: str


class CandleResponse(BaseModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


class ChartResponse(BaseModel):
    symbol: str
    period: str
    candles: list[CandleResponse]


class MutualFundSearchHitResponse(BaseModel):
    scheme_code: int
    scheme_name: Optional[str] = None
    fund_house: Optional[str] = None
    latest_nav: Optional[float] = None


class CombinedSearchResponse(BaseModel):
    stocks: list[StockSummaryResponse]
    mutual_funds: list[MutualFundSearchHitResponse]
