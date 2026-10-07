"""
Tests for the pre-algo API fixes: nearest-expiry resolution and master
staleness, option-chain row fields (tsym/token/lot_size/tick_size/ts), the
expiries endpoint, broker SPAN margin, the master-account position book, and
ShoonyaConnection's "no data" vs failure distinction.

No real broker/DB/network calls - every external boundary is mocked.
"""

import asyncio
import re
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from appconfig import OptionMaster
from service.optionChain.OptionChainCache import OptionChainCache
from service.optionChain.OptionChainService import OptionChainService
from service.spanMargin.SpanMarginService import SpanMarginService
from utils.auth_dependency import get_current_user

IST_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+05:30$")


def _weekly_master():
    """A fresh (built 2026-10-04) master: upcoming weeklies plus a far
    quarterly - the shape that used to resolve to 29-Dec once the weeklies in
    a stale file had all expired."""
    def strikes(expiry_code, step):
        return {
            str(k): {
                "lot_size": 65, "tick_size": 0.05,
                "ce_token": f"{expiry_code}{k}1", "ce_tsym": f"NIFTY{expiry_code}C{k}",
                "pe_token": f"{expiry_code}{k}2", "pe_tsym": f"NIFTY{expiry_code}P{k}",
            }
            for k in range(24000, 26001, step)
        }
    return {
        "_meta": {"built_on": "2026-10-04"},
        "NIFTY": {
            "expiries": ["2026-10-06", "2026-10-13", "2026-12-29"],
            "2026-10-06": strikes("06OCT26", 50),
            "2026-10-13": strikes("13OCT26", 50),
            "2026-12-29": strikes("29DEC26", 500),
        },
        "SENSEX": {
            "expiries": ["2026-10-08"],
            "2026-10-08": {"82000": {"lot_size": 20, "tick_size": 0.05,
                                     "ce_token": "S1", "ce_tsym": "SENSEX08OCT26C82000",
                                     "pe_token": "S2", "pe_tsym": "SENSEX08OCT26P82000"}},
        },
    }


@pytest.fixture
def weekly_master(monkeypatch):
    raw = _weekly_master()
    tsym_index, token_index = OptionMaster._build_indexes(raw)
    monkeypatch.setattr(OptionMaster, "_raw", raw)
    monkeypatch.setattr(OptionMaster, "_tsym_index", tsym_index)
    monkeypatch.setattr(OptionMaster, "_token_index", token_index)
    return raw


# ── OptionMaster ────────────────────────────────────────────────────────────

class TestOptionMasterExpiries:

    def test_nearest_expiry_is_the_next_weekly_not_a_far_quarterly(self, weekly_master):
        assert OptionMaster.nearest_expiry("nifty", today=date(2026, 10, 4)) == "2026-10-06"

    def test_expiry_day_itself_counts_as_nearest(self, weekly_master):
        assert OptionMaster.nearest_expiry("nifty", today=date(2026, 10, 6)) == "2026-10-06"

    def test_upcoming_expiries_sorted_and_past_dropped(self, weekly_master):
        assert OptionMaster.upcoming_expiries("NIFTY", today=date(2026, 10, 7)) == [
            "2026-10-13", "2026-12-29",
        ]

    def test_unknown_underlying_has_no_expiries(self, weekly_master):
        assert OptionMaster.upcoming_expiries("midcpnifty") == []
        assert OptionMaster.nearest_expiry("midcpnifty") is None

    def test_master_built_today_with_upcoming_weeklies_is_fresh(self, weekly_master):
        assert OptionMaster.is_stale(today=date(2026, 10, 4)) is False

    def test_master_built_before_today_is_stale(self, weekly_master):
        assert OptionMaster.is_stale(today=date(2026, 10, 5)) is True

    def test_master_with_no_upcoming_expiry_is_stale(self, weekly_master):
        assert OptionMaster.is_stale(today=date(2027, 1, 1)) is True

    def test_empty_master_is_stale(self, monkeypatch):
        monkeypatch.setattr(OptionMaster, "_raw", {})
        assert OptionMaster.is_stale() is True

    def test_unstamped_master_is_stale(self, weekly_master):
        """Review finding: a git checkout/pull gives an old file today's
        mtime - freshness comes from the embedded stamp, never the mtime."""
        del weekly_master["_meta"]
        assert OptionMaster.is_stale(today=date(2026, 10, 4)) is True

    def test_lagging_expired_contract_in_a_fresh_download_is_not_stale(self, weekly_master):
        """Shoonya's BFO master still listed the 2026-10-01 SENSEX expiry on
        2026-10-04 - a just-downloaded master must not count as stale for it
        (and nearest_expiry still skips it)."""
        weekly_master["SENSEX"]["expiries"].insert(0, "2026-10-01")
        assert OptionMaster.is_stale(today=date(2026, 10, 4)) is False
        assert OptionMaster.nearest_expiry("sensex", today=date(2026, 10, 4)) == "2026-10-08"


