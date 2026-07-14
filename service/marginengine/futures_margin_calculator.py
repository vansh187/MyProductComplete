"""
Futures margin formula (both BUY and SELL - futures are symmetric leveraged
risk in both directions, unlike options):

    required_margin = mark_price * lot_size * qty * span_pct
                       * expiry_multiplier * price_source_multiplier

See FnO_Margin_Engine_Design.md section 3.2. No premium component - futures
carry no upfront premium, so margin is purely exposure-based on notional at
the resolved mark price.
"""

import logging
from decimal import Decimal

from service.marginengine.exceptions import MarginEngineError
from service.marginengine.interfaces import MarginCalculator
from service.marginengine.models import MarginContext, MarginResult

logger = logging.getLogger(__name__)


class FuturesMarginCalculator(MarginCalculator):
    """Prices BUY-to-open and SELL-to-open futures orders/positions."""

    def __init__(self):
        self.logger = logger

    def calculate(self, context: MarginContext) -> MarginResult:
        try:
            if context is None:
                raise ValueError("MarginContext cannot be None")
            if context.contract_type != "FUTURES":
                raise ValueError(f"FuturesMarginCalculator cannot handle contract_type={context.contract_type}")
            if context.side not in ("BUY", "SELL"):
                raise ValueError(f"Invalid side: {context.side}")
            if context.lot_size is None or context.lot_size <= 0:
                raise ValueError("lot_size must be a positive integer")
            if context.qty is None or context.qty <= 0:
                raise ValueError("qty must be a positive integer")
            if context.reference_price is None or context.reference_price <= 0:
                raise ValueError("reference_price (mark price) must be a positive number")

            lot_qty = Decimal(context.lot_size) * Decimal(context.qty)
            notional_component = (
                context.reference_price * lot_qty * context.notional_pct_or_span_pct
                * context.expiry_multiplier * context.price_source_multiplier
            )
            if context.apply_session_gap:
                notional_component *= context.session_gap_multiplier

            return MarginResult(
                blocked_amount=notional_component,
                premium_component=None,
                notional_component=notional_component,
                reference_price=context.reference_price,
                reference_source=context.reference_source,
                reference_source_tier=context.reference_source_tier,
                notional_pct_or_span_pct_used=context.notional_pct_or_span_pct,
                expiry_multiplier_used=context.expiry_multiplier,
                moneyness_multiplier_used=None,
                price_source_multiplier_used=context.price_source_multiplier,
            )
        except Exception as ex:
            self.logger.error(f"Futures margin calculation failed for {getattr(context, 'tsym', '?')}: {str(ex)}")
            raise MarginEngineError(f"Futures margin calculation failed: {str(ex)}") from ex
