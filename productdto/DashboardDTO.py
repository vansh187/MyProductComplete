from dataclasses import dataclass
from typing import Optional
from productdto import OrdersSummaryDTO 
from productdto.PortfolioSummaryDTO import PortfolioSummaryDTO 
from productdto.TradesSummaryDTO import  TradesSummaryDTO

@dataclass
class DashboardDTO:
    user_id: int
    orders: OrdersSummaryDTO
    trades: Optional[TradesSummaryDTO] = None
    portfolio: Optional[PortfolioSummaryDTO] = None
    last_updated: Optional[str] = None
    