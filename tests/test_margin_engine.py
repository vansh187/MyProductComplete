"""
Tests for the F&O margin engine (service/marginengine, database/marginenginepersistence).

Calculator and guard tests are pure unit tests (no mocking needed). MarginEngine
facade tests inject fake ReferencePriceResolver/MarginLedgerRepository/
BrokerMarginProvider/PositionReader implementations (the whole point of the
ports & adapters design) and patch out PositionCache/PostgresConnectionFactory
so no real Redis/Postgres connection is required to exercise the control flow.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from service.marginengine.exceptions import (
    InsufficientMarginError,
    LockOrderViolationError,
    MarginEngineError,
    ReferencePriceUnresolvedError,
)
from service.marginengine.futures_margin_calculator import FuturesMarginCalculator
from service.marginengine.interfaces import BrokerMarginProvider, MarginLedgerRepository, PositionReader, ReferencePriceResolver
from service.marginengine.lock_order_guard import LockOrderGuard
from service.marginengine.margin_engine import MarginEngine
from service.marginengine.models import MarginContext, MarginResult, PriceResolution
from service.marginengine.options_margin_calculator import OptionsMarginCalculator


def _option_context(**overrides) -> MarginContext:
    defaults = dict(
        contract_type="OPTION", side="SELL", tsym="NIFTY07JUL2623800CE", exchange="NFO",
        lot_size=50, qty=1, product_type="MIS",
        reference_price=Decimal("120"), reference_source="CACHED_CHAIN", reference_source_tier=2,
        notional_pct_or_span_pct=Decimal("0.15"), expiry_multiplier=Decimal("1.0"),
        price_source_multiplier=Decimal("1.0"), underlying="NIFTY", strike=Decimal("23800"),
        option_type="CE", spot_price=Decimal("23800"),
        moneyness_itm_multiplier=Decimal("1.2"), moneyness_atm_multiplier=Decimal("1.0"),
        moneyness_otm_multiplier=Decimal("0.9"),
    )
    defaults.update(overrides)
    return MarginContext(**defaults)


def _futures_context(**overrides) -> MarginContext:
    defaults = dict(
        contract_type="FUTURES", side="SELL", tsym="NIFTY28AUG25FUT", exchange="NFO",
        lot_size=50, qty=1, product_type="NRML",
        reference_price=Decimal("21850"), reference_source="ORDER_PRICE_PROXY", reference_source_tier=4,
        notional_pct_or_span_pct=Decimal("0.12"), expiry_multiplier=Decimal("1.0"),
        price_source_multiplier=Decimal("1.2"),
    )
    defaults.update(overrides)
    return MarginContext(**defaults)


# ============================================================================
# OptionsMarginCalculator
# ============================================================================

class TestOptionsMarginCalculator:

    def test_atm_short_option_blocks_premium_plus_notional(self):
        calc = OptionsMarginCalculator()
        context = _option_context()  # spot == strike -> ATM
        result = calc.calculate(context)

        assert result.premium_component == Decimal("120") * 50
        expected_notional = Decimal("0.15") * Decimal("23800") * 50 * Decimal("1.0") * Decimal("1.0") * Decimal("1.0")
        assert result.notional_component == expected_notional
        assert result.blocked_amount == result.premium_component + result.notional_component
        assert result.moneyness_multiplier_used == Decimal("1.0")

    def test_deep_itm_short_call_uses_itm_multiplier(self):
        calc = OptionsMarginCalculator()
        # Spot well above strike for a short CE = deep ITM, real assignment risk.
        context = _option_context(spot_price=Decimal("25000"), strike=Decimal("23800"), option_type="CE")
        result = calc.calculate(context)
        assert result.moneyness_multiplier_used == Decimal("1.2")

    def test_otm_short_put_uses_otm_multiplier(self):
        calc = OptionsMarginCalculator()
        context = _option_context(spot_price=Decimal("25000"), strike=Decimal("23800"), option_type="PE")
        result = calc.calculate(context)
        assert result.moneyness_multiplier_used == Decimal("0.9")

    def test_falls_back_to_strike_when_spot_unavailable(self):
        calc = OptionsMarginCalculator()
        context = _option_context(spot_price=None)
        result = calc.calculate(context)
        expected_notional = Decimal("0.15") * Decimal("23800") * 50
        assert result.notional_component == expected_notional

    def test_buy_side_raises_margin_engine_error(self):
        calc = OptionsMarginCalculator()
        context = _option_context(side="BUY")
        with pytest.raises(MarginEngineError):
            calc.calculate(context)

    def test_wrong_contract_type_raises(self):
        calc = OptionsMarginCalculator()
        context = _option_context(contract_type="FUTURES")
        with pytest.raises(MarginEngineError):
            calc.calculate(context)

    def test_missing_spot_and_strike_raises_cleanly(self):
        calc = OptionsMarginCalculator()
        context = _option_context(spot_price=None, strike=None)
        with pytest.raises(MarginEngineError):
            calc.calculate(context)


# ============================================================================
# FuturesMarginCalculator
# ============================================================================

class TestFuturesMarginCalculator:

    def test_sell_future_blocks_span_pct_of_notional(self):
        calc = FuturesMarginCalculator()
        context = _futures_context(side="SELL")
        result = calc.calculate(context)
        expected = Decimal("21850") * 50 * Decimal("0.12") * Decimal("1.0") * Decimal("1.2")
        assert result.blocked_amount == expected
        assert result.premium_component is None

    def test_buy_future_blocks_identically_to_sell(self):
        calc = FuturesMarginCalculator()
        sell_result = calc.calculate(_futures_context(side="SELL"))
        buy_result = calc.calculate(_futures_context(side="BUY"))
        assert buy_result.blocked_amount == sell_result.blocked_amount

    def test_wrong_contract_type_raises(self):
        calc = FuturesMarginCalculator()
        with pytest.raises(MarginEngineError):
            calc.calculate(_futures_context(contract_type="OPTION"))

    def test_non_positive_reference_price_raises(self):
        calc = FuturesMarginCalculator()
        with pytest.raises(MarginEngineError):
            calc.calculate(_futures_context(reference_price=Decimal("0")))


# ============================================================================
# LockOrderGuard
# ============================================================================

class TestLockOrderGuard:

    def test_wallet_lock_after_position_lock_is_allowed(self):
        guard = LockOrderGuard()
        guard.record_position_lock_acquired()
        guard.record_wallet_lock_acquired()  # must not raise

    def test_wallet_lock_before_position_lock_raises(self):
        guard = LockOrderGuard()
        with pytest.raises(LockOrderViolationError):
            guard.record_wallet_lock_acquired()


# ============================================================================
# MarginEngine facade - fakes injected for every external dependency
# ============================================================================

class _FakePriceResolver(ReferencePriceResolver):
    def __init__(self, price=Decimal("120"), source="CACHED_CHAIN", tier=2, spot=Decimal("23800")):
        self.price, self.source, self.tier, self.spot = price, source, tier, spot

    def resolve(self, tsym, contract_type, underlying, exchange, token, order_price=None, **kwargs):
        return PriceResolution(price=self.price, source=self.source, tier=self.tier, spot=self.spot)


class _UnresolvableFakePriceResolver(ReferencePriceResolver):
    def resolve(self, tsym, contract_type, underlying, exchange, token, order_price=None, **kwargs):
        raise ReferencePriceUnresolvedError(tsym, [{"tier": 1, "result": "NO_DATA"}])


class _FakeLedgerRepository(MarginLedgerRepository):
    def __init__(self):
        self.blocked = []
        self.released = []
        self.reconciled = []

    def block_order_margin(self, cursor, user_id, order_id, block_fields, margin_result):
        self.blocked.append((user_id, order_id, block_fields, margin_result))
        return 999

    def release_order_margin(self, cursor, order_id, release_reason):
        self.released.append((order_id, release_reason))

    def reconcile_position_margin(self, cursor, user_id, tsym, new_required_margin, contract_type):
        self.reconciled.append((user_id, tsym, new_required_margin, contract_type))


class _FakeBrokerProvider(BrokerMarginProvider):
    def get_broker_margin(self, context):
        return None


class _FakePositionReader(PositionReader):
    def __init__(self, netqty=Decimal(0)):
        self.netqty = netqty

    def get_net_position(self, user_id, tsym, cursor=None):
        if self.netqty == 0:
            return None
        return {"netqty": self.netqty, "status": "OPEN"}


def _build_engine(price_resolver=None, ledger_repository=None, position_reader=None):
    return MarginEngine(
        price_resolver=price_resolver or _FakePriceResolver(),
        ledger_repository=ledger_repository or _FakeLedgerRepository(),
        broker_provider=_FakeBrokerProvider(),
        position_reader=position_reader or _FakePositionReader(),
    )


class _FakeOrder:
    def __init__(self, symbol, side, exchange, quantity, price=None, product_type="MIS"):
        self.symbol = symbol
        self.side = MagicMock(value=side)
        self.exchange = MagicMock(value=exchange)
        self.quantity = quantity
        self.price = price
        self.product_type = MagicMock(value=product_type)


class TestMarginEngineClosingSplit:

    def test_sell_with_no_existing_position_is_pure_opening(self):
        engine = _build_engine(position_reader=_FakePositionReader(netqty=Decimal(0)))
        closing, opening = engine._split_closing_opening("SELL", 50, Decimal(0))
        assert closing == 0
        assert opening == 50

    def test_sell_reducing_existing_long_is_pure_closing(self):
        engine = _build_engine()
        closing, opening = engine._split_closing_opening("SELL", 50, Decimal(50))
        assert closing == 50
        assert opening == 0

    def test_sell_reversing_existing_long_splits_closing_and_opening(self):
        engine = _build_engine()
        closing, opening = engine._split_closing_opening("SELL", 80, Decimal(50))
        assert closing == 50
        assert opening == 30

    def test_buy_covering_short_is_pure_closing(self):
        engine = _build_engine()
        closing, opening = engine._split_closing_opening("BUY", 50, Decimal(-50))
        assert closing == 50
        assert opening == 0


class TestMarginEngineIsClosingTrade:

    def test_buy_with_existing_short_is_closing(self):
        engine = _build_engine(position_reader=_FakePositionReader(netqty=Decimal(-50)))
        assert engine.is_closing_trade(1, "NIFTY07JUL2623800CE", "BUY", "NFO") is True

    def test_buy_with_no_position_is_not_closing(self):
        engine = _build_engine(position_reader=_FakePositionReader(netqty=Decimal(0)))
        assert engine.is_closing_trade(1, "NIFTY07JUL2623800CE", "BUY", "NFO") is False

    def test_equity_exchange_is_never_closing(self):
        engine = _build_engine(position_reader=_FakePositionReader(netqty=Decimal(-50)))
        assert engine.is_closing_trade(1, "RELIANCE", "BUY", "NSE") is False


class TestMarginEngineCheckAndBlock:

    def _order(self, symbol="NIFTY07JUL2623800CE", side="SELL", exchange="NFO", qty=1):
        return _FakeOrder(symbol=symbol, side=side, exchange=exchange, quantity=qty, price=120.0)

    @patch("service.marginengine.margin_engine.OptionMaster.find_by_tsym")
    @patch("service.marginengine.margin_engine.PostgresConnectionFactory")
    @patch("service.marginengine.margin_engine.PositionCache")
    @patch("service.marginengine.margin_engine.MarginConfigPersistence")
    @patch("service.marginengine.margin_engine.MarginWalletPersistence")
    def test_sufficient_margin_blocks_and_returns_approved(
        self, MockWalletPersistence, MockConfigPersistence, MockPositionCache, MockConnFactory, mock_find_by_tsym
    ):
        mock_find_by_tsym.return_value = {
            "token": "12345", "lot_size": 50, "exchange": "NFO", "underlying": "NIFTY",
            "expiry": "2026-07-07", "strike": 23800.0, "option_type": "CE",
        }
        MockPositionCache.return_value.lock.return_value.__enter__ = MagicMock(return_value=None)
        MockPositionCache.return_value.lock.return_value.__exit__ = MagicMock(return_value=False)
        MockConnFactory.create_connection.return_value = MagicMock()
        MockConfigPersistence.return_value.get_config.return_value = {
            "notional_pct": Decimal("0.15"), "span_pct": None,
            "near_expiry_multiplier": Decimal("1.2"), "far_expiry_multiplier": Decimal("1.0"),
            "near_expiry_days": 2, "far_expiry_days": 30,
            "moneyness_itm_multiplier": Decimal("1.2"), "moneyness_atm_multiplier": Decimal("1.0"),
            "moneyness_otm_multiplier": Decimal("0.9"), "session_gap_multiplier": Decimal("1.1"),
            "price_source_tier1_multiplier": Decimal("1.0"), "price_source_tier2_multiplier": Decimal("1.0"),
            "price_source_tier3_multiplier": Decimal("1.1"), "price_source_tier4_multiplier": Decimal("1.2"),
            "tier3_verification_band_pct": Decimal("0.20"),
        }
        MockWalletPersistence.return_value.get_wallet_for_update.return_value = {
            "user_id": 1, "balance": Decimal("1000000"), "blocked_margin": Decimal("0")
        }

        fake_ledger = _FakeLedgerRepository()
        engine = _build_engine(ledger_repository=fake_ledger, position_reader=_FakePositionReader(netqty=Decimal(0)))
        result = engine.check_and_block(self._order(), order_id=555, user_id=1)

        assert result.approved is True
        assert result.required_margin > 0
        assert len(fake_ledger.blocked) == 1
        assert fake_ledger.blocked[0][1] == 555  # order_id

    @patch("service.marginengine.margin_engine.OptionMaster.find_by_tsym")
    @patch("service.marginengine.margin_engine.PostgresConnectionFactory")
    @patch("service.marginengine.margin_engine.PositionCache")
    @patch("service.marginengine.margin_engine.MarginConfigPersistence")
    @patch("service.marginengine.margin_engine.MarginWalletPersistence")
    def test_insufficient_balance_raises_and_blocks_nothing(
        self, MockWalletPersistence, MockConfigPersistence, MockPositionCache, MockConnFactory, mock_find_by_tsym
    ):
        mock_find_by_tsym.return_value = {
            "token": "12345", "lot_size": 50, "exchange": "NFO", "underlying": "NIFTY",
            "expiry": "2026-07-07", "strike": 23800.0, "option_type": "CE",
        }
        MockPositionCache.return_value.lock.return_value.__enter__ = MagicMock(return_value=None)
        MockPositionCache.return_value.lock.return_value.__exit__ = MagicMock(return_value=False)
        MockConnFactory.create_connection.return_value = MagicMock()
        MockConfigPersistence.return_value.get_config.return_value = {
            "notional_pct": Decimal("0.15"), "span_pct": None,
            "near_expiry_multiplier": Decimal("1.2"), "far_expiry_multiplier": Decimal("1.0"),
            "near_expiry_days": 2, "far_expiry_days": 30,
            "moneyness_itm_multiplier": Decimal("1.2"), "moneyness_atm_multiplier": Decimal("1.0"),
            "moneyness_otm_multiplier": Decimal("0.9"), "session_gap_multiplier": Decimal("1.1"),
            "price_source_tier1_multiplier": Decimal("1.0"), "price_source_tier2_multiplier": Decimal("1.0"),
            "price_source_tier3_multiplier": Decimal("1.1"), "price_source_tier4_multiplier": Decimal("1.2"),
            "tier3_verification_band_pct": Decimal("0.20"),
        }
        MockWalletPersistence.return_value.get_wallet_for_update.return_value = {
            "user_id": 1, "balance": Decimal("10"), "blocked_margin": Decimal("0")
        }

        fake_ledger = _FakeLedgerRepository()
        engine = _build_engine(ledger_repository=fake_ledger, position_reader=_FakePositionReader(netqty=Decimal(0)))

        with pytest.raises(InsufficientMarginError):
            engine.check_and_block(self._order(), order_id=555, user_id=1)

        assert len(fake_ledger.blocked) == 0

    @patch("service.marginengine.margin_engine.OptionMaster.find_by_tsym")
    @patch("service.marginengine.margin_engine.PositionCache")
    def test_pure_closing_order_requires_no_block(self, MockPositionCache, mock_find_by_tsym):
        mock_find_by_tsym.return_value = {
            "token": "12345", "lot_size": 50, "exchange": "NFO", "underlying": "NIFTY",
            "expiry": "2026-07-07", "strike": 23800.0, "option_type": "CE",
        }
        MockPositionCache.return_value.lock.return_value.__enter__ = MagicMock(return_value=None)
        MockPositionCache.return_value.lock.return_value.__exit__ = MagicMock(return_value=False)

        fake_ledger = _FakeLedgerRepository()
        # Existing long position >= order qty -> pure closing SELL.
        engine = _build_engine(ledger_repository=fake_ledger, position_reader=_FakePositionReader(netqty=Decimal(50)))

        with patch("service.marginengine.margin_engine.PostgresConnectionFactory") as MockConnFactory:
            mock_conn = MagicMock()
            MockConnFactory.create_connection.return_value = mock_conn
            result = engine.check_and_block(self._order(side="SELL", qty=50), order_id=555, user_id=1)

        assert result.approved is True
        assert result.required_margin == Decimal(0)
        assert len(fake_ledger.blocked) == 0

    @patch("service.marginengine.margin_engine.OptionMaster.find_by_tsym")
    def test_equity_order_is_never_margin_checked(self, mock_find_by_tsym):
        mock_find_by_tsym.return_value = None  # not an option
        engine = _build_engine()
        result = engine.check_and_block(
            self._order(symbol="RELIANCE", side="SELL", exchange="NSE", qty=10), order_id=555, user_id=1
        )
        assert result.approved is True
        assert result.required_margin == Decimal(0)


class TestMarginEngineReconcileOnFill:

    def test_non_fo_exchange_is_a_no_op(self):
        engine = _build_engine()
        cursor = MagicMock()
        # Must not raise and must not touch the ledger repository at all.
        engine.reconcile_on_fill(1, "BUY", {"symbol": "RELIANCE", "exchange": "NSE"}, Decimal(10), cursor)
        assert engine.ledger_repository.reconciled == []

    @patch("service.marginengine.margin_engine.OptionMaster.find_by_tsym")
    @patch("service.marginengine.margin_engine.MarginConfigPersistence")
    def test_position_closed_to_zero_reconciles_to_zero_margin(self, MockConfigPersistence, mock_find_by_tsym):
        mock_find_by_tsym.return_value = {
            "token": "12345", "lot_size": 50, "exchange": "NFO", "underlying": "NIFTY",
            "expiry": "2026-07-07", "strike": 23800.0, "option_type": "CE",
        }
        fake_ledger = _FakeLedgerRepository()
        engine = _build_engine(ledger_repository=fake_ledger)
        cursor = MagicMock()

        engine.reconcile_on_fill(
            1, "BUY", {"symbol": "NIFTY07JUL2623800CE", "exchange": "NFO", "id": 555, "lot_size": 50},
            Decimal(0), cursor
        )

        assert fake_ledger.released == [(555, "FILL")]
        assert len(fake_ledger.reconciled) == 1
        assert fake_ledger.reconciled[0][2] == Decimal(0)

    @patch("service.marginengine.margin_engine.OptionMaster.find_by_tsym")
    @patch("service.marginengine.margin_engine.MarginConfigPersistence")
    def test_short_option_position_reconciles_to_positive_margin(self, MockConfigPersistence, mock_find_by_tsym):
        mock_find_by_tsym.return_value = {
            "token": "12345", "lot_size": 50, "exchange": "NFO", "underlying": "NIFTY",
            "expiry": "2026-07-07", "strike": 23800.0, "option_type": "CE",
        }
        MockConfigPersistence.return_value.get_config.return_value = {
            "notional_pct": Decimal("0.15"), "span_pct": None,
            "near_expiry_multiplier": Decimal("1.2"), "far_expiry_multiplier": Decimal("1.0"),
            "near_expiry_days": 2, "far_expiry_days": 30,
            "moneyness_itm_multiplier": Decimal("1.2"), "moneyness_atm_multiplier": Decimal("1.0"),
            "moneyness_otm_multiplier": Decimal("0.9"), "session_gap_multiplier": Decimal("1.1"),
            "price_source_tier1_multiplier": Decimal("1.0"), "price_source_tier2_multiplier": Decimal("1.0"),
            "price_source_tier3_multiplier": Decimal("1.1"), "price_source_tier4_multiplier": Decimal("1.2"),
            "tier3_verification_band_pct": Decimal("0.20"),
        }
        fake_ledger = _FakeLedgerRepository()
        engine = _build_engine(ledger_repository=fake_ledger)
        cursor = MagicMock()

        engine.reconcile_on_fill(
            1, "SELL",
            {"symbol": "NIFTY07JUL2623800CE", "exchange": "NFO", "id": None,
             "lot_size": 50, "avg_fill_price": 120.0},
            Decimal(-50), cursor
        )

        assert len(fake_ledger.reconciled) == 1
        assert fake_ledger.reconciled[0][2] > Decimal(0)
        assert fake_ledger.reconciled[0][3] == "OPTION"

    @patch("service.marginengine.margin_engine.OptionMaster.find_by_tsym")
    def test_long_option_position_requires_no_margin(self, mock_find_by_tsym):
        mock_find_by_tsym.return_value = {
            "token": "12345", "lot_size": 50, "exchange": "NFO", "underlying": "NIFTY",
            "expiry": "2026-07-07", "strike": 23800.0, "option_type": "CE",
        }
        fake_ledger = _FakeLedgerRepository()
        engine = _build_engine(ledger_repository=fake_ledger)
        cursor = MagicMock()

        engine.reconcile_on_fill(
            1, "BUY", {"symbol": "NIFTY07JUL2623800CE", "exchange": "NFO", "id": None, "lot_size": 50},
            Decimal(50), cursor
        )

        assert fake_ledger.reconciled[0][2] == Decimal(0)


class TestMarginEngineResolveContractType:

    @patch("service.marginengine.margin_engine.OptionMaster.find_by_tsym")
    def test_recognized_option_symbol_classified_as_option(self, mock_find_by_tsym):
        mock_find_by_tsym.return_value = {
            "token": "12345", "lot_size": 50, "exchange": "NFO", "underlying": "NIFTY",
            "expiry": "2026-07-07", "strike": 23800.0, "option_type": "CE",
        }
        engine = _build_engine()
        instrument = engine.resolve_contract_type("NIFTY07JUL2623800CE", "NFO")
        assert instrument["contract_type"] == "OPTION"
        assert instrument["strike"] == Decimal("23800.0")

    @patch("service.marginengine.margin_engine.OptionMaster.find_by_tsym")
    def test_fut_suffix_on_fo_exchange_classified_as_futures(self, mock_find_by_tsym):
        mock_find_by_tsym.return_value = None
        engine = _build_engine()
        instrument = engine.resolve_contract_type("NIFTY28AUG25FUT", "NFO", fallback_lot_size=50)
        assert instrument["contract_type"] == "FUTURES"
        assert instrument["lot_size"] == 50

    @patch("service.marginengine.margin_engine.OptionMaster.find_by_tsym")
    def test_equity_symbol_classified_as_none(self, mock_find_by_tsym):
        mock_find_by_tsym.return_value = None
        engine = _build_engine()
        instrument = engine.resolve_contract_type("RELIANCE", "NSE")
        assert instrument["contract_type"] is None


class TestMarginEngineIsMarginRequired:

    def test_option_buy_never_requires_margin(self):
        engine = _build_engine()
        assert engine.is_margin_required("NFO", "BUY", "OPTION") is False

    def test_option_sell_requires_margin(self):
        engine = _build_engine()
        assert engine.is_margin_required("NFO", "SELL", "OPTION") is True

    def test_futures_buy_and_sell_both_require_margin(self):
        engine = _build_engine()
        assert engine.is_margin_required("NFO", "BUY", "FUTURES") is True
        assert engine.is_margin_required("NFO", "SELL", "FUTURES") is True

    def test_none_contract_type_never_requires_margin(self):
        engine = _build_engine()
        assert engine.is_margin_required("NFO", "SELL", None) is False

    def test_non_fo_exchange_never_requires_margin(self):
        engine = _build_engine()
        assert engine.is_margin_required("NSE", "SELL", "OPTION") is False
