"""
Production corner cases: server-only order tagging, strict cancel after a
broker reject, the algo_id kwarg, scrip-master reload/refresh failure
handling, the expiry-day close roll, SPAN leg validation, OI baselines with a
blank previous OI, and broker number parsing.

No real broker/DB/network calls - every external boundary is mocked.
"""

import asyncio
import json
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from appconfig import OptionMaster
from appconfig.ScripMasterRefresher import ScripMasterRefresher, upcoming_expiry_dates
from service.optionChain.OptionChainCache import OptionChainCache
from service.spanMargin.SpanMarginService import SpanMarginService
from utils.market_hours import IST_OFFSET
from utils.safe_numbers import safe_float, safe_int


# ── Server-only BROKER tag ──────────────────────────────────────────────────

class TestOrderSourceIsServerDecided:

    def _create(self, order_source, broker_routed, monkeypatch):
        from service.orderService import OrderService
        monkeypatch.setenv("IS_PROD_ENVIRONMENT", "false")
        service = OrderService()
        service.order_persistence = MagicMock()
        service.order_persistence.create_order.return_value = 101
        order = MagicMock(symbol="RELIANCE", quantity=10, source=order_source)
        with patch("service.orderService.OptionMaster.find_by_tsym", return_value=None):
            service.create_order(order, 42, broker_routed=broker_routed)
        return service.order_persistence.create_order.call_args.args[0].source

    def test_request_body_cannot_tag_an_order_as_broker_routed(self, monkeypatch):
        """Review finding: POST /orders with {"source": "BROKER"} must stay simulated."""
        assert self._create("BROKER", False, monkeypatch) == "SIMULATED"

    def test_only_the_server_argument_tags_broker_routed(self, monkeypatch):
        assert self._create(None, True, monkeypatch) == "BROKER"


# ── Strict cancel after a broker reject ─────────────────────────────────────

class TestCancelRejectedOrder:

    def _service(self, margin_error=None, refund_error=None):
        from service.orderService import OrderService
        service = OrderService()
        service.order_persistence = MagicMock()
        service.order_persistence.cancel_order_by_id.return_value = {
            "side": "BUY", "price": 100.0, "quantity": 75, "symbol": "X", "exchange": "NFO",
        }
        service.margin_engine = MagicMock()
        service.margin_engine.resolve_contract_type.return_value = {"contract_type": "OPTION"}
        if margin_error is not None:
            service.margin_engine.release_on_cancel.side_effect = margin_error
        service.wallet_service = MagicMock()
        if refund_error is not None:
            service.wallet_service.creditWalletStandalone.side_effect = refund_error
        return service

    def test_clean_release_returns_true(self):
        assert self._service().cancel_rejected_order(42, 101) is True

    def test_margin_release_failure_is_raised_after_refunding(self):
        from service.marginengine.exceptions import MarginEngineError
        from service.orderService import FundsReleaseError
        service = self._service(margin_error=MarginEngineError("ledger down"))
        with pytest.raises(FundsReleaseError):
            service.cancel_rejected_order(42, 101)
        service.wallet_service.creditWalletStandalone.assert_called_once()  # still refunded

    def test_refund_failure_is_raised(self):
        from service.orderService import FundsReleaseError
        with pytest.raises(FundsReleaseError):
            self._service(refund_error=Exception("db")).cancel_rejected_order(42, 101)

    def test_user_cancel_stays_best_effort(self):
        from service.marginengine.exceptions import MarginEngineError
        service = self._service(margin_error=MarginEngineError("ledger down"))
        assert service.cancel_order_by_id(42, 101) is True


# ── algo_id kwarg ───────────────────────────────────────────────────────────

class TestAlgoIdKwarg:

    def _place(self, client, algo_id):
        from service.shoonyaOrderService import ShoonyaOrderService
        return ShoonyaOrderService(client).place_order(
            side="BUY", product_type="NRML", exchange="NFO", tradingsymbol="X",
            quantity=65, order_type="LIMIT", price=1.0, algo_id=algo_id,
        )

    def test_no_algo_id_is_not_sent_to_a_client_without_the_parameter(self):
        """Review finding: algo_id=None used to be passed unconditionally."""
        class _OldClient:
            def place_order(self, buy_or_sell, product_type, exchange, tradingsymbol, quantity, discloseqty,
                            price_type, price=0.0, trigger_price=None, retention="DAY", remarks=None):
                return {"stat": "Ok", "norenordno": "1"}

        assert self._place(_OldClient(), None) == {"stat": "Ok", "norenordno": "1"}

    def test_configured_algo_id_is_sent(self):
        client = MagicMock()
        self._place(client, "ALGO-7")
        assert client.place_order.call_args.kwargs["algo_id"] == "ALGO-7"

    def test_unset_algo_id_is_omitted(self):
        client = MagicMock()
        self._place(client, None)
        assert "algo_id" not in client.place_order.call_args.kwargs


