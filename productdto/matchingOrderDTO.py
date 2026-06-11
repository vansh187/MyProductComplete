from decimal import Decimal
from datetime import datetime
from dataclasses import dataclass

@dataclass
class MatchingOrderDTO:
    trade_id: int
    symbol: str
    buy_order_id: int
    sell_order_id: int
    buy_user_id: int
    sell_user_id: int
    quantity: int
    execution_price: Decimal   
    trade_value: Decimal       
    executed_at: datetime