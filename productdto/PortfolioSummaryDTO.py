from dataclasses import dataclass
from decimal import Decimal
@dataclass
class PortfolioSummary:
    total_holdings: int = 0
    total_invested: Decimal = 0.0
    current_value: Decimal = 0.0
    total_pnl: Decimal = 0.0
    pnl_percentage: Decimal = 0.0