class TestOptionMasterIndexes:

    def test_find_by_tsym_both_conventions(self, weekly_master):
        native = OptionMaster.find_by_tsym("NIFTY06OCT26C25000")
        suffix = OptionMaster.find_by_tsym("nifty06oct2625000ce")
        assert native == suffix
        assert native["expiry"] == "2026-10-06"
        assert native["strike"] == 25000.0
        assert native["option_type"] == "CE"
        assert native["exchange"] == "NFO"

    def test_find_by_tsym_returns_a_copy(self, weekly_master):
        OptionMaster.find_by_tsym("NIFTY06OCT26C25000")["token"] = "mutated"
        assert OptionMaster.find_by_tsym("NIFTY06OCT26C25000")["token"] == "06OCT26250001"

    def test_aliases_by_tsym_and_token(self, weekly_master):
        assert OptionMaster.find_tsym_aliases("NIFTY06OCT2625000PE") == ["NIFTY06OCT26P25000", "NIFTY06OCT2625000PE"]
        assert OptionMaster.find_tsym_aliases_by_token("06OCT26250002") == ["NIFTY06OCT26P25000", "NIFTY06OCT2625000PE"]

    def test_unknown_lookups_are_empty_not_errors(self, weekly_master):
        assert OptionMaster.find_by_tsym("NOPE") is None
        assert OptionMaster.find_by_tsym(None) is None
        assert OptionMaster.find_tsym_aliases("NOPE") == []
        assert OptionMaster.find_tsym_aliases_by_token("") == []


# ── Option chain rows ───────────────────────────────────────────────────────

class TestOptionChainRowFields:

    def _cache(self):
        chain = {
            "25000": {"lot_size": 65, "tick_size": 0.05, "ce_token": "111", "ce_tsym": "NIFTY06OCT26C25000",
                      "pe_token": "222", "pe_tsym": "NIFTY06OCT26P25000"},
            "25050": {"lot_size": 65, "ce_token": "333", "ce_tsym": "NIFTY06OCT26C25050",
                      "pe_token": "444", "pe_tsym": "NIFTY06OCT26P25050"},
        }
        return OptionChainCache("NIFTY", "2026-10-06", chain, exchange="NFO")

    def test_seeded_leg_carries_contract_fields_and_ist_ts(self):
        cache = self._cache()
        cache.seed_leg("25000", "ce", {"ltp": 120.5, "oi": 1000})
        ce = cache.get()["strikes"][0]["ce"]
        assert ce["tsym"] == "NIFTY06OCT26C25000"
        assert ce["token"] == "111"
        assert ce["lot_size"] == 65
        assert ce["tick_size"] == 0.05
        assert IST_ISO.match(ce["ts"]), ce["ts"]

    def test_tick_updates_ts_and_keeps_contract_fields(self):
        cache = self._cache()
        asyncio.run(cache.apply_tick("NFO|444", {"ltp": 88.0}))
        pe = cache.get()["strikes"][0]["pe"]
        assert pe["tsym"] == "NIFTY06OCT26P25050"
        assert pe["token"] == "444"
        assert pe["tick_size"] == 0.05  # default when the master predates tick_size
        assert pe["ltp"] == 88.0
        assert IST_ISO.match(pe["ts"])

    def test_strikes_ordered_ascending_and_untouched_strikes_skipped(self):
        cache = self._cache()
        asyncio.run(cache.apply_tick("NFO|444", {"ltp": 1.0}))
        asyncio.run(cache.apply_tick("NFO|111", {"ltp": 2.0}))
        assert [row["strike"] for row in cache.get()["strikes"]] == [25000.0, 25050.0]

    def test_malformed_tick_still_wakes_waiters(self):
        cache = self._cache()
        before = cache.generation
        asyncio.run(cache.apply_tick("NFO|111", {"oi": "not-a-number"}))
        assert cache.generation == before + 1


