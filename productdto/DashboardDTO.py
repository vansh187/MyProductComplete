from dataclasses import dataclass
from typing import Optional
from productdto.OrdersSummaryDTO import OrdersSummary
from productdto.PortfolioSummaryDTO import PortfolioSummary
from productdto.TradesSummaryDTO import TradesSummaryDTO

@dataclass
class DashboardDTO:
    orders: OrdersSummary
    trades: Optional[TradesSummaryDTO] = None
    portfolio: Optional[PortfolioSummary] = None
    last_updated: Optional[str] = None
    