from dataclasses import dataclass

@dataclass
class TradesSummaryDTO:
    total_trades: int = 0
    buy_trades: int = 0
    sell_trades: int = 0