from dataclasses import dataclass
@dataclass
class PortfolioSummary:
    total_holdings: int = 0
    total_invested: float = 0.0
    current_value: float = 0.0
    total_pnl: float = 0.0
    pnl_percentage: float = 0.0