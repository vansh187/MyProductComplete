from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class SchemeSummaryDTO:
    scheme_code: int
    scheme_name: str
    fund_house: Optional[str]
    scheme_category: Optional[str]
    scheme_type: Optional[str]
    latest_nav: Optional[float] = None
    return_3y: Optional[float] = None


@dataclass
class SchemeDetailDTO:
    scheme_code: int
    scheme_name: str
    fund_house: Optional[str]
    scheme_category: Optional[str]
    scheme_type: Optional[str]
    isin_growth: Optional[str]
    isin_div_reinvestment: Optional[str]
    min_sip_amount: Optional[float] = None      # not provided by mfapi.in - always None in v1
    fund_size_aum: Optional[float] = None        # not provided by mfapi.in - always None in v1
    expense_ratio: Optional[float] = None         # not provided by mfapi.in - always None in v1
    rating: Optional[float] = None                # not provided by mfapi.in - always None in v1
    holdings: Optional[list] = None               # not provided by mfapi.in - always None in v1


@dataclass
class NavPointDTO:
    nav_date: date
    nav: float


@dataclass
class ReturnsDTO:
    return_1m: Optional[float] = None
    return_6m: Optional[float] = None
    return_1y: Optional[float] = None
    return_3y: Optional[float] = None
    return_5y: Optional[float] = None
    day_change_pct: Optional[float] = None
    latest_nav: Optional[float] = None


@dataclass
class NavChartDTO:
    scheme_code: int
    period: str
    points: list[NavPointDTO]
    returns: ReturnsDTO
    is_live: bool


@dataclass
class CollectionTileDTO:
    key: str
    title: str
    icon_hint: str


@dataclass
class CuratedPickDTO:
    scheme_code: int
    rank: int
    blurb: Optional[str] = None
    curated_by: str = "fallback_ranked"


@dataclass
class ExplorePageDTO:
    popular_funds: list[SchemeSummaryDTO] = field(default_factory=list)
    collections: list[CollectionTileDTO] = field(default_factory=list)