class TestOptionChainServiceExpiry:

    def test_no_expiry_uses_nearest_weekly_with_50_point_strikes(self, weekly_master, monkeypatch):
        monkeypatch.setattr(OptionMaster, "today_ist", lambda: date(2026, 10, 4))
        shoonya = MagicMock()
        shoonya.get_index_quote.return_value = {"ltp": 25010.0}
        shoonya.get_option_quote.return_value = {"ltp": 10.0, "bid": 9.9, "ask": 10.1, "oi": 5, "volume": 1}

        data, errors = asyncio.run(OptionChainService().get_chain(shoonya, "nifty", None))

        assert errors == []
        assert data["expiry"] == "2026-10-06"
        strikes = [row["strike"] for row in data["strikes"]]
        assert {b - a for a, b in zip(strikes, strikes[1:])} == {50.0}
        assert 25000.0 in strikes

    def test_spot_comes_from_tick_cache_before_rest(self, weekly_master):
        service = OptionChainService()
        spot_source = MagicMock()
        spot_source.get_tick.return_value = {"ltp": 25020.0}
        service.set_spot_source(spot_source)
        shoonya = MagicMock()

        spot = asyncio.run(service._resolve_spot(shoonya, "nifty"))

        assert spot == 25020.0
        spot_source.get_tick.assert_called_once_with("NSE|26000")
        shoonya.get_index_quote.assert_not_called()

    def test_window_is_centred_on_spot(self):
        chain = {str(k): {} for k in range(24000, 26001, 50)}
        window = OptionChainService()._window_around_spot(chain, 25010.0)
        keys = sorted(window, key=float)
        assert len(keys) == 41
        assert keys[20] == "25000"


class TestExpiriesEndpoint:

    def _client(self):
        from api.optionChain import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_returns_sorted_upcoming_dates(self, weekly_master, monkeypatch):
        monkeypatch.setattr(OptionMaster, "today_ist", lambda: date(2026, 10, 4))
        resp = self._client().get("/api/market/nifty/expiries")
        assert resp.status_code == 200
        assert resp.json() == ["2026-10-06", "2026-10-13", "2026-12-29"]

    def test_invalid_underlying_is_400(self, weekly_master):
        assert self._client().get("/api/market/dogecoin/expiries").status_code == 400


# ── SPAN margin ─────────────────────────────────────────────────────────────