# ── Scrip master reload / refresh ───────────────────────────────────────────

class TestMasterReloadKeepsDataOnFailure:

    def test_unreadable_file_keeps_the_in_memory_master(self, monkeypatch, tmp_path):
        """Review finding: a failed read used to swap in an empty master."""
        current = {"NIFTY": {"expiries": ["2099-01-01"]}}
        monkeypatch.setattr(OptionMaster, "_raw", current)
        monkeypatch.setattr(OptionMaster, "_FILE", tmp_path / "missing.json")
        assert OptionMaster.reload() is False
        assert OptionMaster._raw is current

    def test_future_master_unreadable_file_keeps_the_in_memory_master(self, monkeypatch, tmp_path):
        from appconfig import FutureMaster
        current = {"NIFTY": {"expiries": ["2099-01-01"]}}
        monkeypatch.setattr(FutureMaster, "_raw", current)
        monkeypatch.setattr(FutureMaster, "_FILE", tmp_path / "missing.json")
        assert FutureMaster.reload() is False
        assert FutureMaster._raw is current


class TestRefresher:

    def _refresher(self, tmp_path, download, installed):
        return ScripMasterRefresher(
            name="Test", file_path=tmp_path / "master.json", download=download,
            install=installed.append, current_master=lambda: installed[-1] if installed else {},
        )

    def test_download_is_installed_directly_and_persisted(self, tmp_path):
        installed = []
        master = {"NIFTY": {"expiries": ["2099-01-01"]}}
        refresher = self._refresher(tmp_path, lambda: master, installed)

        assert refresher.refresh_now() is True
        assert installed[0]["NIFTY"] == master["NIFTY"]
        assert "_meta" in installed[0]
        assert json.loads((tmp_path / "master.json").read_text(encoding="utf-8"))["NIFTY"] == master["NIFTY"]
        assert not list(tmp_path.glob("*.tmp"))

    def test_failed_disk_write_still_installs_in_memory(self, tmp_path):
        installed = []
        refresher = self._refresher(tmp_path, lambda: {"NIFTY": {"expiries": ["2099-01-01"]}}, installed)
        with patch("appconfig.ScripMasterRefresher.os.replace", side_effect=PermissionError("locked")), \
             patch("appconfig.ScripMasterRefresher.time.sleep"):
            assert refresher.refresh_now() is True
        assert installed and not list(tmp_path.glob("*.tmp"))

    def test_failed_or_empty_download_installs_nothing(self, tmp_path):
        installed = []
        def boom():
            raise ConnectionError("down")
        assert self._refresher(tmp_path, boom, installed).refresh_now() is False
        assert self._refresher(tmp_path, lambda: {"NIFTY": {"expiries": []}}, installed).refresh_now() is False
        assert installed == []


# ── Expiry day: roll after the 15:30 close ──────────────────────────────────

class TestExpiryDayClose:

    EXPIRIES = ["2026-10-06", "2026-10-13"]

    def _at(self, hour, minute):
        return datetime(2026, 10, 6, hour, minute, tzinfo=IST_OFFSET)

    def test_before_close_today_is_still_tradable(self):
        assert upcoming_expiry_dates(self.EXPIRIES, now=self._at(15, 29)) == self.EXPIRIES

    def test_after_close_rolls_to_next_expiry(self):
        assert upcoming_expiry_dates(self.EXPIRIES, now=self._at(15, 30)) == ["2026-10-13"]

    def test_option_master_default_chain_rolls_after_close(self, monkeypatch):
        monkeypatch.setattr(OptionMaster, "_raw", {"NIFTY": {"expiries": self.EXPIRIES}})
        assert OptionMaster.nearest_expiry("nifty", now=self._at(10, 0)) == "2026-10-06"
        assert OptionMaster.nearest_expiry("nifty", now=self._at(16, 0)) == "2026-10-13"

    def test_non_text_inputs_are_not_found_not_errors(self):
        assert OptionMaster.is_valid_underlying(None) is False
        assert OptionMaster.get_expiries(123) == []
        assert OptionMaster.get_strike_chain(None, None) == {}
        assert OptionMaster.find_by_tsym(42) is None


