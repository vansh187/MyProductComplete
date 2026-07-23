from typing import Optional
from decimal import Decimal
from datetime import datetime
from dataclasses import dataclass

@dataclass
class MatchingOrderDTO:
    trade_id: Optional[int] = None
    symbol: str = ""
    buy_order_id: int = 0
    sell_order_id: int = 0
    buy_user_id: int = 0
    sell_user_id: int = 0
    quantity: int = 0
    execution_price: Decimal = Decimal("0")
    trade_value: Decimal = Decimal("0")
    executed_at: datetime = None
    remaining_qty:int=0
    counterparty_product_type: Optional[str] = None