"""
TieredPriceResolver - the only ReferencePriceResolver implementation today.
Walks a confidence-ordered set of price sources per FnO_Margin_Engine_Design.md
section 4, and is itself the seam a real broker feed / real-broker SPAN API
slots into later as a higher-confidence tier without touching MarginEngine
or any MarginCalculator.

Tier 1 (live broker feed tick) has no backing store in this codebase today -
ticks are only cached per open *position* (database/positionCache.py), not
per instrument independent of any position, so there is nothing to read for
a fresh opening order. This is intentionally left as a documented gap and
the natural first target once a real broker margin/tick integration lands
(see BrokerMarginProvider) - implemented as a best-effort no-op rather than
skipped entirely so wiring a real feed later is a one-method change here.

Tier 3's "verified last internal trade" is checked against a sanity
baseline (the option's strike, or the client's own order price for
futures) rather than blindly trusted - this exists specifically to close
the manipulation gap flagged in review: two colluding accounts trading an
illiquid contract at an off-market price must not be able to move a third
party's required margin.
"""

import logging
from decimal import Decimal
from typing import Optional

from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
from service.marginengine.exceptions import ReferencePriceUnresolvedError
from service.marginengine.interfaces import ReferencePriceResolver
from service.marginengine.models import PriceResolution

logger = logging.getLogger(__name__)


class TieredPriceResolver(ReferencePriceResolver):
    """Resolves a trustworthy reference price for an OPTION or FUTURES instrument."""

    def __init__(self):
        self.logger = logger

    def resolve(self, tsym: str, contract_type: str, underlying: Optional[str],
                exchange: str, token: Optional[str],
                order_price=None, strike=None, option_type=None, expiry=None,
                verification_band_pct=Decimal("0.20")) -> PriceResolution:
        if not tsym or not tsym.strip():
            raise ValueError("tsym cannot be empty")
        if contract_type not in ("OPTION", "FUTURES"):
            raise ValueError(f"Invalid contract_type: {contract_type}")

        attempted = []

        # Tier 1: live broker feed tick - no per-instrument tick store exists
        # yet independent of an open position (see module docstring).
        attempted.append({"tier": 1, "source": "BROKER_TICK", "result": "NO_RECENT_TICK"})

        # Tier 2: cached option chain (options only today).
        if contract_type == "OPTION":
            tier2 = self._resolve_from_cached_chain(underlying, expiry, strike, option_type)
            if tier2 is not None:
                return tier2
            attempted.append({"tier": 2, "source": "CACHED_CHAIN", "result": "STALE_OR_MISSING"})
        else:
            attempted.append({"tier": 2, "source": "CACHED_CHAIN", "result": "NOT_AVAILABLE_FOR_FUTURES"})

        # Tier 3: verified last internal trade.
        baseline = strike if contract_type == "OPTION" else order_price
        tier3 = self._resolve_from_verified_internal_trade(tsym, baseline, verification_band_pct)
        if tier3 is not None:
            return tier3
        attempted.append({"tier": 3, "source": "VERIFIED_INTERNAL", "result": "OUTSIDE_VERIFICATION_BAND_OR_MISSING"})

        # Tier 4: client's own submitted order price as a last-resort proxy,
        # for both instrument classes. This field feeds OptionsMarginCalculator's
        # premium_component directly (premium_per_unit * lot_size * qty) -
        # it must be a genuine premium signal, never the strike. Strike is a
        # separate, deliberately-independent fallback the calculator already
        # uses for the *notional* reference price only (spot-or-strike, see
        # OptionsMarginCalculator.calculate()) - conflating the two here by
        # returning strike as this field previously meant a real ~Rs120
        # option premium got treated as a ~Rs23,700 "premium", overstating
        # required margin by roughly the strike/premium ratio (a bug found
        # via a live production order, not a design choice).
        if order_price is not None and order_price > 0:
            return PriceResolution(price=Decimal(str(order_price)), source="ORDER_PRICE_PROXY", tier=4)
        attempted.append({"tier": 4, "source": "ORDER_PRICE_PROXY", "result": "UNAVAILABLE"})

        # Tier 5: terminal - nothing resolved.
        self.logger.error(f"Reference price unresolved for {tsym}: {attempted}")
        raise ReferencePriceUnresolvedError(tsym, attempted)

    def _resolve_from_cached_chain(self, underlying, expiry, strike, option_type) -> Optional[PriceResolution]:
        if not underlying or strike is None or not option_type:
            return None
        try:
            from service.optionChain.OptionChainService import OptionChainService
            chain, resolved_expiry = OptionChainService().peek_cached_chain(underlying, expiry)
            if chain is None:
                return None

            spot = chain.get("spot")
            spot_decimal = Decimal(str(spot)) if spot is not None else None
            leg_key = option_type.lower()

            for entry in chain.get("strikes", []):
                if entry.get("strike") is None:
                    continue
                if Decimal(str(entry["strike"])) != Decimal(str(strike)):
                    continue
                leg_data = entry.get(leg_key)
                if leg_data and leg_data.get("ltp") is not None:
                    return PriceResolution(
                        price=Decimal(str(leg_data["ltp"])),
                        source="CACHED_CHAIN",
                        tier=2,
                        spot=spot_decimal,
                    )
                break
            return None
        except Exception as ex:
            self.logger.warning(f"Tier 2 cached-chain lookup failed for {underlying}/{strike}/{option_type}: {str(ex)}")
            return None

    def _resolve_from_verified_internal_trade(self, tsym, baseline, verification_band_pct) -> Optional[PriceResolution]:
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('margin.yaml', 'get_last_trade_price_for_symbol'),
                (tsym,)
            )
            row = cursor.fetchone()
            if row is None or row[0] is None:
                return None

            last_trade_price = Decimal(str(row[0]))

            if baseline is None or baseline <= 0:
                # No independent sanity baseline available - too risky to
                # trust an unverified internal trade at full Tier 3
                # confidence, so this falls through to Tier 4 instead.
                return None

            baseline_decimal = Decimal(str(baseline))
            deviation_pct = abs(last_trade_price - baseline_decimal) / baseline_decimal
            if deviation_pct > verification_band_pct:
                self.logger.warning(
                    f"Tier 3 internal trade price for {tsym} rejected: "
                    f"{last_trade_price} deviates {deviation_pct:.2%} from baseline {baseline_decimal}"
                )
                return None

            return PriceResolution(price=last_trade_price, source="VERIFIED_INTERNAL", tier=3)
        except Exception as ex:
            self.logger.warning(f"Tier 3 verified-internal-trade lookup failed for {tsym}: {str(ex)}")
            return None
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
