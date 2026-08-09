"""
Pydantic response models for the Mutual Funds API (api/mutualFunds.py).
Kept in a dedicated module, same convention as api/models.py being split
out from orders.py, to avoid growing that already-large shared file.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel


class SchemeSummaryResponse(BaseModel):
    scheme_code: int
    scheme_name: Optional[str] = None
    fund_house: Optional[str] = None
    scheme_category: Optional[str] = None
    scheme_type: Optional[str] = None
    latest_nav: Optional[float] = None
    return_3y: Optional[float] = None


class SchemeDetailResponse(BaseModel):
    scheme_code: int
    scheme_name: str
    fund_house: Optional[str] = None
    scheme_category: Optional[str] = None
    scheme_type: Optional[str] = None
    isin_growth: Optional[str] = None
    isin_div_reinvestment: Optional[str] = None
    min_sip_amount: Optional[float] = None
    fund_size_aum: Optional[float] = None
    expense_ratio: Optional[float] = None
    rating: Optional[float] = None
    holdings: Optional[str] = "unavailable"


class NavPointResponse(BaseModel):
    nav_date: date
    nav: float


class ReturnsResponse(BaseModel):
    return_1m: Optional[float] = None
    return_6m: Optional[float] = None
    return_1y: Optional[float] = None
    return_3y: Optional[float] = None
    return_5y: Optional[float] = None
    day_change_pct: Optional[float] = None
    latest_nav: Optional[float] = None


class NavChartResponse(BaseModel):
    scheme_code: int
    period: str
    points: list[NavPointResponse]
    returns: ReturnsResponse
    is_live: bool


class CategoriesResponse(BaseModel):
    categories: list[str]
    fund_houses: list[str]


class CollectionTileResponse(BaseModel):
    key: str
    title: str
    icon_hint: str


class ExplorePageResponse(BaseModel):
    popular_funds: list[SchemeSummaryResponse]
    collections: list[CollectionTileResponse]