class TestSpanMarginService:

    def test_legs_are_resolved_and_netted_per_contract(self, weekly_master):
        positions, errors = SpanMarginService().build_positions([
            {"tsym": "NIFTY06OCT26C25000", "side": "SELL", "quantity": 130, "product_type": "NRML"},
            {"tsym": "NIFTY06OCT2625000CE", "side": "BUY", "quantity": 65, "product_type": "NRML"},
            {"tsym": "NIFTY06OCT26C25200", "side": "BUY", "quantity": 130, "product_type": "NRML"},
        ])
        assert errors == []
        by_strike = {p["strprc"]: p for p in positions}
        assert by_strike["25000.00"] == {
            "prd": "M", "exch": "NFO", "instname": "OPTIDX", "symname": "NIFTY",
            "exd": "06-OCT-2026", "optt": "CE", "strprc": "25000.00",
            "buyqty": "65", "sellqty": "130", "netqty": "-65",
        }
        assert by_strike["25200.00"]["netqty"] == "130"

    def test_sensex_uses_the_bfo_scrip_symbol(self, weekly_master):
        """Review finding: BFO's master names Sensex options BSXOPT, not SENSEX."""
        positions, errors = SpanMarginService().build_positions([
            {"tsym": "SENSEX08OCT26C82000", "side": "SELL", "quantity": 20},
        ])
        assert errors == []
        assert positions[0]["exch"] == "BFO"
        assert positions[0]["symname"] == "BSXOPT"
        assert positions[0]["exd"] == "08-OCT-2026"

    def test_one_bad_leg_fails_the_whole_basket(self, weekly_master):
        """Review finding: dropping a mistyped hedge would price the naked
        short as if it were the hedged basket."""
        shoonya = MagicMock()
        shoonya.is_connected = True
        result, errors = asyncio.run(SpanMarginService().calculate(shoonya, [
            {"tsym": "NIFTY06OCT26C25000", "side": "SELL", "quantity": 65},
            {"tsym": "NIFTY06OCT26C2520", "side": "BUY", "quantity": 65},
        ]))
        assert result is None
        assert errors == [{"index": 1, "tsym": "NIFTY06OCT26C2520", "reason": "unknown_contract"}]
        shoonya.get_span_margin.assert_not_called()

    def test_unknown_and_invalid_legs_are_reported(self, weekly_master):
        positions, errors = SpanMarginService().build_positions([
            {"tsym": "NOPE", "side": "BUY", "quantity": 65},
            {"tsym": "NIFTY06OCT26C25000", "side": "BUY", "quantity": 65, "product_type": "CNC"},
        ])
        assert positions == []
        assert [e["reason"] for e in errors] == ["unknown_contract", "unsupported_product_type"]

    def test_calculate_returns_span_exposure_total(self, weekly_master):
        shoonya = MagicMock()
        shoonya.is_connected = True
        shoonya.get_span_margin.return_value = {"stat": "Ok", "span": "41250.00", "expo": "9870.50"}

        result, errors = asyncio.run(SpanMarginService().calculate(
            shoonya, [{"tsym": "NIFTY06OCT26C25000", "side": "SELL", "quantity": 65}]
        ))

        assert errors == []
        assert result["span"] == 41250.0
        assert result["exposure"] == 9870.5
        assert result["total_margin"] == 51120.5

    def test_broker_reject_and_disconnect_are_errors_not_exceptions(self, weekly_master):
        legs = [{"tsym": "NIFTY06OCT26C25000", "side": "SELL", "quantity": 65}]
        shoonya = MagicMock()
        shoonya.is_connected = True
        shoonya.get_span_margin.return_value = {"stat": "Not_Ok", "emsg": "Invalid input"}
        result, errors = asyncio.run(SpanMarginService().calculate(shoonya, legs))
        assert result is None and errors[-1]["reason"] == "broker_rejected"

        result, errors = asyncio.run(SpanMarginService().calculate(None, legs))
        assert result is None and errors[-1]["reason"] == "shoonya_disconnected"


