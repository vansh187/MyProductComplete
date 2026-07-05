"""
Endpoint-level tests for api/optionChain.py.
All DB/Shoonya calls are mocked - no live network, no real master file needed
(patches OptionMaster and the module-level service directly).
"""

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.optionChain import router


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _fake_shoonya():
    shoonya = MagicMock()
    shoonya.is_connected = True
    return shoonya


class TestGetOptionChainEndpoint:

    def test_success_response_shape(self):
        app = _make_app()
        app.state.shoonya = _fake_shoonya()
        with patch("api.optionChain.OptionMaster") as mock_master, \
             patch("api.optionChain._optionChainService.get_chain", return_value=(
                 {"expiry": "2026-07-07", "spot": 24270.85, "strikes": [
                     {"strike": 24300.0, "ce": {"ltp": 62.16}, "pe": {"ltp": 42.16}}
                 ]},
                 [],
             )):
            mock_master.is_valid_underlying.return_value = True

            client = TestClient(app)
            resp = client.get("/api/market/nifty/optionchain")

        assert resp.status_code == 200
        body = resp.json()
        assert body["symbol"] == "NIFTY"
        assert body["exchange"] == "NFO"
        assert body["expiry"] == "2026-07-07"
        assert body["spot"] == 24270.85
        assert len(body["strikes"]) == 1
        assert body["errors"] == []
        assert "last_updated" in body

    def test_invalid_underlying_returns_400(self):
        app = _make_app()
        with patch("api.optionChain.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = False
            client = TestClient(app)
            resp = client.get("/api/market/dowjones/optionchain")

        assert resp.status_code == 400

    def test_invalid_expiry_format_returns_400(self):
        app = _make_app()
        with patch("api.optionChain.OptionMaster") as mock_master:
            mock_master.is_valid_underlying.return_value = True
            client = TestClient(app)
            resp = client.get("/api/market/nifty/optionchain?expiry=not-a-date")

        assert resp.status_code == 400

    def test_shoonya_disconnected_returns_503_when_nothing_cached(self):
        """No prior cache exists for this chain in this process, so a
        disconnected broker session has nothing to fall back to."""
        app = _make_app()
        disconnected = MagicMock()
        disconnected.is_connected = False
        app.state.shoonya = disconnected
        with patch("api.optionChain.OptionMaster") as mock_master, \
             patch("api.optionChain._optionChainService.peek_cached_chain", return_value=(None, None)):
            mock_master.is_valid_underlying.return_value = True

            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/market/nifty/optionchain")

        assert resp.status_code == 503

    def test_shoonya_disconnected_serves_cached_chain_instead_of_503(self):
        """A disconnected broker session must still serve whatever this
        process last cached for the chain, tagged with a disconnect reason,
        so the frontend can keep showing last-traded data."""
        app = _make_app()
        disconnected = MagicMock()
        disconnected.is_connected = False
        app.state.shoonya = disconnected
        cached_chain = {
            "expiry": "2026-07-07", "spot": 24270.85,
            "strikes": [{"strike": 24300.0, "ce": {"ltp": 62.16}, "pe": {"ltp": 42.16}}],
        }
        with patch("api.optionChain.OptionMaster") as mock_master, \
             patch("api.optionChain._optionChainService.peek_cached_chain",
                   return_value=(cached_chain, "2026-07-07")):
            mock_master.is_valid_underlying.return_value = True

            client = TestClient(app)
            resp = client.get("/api/market/nifty/optionchain")

        assert resp.status_code == 200
        body = resp.json()
        assert body["spot"] == 24270.85
        assert len(body["strikes"]) == 1
        assert body["errors"] == [{"reason": "shoonya_disconnected"}]

    def test_error_reason_propagated(self):
        app = _make_app()
        app.state.shoonya = _fake_shoonya()
        with patch("api.optionChain.OptionMaster") as mock_master, \
             patch("api.optionChain._optionChainService.get_chain", return_value=(
                 None, [{"reason": "no_expiry_available"}],
             )):
            mock_master.is_valid_underlying.return_value = True

            client = TestClient(app)
            resp = client.get("/api/market/nifty/optionchain")

        assert resp.status_code == 200
        body = resp.json()
        assert body["strikes"] == []
        assert body["errors"] == [{"reason": "no_expiry_available"}]
