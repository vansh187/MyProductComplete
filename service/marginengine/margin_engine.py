"""
MarginEngine - the single facade the rest of the codebase imports from this
subsystem (api/orders.py, service/orderService.py, service/executionEngine.py,
service/positionService.py never talk to a concrete calculator, resolver,
ledger or config table directly). See FnO_Margin_Engine_Design.md sections 2
and 5 for the full architecture and the critical fixes this class implements:

  - Fix #1: closing trades (F&O SELL closing a long, F&O BUY closing a
    short) never require fresh notional cash - only the excess "opening"
    quantity beyond what closes the existing position is priced/blocked.
  - Fix #2: reconcile_on_fill() is the only margin-mutating call wired to a
    fill - it releases the order-level block once, then sets the position's
    blocked_margin to a freshly computed absolute value (never "adds" to it),
    so a multi-fill order can never be double-blocked.
  - Fix #3: MARKET orders never leave premium/mark price undefined - the
    calculators never read order.price, only the resolved reference price.
  - Fix #4: lock ordering (position lock before wallet lock) is asserted via
    LockOrderGuard at every entry point that touches both.
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from appconfig import FutureMaster, OptionMaster
from database.PostgresConnectionFactory import PostgresConnectionFactory
from database.marginenginepersistence.margin_config_persistence import MarginConfigPersistence
from database.marginenginepersistence.margin_wallet_persistence import MarginWalletPersistence
from database.positionCache import PositionCache
from utils.instrumentClassifier import is_fo_exchange, looks_like_future_symbol
from service.marginengine.exceptions import InsufficientMarginError, MarginEngineError
from service.marginengine.futures_margin_calculator import FuturesMarginCalculator
from service.marginengine.interfaces import BrokerMarginProvider, MarginLedgerRepository, PositionReader, ReferencePriceResolver
from service.marginengine.lock_order_guard import LockOrderGuard
from service.marginengine.models import MarginCheckResult, MarginContext
from service.marginengine.null_broker_margin_provider import NullBrokerMarginProvider
from service.marginengine.options_margin_calculator import OptionsMarginCalculator
from service.marginengine.position_reader_adapter import PositionServicePositionReader
from service.marginengine.postgres_margin_ledger_repository import PostgresMarginLedgerRepository
from service.marginengine.tiered_price_resolver import TieredPriceResolver

logger = logging.getLogger(__name__)

_DEFAULT_NOTIONAL_PCT = Decimal("0.15")
_DEFAULT_SPAN_PCT = Decimal("0.12")


class MarginEngine:
    """Facade over the margin calculators, price resolver, ledger repository
    and broker provider. Every dependency is injectable (constructor
    defaults to today's real implementations) so tests can substitute fakes
    without touching this class."""

    def __init__(self,
                 price_resolver: Optional[ReferencePriceResolver] = None,
                 ledger_repository: Optional[MarginLedgerRepository] = None,
                 broker_provider: Optional[BrokerMarginProvider] = None,
                 position_reader: Optional[PositionReader] = None):
        self.calculators = {
            "OPTION": OptionsMarginCalculator(),
            "FUTURES": FuturesMarginCalculator(),
        }
        self.price_resolver = price_resolver or TieredPriceResolver()
        self.ledger_repository = ledger_repository or PostgresMarginLedgerRepository()
        self.broker_provider = broker_provider or NullBrokerMarginProvider()
        self.position_reader = position_reader or PositionServicePositionReader()
        self.config_persistence = MarginConfigPersistence()
        self.position_cache = PositionCache()
        self.logger = logger

    # -- public facade -----------------------------------------------------

    def is_margin_required(self, exchange: str, side: str, contract_type: Optional[str]) -> bool:
        """True if this order needs a margin check at all: any F&O SELL
        (options, per Options being SELL/write-only above) or any F&O
        FUTURES order regardless of side (futures are symmetric risk).
        contract_type=None (not recognized as either OPTION or FUTURES)
        always means "no margin engine involvement" regardless of exchange -
        callers still route it through the pre-existing equity paths."""
        try:
            if contract_type not in ("OPTION", "FUTURES"):
                return False
            if not is_fo_exchange(exchange):
                return False
            return contract_type == "FUTURES" or side == "SELL"
        except Exception as ex:
            self.logger.error(f"is_margin_required() failed for exchange={exchange}, side={side}: {str(ex)}")
            raise MarginEngineError(f"is_margin_required() failed: {str(ex)}") from ex

    def resolve_contract_type(self, tsym: str, exchange: str, fallback_lot_size: Optional[int] = None) -> dict:
        """Looks up instrument details for a tsym. OPTION when OptionMaster
        recognizes it (authoritative - token/exchange/strike/expiry all come
        from the scrip master); FUTURES when either FutureMaster recognizes
        it (authoritative - token/expiry/lot_size from the scrip master, see
        appconfig/FutureMaster.py) or, when the master doesn't have it
        (stale/missing), the symbol matches the futures trading-symbol
        convention AND the caller-supplied exchange is already F&O (see
        utils/instrumentClassifier.looks_like_future_symbol - kept as a
        fallback for resilience, not the primary source of truth anymore);
        otherwise contract_type is None, meaning "not this engine's concern"
        (equity, or an unrecognized instrument) - callers must fall back to
        their existing equity-path logic in that case, never guess."""
        try:
            contract = OptionMaster.find_by_tsym(tsym)
            if contract:
                return {
                    "contract_type": "OPTION",
                    "underlying": contract["underlying"],
                    "strike": Decimal(str(contract["strike"])),
                    "option_type": contract["option_type"],
                    "expiry": contract["expiry"],
                    "lot_size": contract["lot_size"],
                    "token": contract["token"],
                }
            future_contract = FutureMaster.find_by_tsym(tsym)
            if future_contract:
                return {
                    "contract_type": "FUTURES",
                    "underlying": future_contract["underlying"],
                    "strike": None,
                    "option_type": None,
                    "expiry": future_contract["expiry"],
                    "lot_size": future_contract["lot_size"],
                    "token": future_contract["token"],
                }
            if looks_like_future_symbol(tsym) and is_fo_exchange(exchange):
                self.logger.warning(
                    f"resolve_contract_type: {tsym} matched the futures symbol "
                    f"heuristic but FutureMaster doesn't recognize it (stale/missing "
                    f"master?) - classifying as FUTURES with no real expiry/lot_size"
                )
                return {
                    "contract_type": "FUTURES",
                    "underlying": None,
                    "strike": None,
                    "option_type": None,
                    "expiry": None,
                    "lot_size": fallback_lot_size,
                    "token": None,
                }
            return {
                "contract_type": None,
                "underlying": None,
                "strike": None,
                "option_type": None,
                "expiry": None,
                "lot_size": fallback_lot_size,
                "token": None,
            }
        except Exception as ex:
            self.logger.error(f"resolve_contract_type() failed for tsym={tsym}: {str(ex)}")
            raise MarginEngineError(f"resolve_contract_type() failed: {str(ex)}") from ex

    def is_closing_trade(self, user_id: int, tsym: str, side: str, exchange: str) -> bool:
        """True if this order (at least partially) reduces an existing
        opposite-side F&O position - used by api/orders.py to route
        BUY-to-close orders through the margin engine instead of the
        equity-style cash-debit path (Critical Fix #1)."""
        try:
            if not is_fo_exchange(exchange):
                return False
            existing = self.position_reader.get_net_position(user_id, tsym.strip().upper())
            if existing is None:
                return False
            netqty = existing.get("netqty", Decimal(0))
            if side == "BUY":
                return netqty < 0
            return netqty > 0
        except Exception as ex:
            self.logger.error(f"is_closing_trade() failed for user {user_id}, tsym {tsym}: {str(ex)}")
            raise MarginEngineError(f"is_closing_trade() failed: {str(ex)}") from ex

    def check_and_block(self, order, order_id: int, user_id: int) -> MarginCheckResult:
        """
        Pre-trade margin check + block for an already-created order. Only
        the portion of the order quantity that OPENS new exposure (beyond
        whatever closes an existing opposite position) is priced and
        blocked (Critical Fix #1). Callers must delete/cancel the order if
        this raises InsufficientMarginError, mirroring the existing BUY
        wallet-check fail-fast behavior in api/orders.py.

        Raises:
            ValueError: If parameters are invalid
            service.marginengine.exceptions.InsufficientMarginError: If the
                opening portion cannot be covered by available balance
            service.marginengine.exceptions.ReferencePriceUnresolvedError:
                If no price tier resolves for this instrument
            service.marginengine.exceptions.MarginEngineError: On any other failure
        """
        if order is None:
            raise ValueError("Order cannot be None")
        if order_id is None or order_id <= 0:
            raise ValueError("Order ID must be a positive integer")
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")

        tsym = order.symbol.strip().upper()
        side = order.side.value if hasattr(order.side, "value") else str(order.side)
        exchange = order.exchange.value if hasattr(order.exchange, "value") else str(order.exchange)

        instrument = self.resolve_contract_type(tsym, exchange, fallback_lot_size=order.quantity)
        contract_type = instrument["contract_type"]

        if not self.is_margin_required(exchange, side, contract_type):
            return MarginCheckResult(approved=True, required_margin=Decimal(0),
                                      available_balance=Decimal(0), shortfall=Decimal(0))

        guard = LockOrderGuard()
        conn = None
        cursor = None
        try:
            with self.position_cache.lock(user_id, tsym):
                guard.record_position_lock_acquired()

                conn = PostgresConnectionFactory.create_connection()
                cursor = conn.cursor()

                existing_position = self.position_reader.get_net_position(user_id, tsym, cursor)
                existing_netqty = existing_position.get("netqty", Decimal(0)) if existing_position else Decimal(0)
                closing_qty, opening_qty = self._split_closing_opening(side, order.quantity, existing_netqty)

                if opening_qty <= 0:
                    # Pure closing order - no new exposure, nothing to
                    # block. The position's blocked_margin is adjusted
                    # progressively as fills land, via reconcile_on_fill().
                    conn.commit()
                    return MarginCheckResult(approved=True, required_margin=Decimal(0),
                                              available_balance=Decimal(0), shortfall=Decimal(0))

                context = self._build_context(order, tsym, exchange, side, instrument, opening_qty, cursor)
                margin_result = self.broker_provider.get_broker_margin(context)
                if margin_result is None:
                    margin_result = self.calculators[contract_type].calculate(context)

                wallet_persistence = MarginWalletPersistence()
                wallet = wallet_persistence.get_wallet_for_update(cursor, user_id)
                guard.record_wallet_lock_acquired()
                if wallet is None:
                    raise MarginEngineError(f"No wallet found for user {user_id}")

                available_balance = wallet["balance"] - wallet["blocked_margin"]
                if available_balance < margin_result.blocked_amount:
                    shortfall = margin_result.blocked_amount - available_balance
                    raise InsufficientMarginError(margin_result.blocked_amount, available_balance, shortfall)

                block_fields = {
                    "tsym": tsym, "exchange": exchange, "contract_type": contract_type,
                    "side": side, "qty": opening_qty, "lot_size": instrument["lot_size"] or order.quantity,
                }
                block_id = self.ledger_repository.block_order_margin(cursor, user_id, order_id, block_fields, margin_result)

                conn.commit()
                return MarginCheckResult(
                    approved=True, required_margin=margin_result.blocked_amount,
                    available_balance=available_balance - margin_result.blocked_amount,
                    shortfall=Decimal(0), order_margin_block_id=block_id, margin_result=margin_result,
                )
        except (InsufficientMarginError, MarginEngineError):
            if conn is not None:
                conn.rollback()
            raise
        except Exception as ex:
            if conn is not None:
                conn.rollback()
            self.logger.error(f"check_and_block failed for user {user_id}, order {order_id}: {str(ex)}")
            raise MarginEngineError(f"check_and_block failed: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def release_on_cancel(self, order_id: int) -> None:
        """Releases any ACTIVE margin block for a cancelled order. Idempotent -
        a no-op if the order never had a block (equity order, pure-closing
        F&O order, or already released)."""
        if order_id is None or order_id <= 0:
            raise ValueError("Order ID must be a positive integer")

        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            self.ledger_repository.release_order_margin(cursor, order_id, "CANCEL")
            conn.commit()
        except Exception as ex:
            if conn is not None:
                conn.rollback()
            self.logger.error(f"release_on_cancel failed for order {order_id}: {str(ex)}")
            raise MarginEngineError(f"release_on_cancel failed: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def reconcile_on_fill(self, user_id: int, side: str, order_snapshot: dict,
                           new_net_qty, cursor) -> None:
        """
        The only margin-mutating call wired to a fill event (Critical Fix
        #2). Must be called under the same position lock and shared cursor
        PositionService.apply_fill() already holds/uses for this fill.

        Args:
            user_id: User ID for this side of the trade
            side: 'BUY' or 'SELL' (this fill's side)
            order_snapshot: dict from OrderService.get_order_snapshot()
            new_net_qty: the position's netqty AFTER this fill is applied
            cursor: caller-owned database cursor (shared transaction)

        Raises:
            ValueError: If parameters are invalid
            service.marginengine.exceptions.MarginEngineError: On failure
        """
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")
        if cursor is None:
            raise ValueError("Database cursor cannot be None")
        if order_snapshot is None:
            raise ValueError("order_snapshot cannot be None")

        exchange = order_snapshot.get("exchange")
        if not is_fo_exchange(exchange):
            return

        tsym = order_snapshot.get("symbol")
        if not tsym or not tsym.strip():
            raise ValueError("order_snapshot must contain a symbol")
        tsym = tsym.strip().upper()
        order_id = order_snapshot.get("id") or order_snapshot.get("order_id")

        try:
            if order_id:
                # Idempotent: no-op if this order never had an active block
                # (a pure-closing order, or a later fill of a multi-fill order).
                self.ledger_repository.release_order_margin(cursor, order_id, "FILL")

            instrument = self.resolve_contract_type(tsym, exchange, fallback_lot_size=order_snapshot.get("lot_size"))
            contract_type = instrument["contract_type"]
            was_unclassified = contract_type not in ("OPTION", "FUTURES")
            if was_unclassified:
                # This position's exchange is F&O (checked above) but neither
                # OptionMaster nor the futures-symbol heuristic recognized
                # it - default to the OPTION formula rather than skip
                # margining entirely, since that is strictly more
                # conservative than leaving the position unmargined.
                self.logger.warning(
                    f"reconcile_on_fill: could not classify F&O instrument {tsym} on {exchange}; "
                    f"defaulting to OPTION margin formula"
                )
                contract_type = "OPTION"
                instrument["contract_type"] = "OPTION"
            new_net_qty = Decimal(str(new_net_qty))

            if new_net_qty == 0:
                new_required_margin = Decimal(0)
            elif contract_type == "FUTURES":
                required_side = "SELL" if new_net_qty < 0 else "BUY"
                context = self._build_context_for_reconcile(
                    tsym, exchange, required_side, instrument, abs(int(new_net_qty)), order_snapshot, cursor
                )
                result = self.calculators["FUTURES"].calculate(context)
                new_required_margin = result.blocked_amount
            elif new_net_qty < 0 or was_unclassified:
                # Short option position: margin required on the short qty.
                # Also applies to a LONG position of an unclassified
                # instrument - the "default to OPTION, conservative" claim
                # above is only true if a long position is margined too;
                # exempting it via the long-option-pays-premium-only rule
                # below would silently zero the margin for what might
                # actually be genuine directional exposure (e.g. a
                # misclassified future), which is the opposite of
                # conservative.
                context = self._build_context_for_reconcile(
                    tsym, exchange, "SELL", instrument, abs(int(new_net_qty)), order_snapshot, cursor
                )
                result = self.calculators["OPTION"].calculate(context)
                new_required_margin = result.blocked_amount
            else:
                # Long option position (genuinely classified as OPTION):
                # buyer only ever pays premium up front (already debited via
                # the wallet balance check at order time) - no margin block.
                new_required_margin = Decimal(0)

            self.ledger_repository.reconcile_position_margin(cursor, user_id, tsym, new_required_margin, contract_type)
        except Exception as ex:
            self.logger.error(f"reconcile_on_fill failed for user {user_id}, tsym {tsym}: {str(ex)}")
            raise MarginEngineError(f"reconcile_on_fill failed: {str(ex)}") from ex

    # -- internal helpers ----------------------------------------------------

    def _split_closing_opening(self, side: str, qty: int, existing_netqty: Decimal) -> tuple[int, int]:
        qty = int(qty)
        existing_netqty = existing_netqty or Decimal(0)
        if side == "BUY" and existing_netqty < 0:
            closing_qty = min(qty, int(-existing_netqty))
            return closing_qty, qty - closing_qty
        if side == "SELL" and existing_netqty > 0:
            closing_qty = min(qty, int(existing_netqty))
            return closing_qty, qty - closing_qty
        return 0, qty

    def _expiry_multiplier(self, expiry, config: dict) -> Decimal:
        if not expiry:
            return Decimal("1.0")
        try:
            expiry_date = expiry if isinstance(expiry, date) else datetime.strptime(str(expiry), "%Y-%m-%d").date()
            days_to_expiry = (expiry_date - datetime.now().date()).days
            if days_to_expiry <= config["near_expiry_days"]:
                return Decimal(str(config["near_expiry_multiplier"]))
            if days_to_expiry >= config["far_expiry_days"]:
                return Decimal(str(config["far_expiry_multiplier"]))
            return Decimal("1.0")
        except Exception as ex:
            self.logger.warning(f"Could not compute expiry_multiplier for expiry={expiry}: {str(ex)}")
            return Decimal("1.0")

    def _price_source_multiplier(self, tier: int, config: dict) -> Decimal:
        tier_field = {
            1: "price_source_tier1_multiplier", 2: "price_source_tier2_multiplier",
            3: "price_source_tier3_multiplier", 4: "price_source_tier4_multiplier",
        }.get(tier, "price_source_tier4_multiplier")
        return Decimal(str(config.get(tier_field, Decimal("1.0"))))

    def _get_config(self, contract_type: str, underlying: Optional[str]) -> dict:
        config = self.config_persistence.get_config(contract_type, underlying)
        if config is not None:
            return config
        # No row in margin_config at all (should only happen if the seed
        # insert was skipped) - fall back to safe hardcoded defaults rather
        # than blocking every F&O order.
        self.logger.warning(f"No margin_config found for {contract_type}/{underlying}, using hardcoded defaults")
        return {
            "notional_pct": _DEFAULT_NOTIONAL_PCT, "span_pct": _DEFAULT_SPAN_PCT,
            "near_expiry_multiplier": Decimal("1.2"), "far_expiry_multiplier": Decimal("1.0"),
            "near_expiry_days": 2, "far_expiry_days": 30,
            "moneyness_itm_multiplier": Decimal("1.2"), "moneyness_atm_multiplier": Decimal("1.0"),
            "moneyness_otm_multiplier": Decimal("0.9"), "session_gap_multiplier": Decimal("1.1"),
            "price_source_tier1_multiplier": Decimal("1.0"), "price_source_tier2_multiplier": Decimal("1.0"),
            "price_source_tier3_multiplier": Decimal("1.1"), "price_source_tier4_multiplier": Decimal("1.2"),
            "tier3_verification_band_pct": Decimal("0.20"),
        }

    def _build_context(self, order, tsym, exchange, side, instrument, qty, cursor) -> MarginContext:
        contract_type = instrument["contract_type"]
        config = self._get_config(contract_type, instrument.get("underlying"))

        resolution = self.price_resolver.resolve(
            tsym, contract_type, instrument.get("underlying"), exchange, instrument.get("token"),
            order_price=Decimal(str(order.price)) if order.price else None,
            strike=instrument.get("strike"), option_type=instrument.get("option_type"),
            expiry=instrument.get("expiry"),
            verification_band_pct=Decimal(str(config["tier3_verification_band_pct"])),
        )

        notional_pct_or_span_pct = Decimal(str(config["notional_pct"] if contract_type == "OPTION" else config["span_pct"]))

        # Calculators compute notional as lot_size * qty - qty here is a raw
        # unit count (opening_qty), not a lot count, and there is no reliable
        # way to always derive a true lot count (futures have no instrument
        # master to divide by). Passing lot_size=1 with qty=total units makes
        # lot_size*qty equal the real total exposure directly, regardless of
        # instrument class - using the real per-lot lot_size here as well
        # would silently square the exposure (e.g. a 65-unit NIFTY order
        # computing margin as if it were 65 lots = 4225 units).
        return MarginContext(
            contract_type=contract_type, side=side, tsym=tsym, exchange=exchange,
            lot_size=1, qty=qty,
            product_type=order.product_type.value if hasattr(order.product_type, "value") else str(order.product_type),
            reference_price=resolution.price, reference_source=resolution.source,
            reference_source_tier=resolution.tier, notional_pct_or_span_pct=notional_pct_or_span_pct,
            expiry_multiplier=self._expiry_multiplier(instrument.get("expiry"), config),
            price_source_multiplier=self._price_source_multiplier(resolution.tier, config),
            underlying=instrument.get("underlying"), strike=instrument.get("strike"),
            option_type=instrument.get("option_type"), expiry=instrument.get("expiry"),
            spot_price=resolution.spot,
            moneyness_itm_multiplier=Decimal(str(config["moneyness_itm_multiplier"])),
            moneyness_atm_multiplier=Decimal(str(config["moneyness_atm_multiplier"])),
            moneyness_otm_multiplier=Decimal(str(config["moneyness_otm_multiplier"])),
            session_gap_multiplier=Decimal(str(config["session_gap_multiplier"])),
            apply_session_gap=False,
        )

    def _build_context_for_reconcile(self, tsym, exchange, side, instrument, qty, order_snapshot, cursor) -> MarginContext:
        contract_type = instrument["contract_type"]
        config = self._get_config(contract_type, instrument.get("underlying"))

        # The fill's own execution price is the best possible reference
        # price available - an actual matched trade, strictly better
        # signal than any of the pre-trade resolver's tiers.
        fill_price = Decimal(str(order_snapshot.get("avg_fill_price") or order_snapshot.get("price") or 0))
        if fill_price <= 0:
            resolution = self.price_resolver.resolve(
                tsym, contract_type, instrument.get("underlying"), exchange, instrument.get("token"),
                strike=instrument.get("strike"), option_type=instrument.get("option_type"),
                expiry=instrument.get("expiry"),
                verification_band_pct=Decimal(str(config["tier3_verification_band_pct"])),
            )
            reference_price, reference_source, reference_tier, spot = resolution.price, resolution.source, resolution.tier, resolution.spot
        else:
            reference_price, reference_source, reference_tier, spot = fill_price, "FILL_PRICE", 1, None

        notional_pct_or_span_pct = Decimal(str(config["notional_pct"] if contract_type == "OPTION" else config["span_pct"]))

        # See the identical comment in _build_context() above - lot_size=1,
        # qty=total units keeps lot_size*qty equal to the real total
        # exposure instead of squaring it.
        return MarginContext(
            contract_type=contract_type, side=side, tsym=tsym, exchange=exchange,
            lot_size=1, qty=qty,
            product_type=order_snapshot.get("product_type") or "MIS",
            reference_price=reference_price, reference_source=reference_source,
            reference_source_tier=reference_tier, notional_pct_or_span_pct=notional_pct_or_span_pct,
            expiry_multiplier=self._expiry_multiplier(instrument.get("expiry"), config),
            price_source_multiplier=self._price_source_multiplier(reference_tier, config),
            underlying=instrument.get("underlying"), strike=instrument.get("strike"),
            option_type=instrument.get("option_type"), expiry=instrument.get("expiry"),
            spot_price=spot,
            moneyness_itm_multiplier=Decimal(str(config["moneyness_itm_multiplier"])),
            moneyness_atm_multiplier=Decimal(str(config["moneyness_atm_multiplier"])),
            moneyness_otm_multiplier=Decimal(str(config["moneyness_otm_multiplier"])),
            session_gap_multiplier=Decimal(str(config["session_gap_multiplier"])),
            apply_session_gap=False,
        )
