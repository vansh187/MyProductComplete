from dataclasses import dataclass


@dataclass
class HoldingDTO:
    symbol: str
    quantity: int
    avg_price: float
    current_price: float
    pnl: float