class TestMarginEndpoint:

    def _client(self, shoonya):
        from api.margin import router
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: {"user_id": 42}
        app.state.shoonya = shoonya
        return TestClient(app)

    def test_success(self, weekly_master):
        shoonya = MagicMock()
        shoonya.is_connected = True
        shoonya.get_span_margin.return_value = {"stat": "Ok", "span": "100", "expo": "50"}
        resp = self._client(shoonya).post("/api/market/margin", json={"legs": [
            {"tsym": "NIFTY06OCT26C25000", "side": "SELL", "quantity": 65},
        ]})
        assert resp.status_code == 200
        assert resp.json()["total_margin"] == 150.0

    def test_unknown_contract_is_400(self, weekly_master):
        shoonya = MagicMock()
        shoonya.is_connected = True
        resp = self._client(shoonya).post("/api/market/margin", json={"legs": [
            {"tsym": "NOPE", "side": "SELL", "quantity": 65},
        ]})
        assert resp.status_code == 400

    def test_partially_valid_basket_is_400_not_a_partial_margin(self, weekly_master):
        shoonya = MagicMock()
        shoonya.is_connected = True
        resp = self._client(shoonya).post("/api/market/margin", json={"legs": [
            {"tsym": "NIFTY06OCT26C25000", "side": "SELL", "quantity": 65},
            {"tsym": "NOPE", "side": "BUY", "quantity": 65},
        ]})
        assert resp.status_code == 400
        assert resp.json()["detail"]["errors"][0]["index"] == 1

    def test_disconnected_is_503(self, weekly_master):
        shoonya = MagicMock()
        shoonya.is_connected = False
        resp = self._client(shoonya).post("/api/market/margin", json={"legs": [
            {"tsym": "NIFTY06OCT26C25000", "side": "SELL", "quantity": 65},
        ]})
        assert resp.status_code == 503

    def test_empty_legs_rejected(self, weekly_master):
        assert self._client(MagicMock()).post("/api/market/margin", json={"legs": []}).status_code == 422


# ── Master-account position book ────────────────────────────────────────────

class TestMasterPositionBookEndpoint:

    @pytest.fixture(autouse=True)
    def _admin_configured(self, monkeypatch):
        monkeypatch.setenv("ADMIN_USER_IDS", "1, 9")

    def _client(self, shoonya, user_id=1):
        from api.admin_shoonya import router
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: {"user_id": user_id}
        app.state.shoonya = shoonya
        return TestClient(app)

    def test_non_admin_user_is_403(self):
        """Review finding: any logged-in user could read the master book."""
        shoonya = MagicMock()
        shoonya.is_connected = True
        assert self._client(shoonya, user_id=42).get("/admin/shoonya/positions").status_code == 403
        shoonya.get_position_book.assert_not_called()

    def test_whole_admin_router_is_admin_only(self):
        assert self._client(MagicMock(), user_id=42).get("/admin/shoonya/status").status_code == 403
        assert self._client(MagicMock(), user_id=42).get("/admin/shoonya/auth-url").status_code == 403

    def test_unset_allowlist_allows_any_logged_in_user(self, monkeypatch):
        """Current choice: with ADMIN_USER_IDS empty the admin routes are open
        to every logged-in user; the allowlist only applies once it's set."""
        monkeypatch.delenv("ADMIN_USER_IDS")
        shoonya = MagicMock()
        shoonya.is_connected = True
        shoonya.get_position_book.return_value = []
        assert self._client(shoonya, user_id=42).get("/admin/shoonya/positions").status_code == 200
        monkeypatch.setenv("ADMIN_USER_IDS", "  ")
        assert self._client(shoonya, user_id=42).get("/admin/shoonya/status").status_code == 200

    def test_admin_routes_still_require_login(self, monkeypatch):
        from api.admin_shoonya import router
        monkeypatch.delenv("ADMIN_USER_IDS")
        app = FastAPI()
        app.include_router(router)
        assert TestClient(app).get("/admin/shoonya/status").status_code in (401, 403)

    def test_returns_broker_rows_unchanged(self):
        rows = [{"tsym": "NIFTY06OCT26C25000", "netqty": "-65", "netavgprc": "120.50", "rpnl": "0"}]
        shoonya = MagicMock()
        shoonya.is_connected = True
        shoonya.get_position_book.return_value = rows
        body = self._client(shoonya).get("/admin/shoonya/positions").json()
        assert body["positions"] == rows
        assert body["count"] == 1
        assert IST_ISO.match(body["as_of"])

    def test_empty_book_is_an_empty_list(self):
        shoonya = MagicMock()
        shoonya.is_connected = True
        shoonya.get_position_book.return_value = []
        body = self._client(shoonya).get("/admin/shoonya/positions").json()
        assert body["positions"] == [] and body["count"] == 0

    def test_broker_failure_is_502_and_disconnect_503(self):
        shoonya = MagicMock()
        shoonya.is_connected = True
        shoonya.get_position_book.return_value = None
        assert self._client(shoonya).get("/admin/shoonya/positions").status_code == 502
        shoonya.is_connected = False
        assert self._client(shoonya).get("/admin/shoonya/positions").status_code == 503