# ── SPAN leg validation ─────────────────────────────────────────────────────

class TestSpanLegValidation:

    @pytest.fixture(autouse=True)
    def _contract(self, monkeypatch):
        contract = {"token": "1", "lot_size": 65, "exchange": "NFO", "underlying": "NIFTY",
                    "expiry": "2099-10-06", "strike": 25000.0, "option_type": "CE"}
        monkeypatch.setattr(OptionMaster, "find_by_tsym", lambda tsym: dict(contract) if tsym == "OK" else None)
        self.contract = contract

    def _reasons(self, *legs):
        _, errors = SpanMarginService().build_positions(list(legs))
        return [error["reason"] for error in errors]

    def test_unknown_side_is_rejected_not_treated_as_buy(self):
        assert self._reasons({"tsym": "OK", "side": "SHORT", "quantity": 65}) == ["invalid_side"]

    def test_quantity_must_be_whole_lots(self):
        assert self._reasons({"tsym": "OK", "side": "BUY", "quantity": 70}) == ["quantity_not_lot_multiple"]

    def test_expired_contract_is_rejected(self):
        self.contract["expiry"] = "2020-01-01"
        assert self._reasons({"tsym": "OK", "side": "BUY", "quantity": 65}) == ["contract_expired"]

    def test_ok_reply_without_numbers_is_not_a_zero_margin(self):
        shoonya = MagicMock()
        shoonya.is_connected = True
        shoonya.get_span_margin.return_value = {"stat": "Ok", "span": "", "expo": "NaN"}
        result, errors = asyncio.run(SpanMarginService().calculate(
            shoonya, [{"tsym": "OK", "side": "SELL", "quantity": 65}]
        ))
        assert result is None and errors[-1]["reason"] == "broker_bad_reply"


# ── Option chain cache ──────────────────────────────────────────────────────

class TestOptionChainOiBaseline:

    def _cache(self):
        chain = {"25000": {"lot_size": 65, "ce_token": "111", "ce_tsym": "X", "pe_token": "222", "pe_tsym": "Y"}}
        return OptionChainCache("NIFTY", "2099-10-06", chain, exchange="NFO")

    def test_blank_previous_oi_does_not_break_later_ticks(self):
        """Found while removing a silent except: poi=None became the baseline
        and every later OI tick failed on oi - None."""
        cache = self._cache()
        asyncio.run(cache.apply_tick("NFO|111", {"oi": 1000, "poi": None, "ltp": 10.0}))
        asyncio.run(cache.apply_tick("NFO|111", {"oi": 1500, "ltp": 11.0}))
        ce = cache.get()["strikes"][0]["ce"]
        assert ce["oi_change"] == 500
        assert ce["ltp"] == 11.0

    def test_previous_day_oi_is_the_baseline_when_sent(self):
        cache = self._cache()
        asyncio.run(cache.apply_tick("NFO|111", {"oi": 1000, "poi": 800}))
        assert cache.get()["strikes"][0]["ce"]["oi_change"] == 200

    def test_malformed_strike_rows_are_skipped(self):
        cache = OptionChainCache("NIFTY", "2099-10-06", {"abc": {"ce_token": "1"}, "25000": "not-a-dict"})
        assert cache.tokens() == set()


# ── Broker number parsing ───────────────────────────────────────────────────

class TestSafeNumbers:

    @pytest.mark.parametrize("raw", [None, "", "abc", "NaN", "inf", "-inf", float("nan"), [], {}])
    def test_unusable_values_give_the_default(self, raw):
        assert safe_float(raw) is None
        assert safe_float(raw, 0.0) == 0.0
        assert safe_int(raw, 0) == 0

    def test_broker_strings_parse(self):
        assert safe_float("101.15") == 101.15
        assert safe_int("75.0") == 75
        assert safe_int(" 65 ") == 65
