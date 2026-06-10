from dataclasses import dataclass
from typing import List
from productdto.holdingDTO import HoldingDTO
@dataclass
class PortfolioDTO:
    user_id: int
    total_pnl: float
    holdings: List[HoldingDTO]