# ── ShoonyaConnection account calls ─────────────────────────────────────────

class TestShoonyaConnectionAccountCalls:

    def _connection(self, reply=None, raises=None):
        from marketengine.ShoonyaConnection import ShoonyaConnection
        conn = ShoonyaConnection.__new__(ShoonyaConnection)
        conn._connected = True
        conn._api = MagicMock()
        if raises is not None:
            conn._api.post_jdata.side_effect = raises
        else:
            conn._api.post_jdata.return_value = reply
        return conn

    def test_list_reply_is_returned(self):
        assert self._connection([{"tsym": "X"}]).get_position_book() == [{"tsym": "X"}]

    def test_no_data_reply_is_an_empty_list_not_a_failure(self):
        conn = self._connection({"stat": "Not_Ok", "emsg": "Error Occurred : 5 \"no data\""})
        assert conn.get_position_book() == []
        assert conn.get_order_book() == []

    def test_other_errors_are_none(self):
        assert self._connection({"stat": "Not_Ok", "emsg": "Session Expired"}).get_position_book() is None
        assert self._connection(raises=ConnectionError("down")).get_order_book() is None

    def test_order_history_sends_norenordno_without_actid(self):
        conn = self._connection([{"status": "OPEN"}])
        assert conn.get_order_history("123") == [{"status": "OPEN"}]
        args, kwargs = conn._api.post_jdata.call_args
        assert args == ("singleorderhistory", {"norenordno": "123"})
        assert kwargs == {"with_account": False, "with_source": True}

    def test_disconnected_never_calls_broker(self):
        conn = self._connection([])
        conn._connected = False
        assert conn.get_position_book() is None
        conn._api.post_jdata.assert_not_called()


# ── algo_id guard ───────────────────────────────────────────────────────────

class TestAlgoIdGuard:

    def test_installed_noren_place_order_accepts_algo_id(self):
        """The real library takes algo_id - guards against an upgrade dropping it."""
        import inspect
        noren = pytest.importorskip("NorenRestApiPy.NorenApi")
        assert "algo_id" in inspect.signature(noren.NorenApi.place_order).parameters

    def test_client_without_algo_id_is_refused_before_any_broker_call(self):
        from service.shoonyaOrderService import ShoonyaOrderMappingError, ShoonyaOrderService

        class _OldClient:
            calls = []

            def place_order(self, buy_or_sell, product_type, exchange, tradingsymbol, quantity, discloseqty,
                            price_type, price=0.0, trigger_price=None, retention="DAY", remarks=None):
                self.calls.append(tradingsymbol)

        client = _OldClient()
        with pytest.raises(ShoonyaOrderMappingError):
            ShoonyaOrderService(client).place_order(
                side="BUY", product_type="NRML", exchange="NFO", tradingsymbol="X",
                quantity=65, order_type="LIMIT", price=1.0, algo_id="ALGO-7",
            )
        assert client.calls == []

    def test_mapping_error_is_a_clean_reject_not_uncertain(self):
        from service.liveOrderRoutingService import LiveOrderRejectedError, LiveOrderRoutingService
        from service.shoonyaOrderService import ShoonyaOrderMappingError

        with patch("service.liveOrderRoutingService.OrderPersistence"), \
             patch("service.liveOrderRoutingService.ShoonyaOrderService") as MockShoonyaOrderService:
            service = LiveOrderRoutingService(shoonya_api=MagicMock())
        MockShoonyaOrderService.return_value.place_order.side_effect = ShoonyaOrderMappingError("no algo_id")
        order = MagicMock(symbol="X", quantity=65, price=1.0, trigger_price=None, client_order_id="a1")

        with pytest.raises(LiveOrderRejectedError):
            service.place_live_order(order, 101, {"lot_size": 65}, algo_id="ALGO-7")
