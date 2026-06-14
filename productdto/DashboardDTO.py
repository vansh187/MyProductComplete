from dataclasses import dataclass
from typing import Optional
from productdto import OrdersSummaryDTO 
from productdto.portfolioDTO import PortfolioDTO 
from productdto.TradesSummaryDTO import  TradesSummaryDTO

@dataclass
class DashboardDTO:
    user_id: int
    orders: OrdersSummaryDTO
    trades: Optional[TradesSummaryDTO] = None
    portfolio: Optional[PortfolioDTO] = None
    last_updated: Optional[str] = None
    