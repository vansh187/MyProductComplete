from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class AssetClassSummaryDTO:
    bucket: str
    net_value: Decimal
    todays_pnl: Decimal
    total_unrealized_pnl: Decimal
    buying_power: Decimal
    last_updated: Optional[str] = None
