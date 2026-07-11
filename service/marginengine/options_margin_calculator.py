"""
Options margin formula (SELL/write only):

    required_margin = (premium_per_unit * lot_size * qty)
                     + (notional_pct * notional_reference_price * lot_size * qty
                        * expiry_multiplier * moneyness_multiplier * price_source_multiplier)

See FnO_Margin_Engine_Design.md section 3.1. `premium_per_unit` is
context.reference_price (the option's own resolved price); the notional
component's reference price is the resolved underlying spot when available,
falling back to the contract's own strike (Tier 4 proxy) when it isn't.
"""

import logging
from decimal import Decimal

from service.marginengine.exceptions import MarginEngineError
from service.marginengine.interfaces import MarginCalculator
from service.marginengine.models import MarginContext, MarginResult

logger = logging.getLogger(__name__)


class OptionsMarginCalculator(MarginCalculator):
    """Prices SELL-to-open option orders/positions."""

    def __init__(self):
        self.logger = logger

    def calculate(self, context: MarginContext) -> MarginResult:
        try:
            if context is None:
                raise ValueError("MarginContext cannot be None")
            if context.contract_type != "OPTION":
                raise ValueError(f"OptionsMarginCalculator cannot handle contract_type={context.contract_type}")
            if context.side != "SELL":
                raise ValueError("Options margin is only required for SELL (write) orders")
            if context.lot_size is None or context.lot_size <= 0:
                raise ValueError("lot_size must be a positive integer")
            if context.qty is None or context.qty <= 0:
                raise ValueError("qty must be a positive integer")
            if context.reference_price is None or context.reference_price < 0:
                raise ValueError("reference_price must be a non-negative number")

            lot_qty = Decimal(context.lot_size) * Decimal(context.qty)
            premium_component = context.reference_price * lot_qty

            notional_reference_price = context.spot_price if context.spot_price is not None else context.strike
            if notional_reference_price is None:
                raise ValueError("Neither spot_price nor strike is available for notional margin calculation")

            moneyness_multiplier = self._resolve_moneyness_multiplier(context, notional_reference_price)

            notional_component = (
                context.notional_pct_or_span_pct * notional_reference_price * lot_qty
                * context.expiry_multiplier * moneyness_multiplier * context.price_source_multiplier
            )
            if context.apply_session_gap:
                notional_component *= context.session_gap_multiplier

            blocked_amount = premium_component + notional_component

            return MarginResult(
                blocked_amount=blocked_amount,
                premium_component=premium_component,
                notional_component=notional_component,
                reference_price=context.reference_price,
                reference_source=context.reference_source,
                reference_source_tier=context.reference_source_tier,
                notional_pct_or_span_pct_used=context.notional_pct_or_span_pct,
                expiry_multiplier_used=context.expiry_multiplier,
                moneyness_multiplier_used=moneyness_multiplier,
                price_source_multiplier_used=context.price_source_multiplier,
            )
        except Exception as ex:
            self.logger.error(f"Options margin calculation failed for {getattr(context, 'tsym', '?')}: {str(ex)}")
            raise MarginEngineError(f"Options margin calculation failed: {str(ex)}") from ex

    def _resolve_moneyness_multiplier(self, context: MarginContext, spot: Decimal) -> Decimal:
        """Buckets abs(strike - spot) / spot into ITM/ATM/OTM bands. A short
        deep-ITM option carries real assignment risk a flat expiry-only
        multiplier would miss - see design review finding in section 3.1."""
        if context.strike is None or spot is None or spot == 0:
            return context.moneyness_atm_multiplier

        moneyness_pct = abs(context.strike - spot) / spot
        is_itm = (
            (context.option_type == "CE" and spot > context.strike)
            or (context.option_type == "PE" and spot < context.strike)
        )

        if moneyness_pct <= Decimal("0.01"):
            return context.moneyness_atm_multiplier
        if is_itm:
            return context.moneyness_itm_multiplier
        return context.moneyness_otm_multiplier
