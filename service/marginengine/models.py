"""
Data carriers passed between the margin engine's interfaces. Kept as plain
dataclasses with no persistence/network knowledge, per the ports & adapters
design - MarginContext/MarginResult/PriceResolution are the only shapes a
MarginCalculator or ReferencePriceResolver implementation ever needs to know.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass
class PriceResolution:
    """Result of resolving one instrument's reference price through the tiered resolver."""
    price: Decimal
    source: str
    tier: int
    spot: Optional[Decimal] = None


@dataclass
class MarginContext:
    """Everything a MarginCalculator needs to price one order/position, with
    the reference price and every multiplier already resolved by the
    MarginEngine - the calculator itself does no I/O and no config lookups."""
    contract_type: str          # 'OPTION' or 'FUTURES'
    side: str                   # 'BUY' or 'SELL'
    tsym: str
    exchange: str
    lot_size: int
    qty: int
    product_type: str
    reference_price: Decimal
    reference_source: str
    reference_source_tier: int
    notional_pct_or_span_pct: Decimal
    expiry_multiplier: Decimal
    price_source_multiplier: Decimal
    underlying: Optional[str] = None
    strike: Optional[Decimal] = None
    option_type: Optional[str] = None
    expiry: Optional[str] = None
    spot_price: Optional[Decimal] = None
    moneyness_itm_multiplier: Decimal = Decimal("1.2")
    moneyness_atm_multiplier: Decimal = Decimal("1.0")
    moneyness_otm_multiplier: Decimal = Decimal("0.9")
    session_gap_multiplier: Decimal = Decimal("1.0")
    apply_session_gap: bool = False


@dataclass
class MarginResult:
    """Output of MarginCalculator.calculate() - the full breakdown persisted
    onto order_margin_blocks, not just the final number, so a support/ops
    engineer can see exactly how a block was derived."""
    blocked_amount: Decimal
    notional_component: Decimal
    reference_price: Decimal
    reference_source: str
    reference_source_tier: int
    notional_pct_or_span_pct_used: Decimal
    expiry_multiplier_used: Decimal
    price_source_multiplier_used: Decimal
    premium_component: Optional[Decimal] = None
    moneyness_multiplier_used: Optional[Decimal] = None


@dataclass
class MarginCheckResult:
    """Outcome of MarginEngine.check_and_block() - what api/orders.py maps
    onto the JSON response's `margin` block or an error response."""
    approved: bool
    required_margin: Decimal
    available_balance: Decimal
    shortfall: Decimal
    order_margin_block_id: Optional[int] = None
    margin_result: Optional[MarginResult] = None
