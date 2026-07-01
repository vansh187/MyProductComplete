"""
Unit tests for OHLC candle endpoints.
All DB calls, Shoonya, and broker APIs are fully mocked.
"""

from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.candles import router
from service.candleService.CandleService import CandleService


# ── App fixture ──────────────────────────────────────────────────────────────

def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


_FAKE_USER = {"user_id": 42, "email": "test@example.com"}


def _override_auth():
    return _FAKE_USER


# ── Candle Service Tests ─────────────────────────────────────────────────────

class TestCandleService:

    def test_normalize_interval_1m(self):
        """1m → 1minute"""
        result = CandleService._normalize_interval("1m")
        assert result == "1minute"

    def test_normalize_interval_5m(self):
        """5m → 5minute"""
        result = CandleService._normalize_interval("5m")
        assert result == "5minute"

    def test_normalize_interval_1h(self):
        """1h → 1hour"""
        result = CandleService._normalize_interval("1h")
        assert result == "1hour"

    def test_normalize_interval_1d(self):
        """1d → 1day"""
        result = CandleService._normalize_interval("1d")
        assert result == "1day"

    def test_normalize_interval_invalid_defaults_to_1minute(self):
        """Unknown interval defaults to 1minute"""
        result = CandleService._normalize_interval("30m")
        assert result == "1minute"

    def test_format_candle_valid(self):
        """Candle dict is normalized and rounded correctly"""
        raw = {
            "timestamp": "2026-07-01T09:15:00+05:30",
            "open": 24865.756789,
            "high": 24880.201234,
            "low": 24850.105678,
            "close": 24870.506789,
            "volume": 50000,
        }
        result = CandleService._format_candle(raw)
        assert result["timestamp"] == "2026-07-01T09:15:00+05:30"
        assert result["open"] == 24865.76
        assert result["high"] == 24880.2
        assert result["low"] == 24850.11
        assert result["close"] == 24870.51
        assert result["volume"] == 50000

    def test_format_candle_missing_fields_defaults_to_zero(self):
        """Missing OHLC fields default to 0"""
        raw = {"timestamp": "2026-07-01T09:15:00+05:30"}
        result = CandleService._format_candle(raw)
        assert result["open"] == 0.0
        assert result["high"] == 0.0
        assert result["low"] == 0.0
        assert result["close"] == 0.0
        assert result["volume"] == 0


# ── GET /api/market/nifty/candles ───────────────────────────────────────────

class TestNiftyCandles:

    def test_nifty_candles_1m_success(self):
        """Fetch 1m candles — returns data"""
        app = _make_app()

        with patch("api.candles._candleService.get_index_candles", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (
                [
                    {"timestamp": "2026-07-01T09:15:00+05:30", "open": 24865.75, "high": 24880.20, "low": 24850.10, "close": 24870.50, "volume": 50000},
                    {"timestamp": "2026-07-01T09:16:00+05:30", "open": 24870.50, "high": 24895.00, "low": 24865.30, "close": 24888.15, "volume": 55000},
                ],
                []
            )

            with patch("api.candles._get_shoonya") as mock_shoonya:
                mock_shoonya_conn = MagicMock()
                mock_shoonya_conn.is_connected = True
                mock_shoonya.return_value = mock_shoonya_conn

                client = TestClient(app)
                resp = client.get("/api/market/nifty/candles?timeframe=1m&limit=100")

        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "NIFTY"
        assert data["timeframe"] == "1m"
        assert len(data["candles"]) == 2
        assert data["errors"] == []
        assert "last_updated" in data

    def test_nifty_candles_5m(self):
        """Fetch 5m candles"""
        app = _make_app()

        with patch("api.candles._candleService.get_index_candles", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (
                [{"timestamp": "2026-07-01T09:15:00+05:30", "open": 24865.75, "high": 24880.20, "low": 24850.10, "close": 24870.50, "volume": 250000}],
                []
            )

            with patch("api.candles._get_shoonya") as mock_shoonya:
                mock_shoonya_conn = MagicMock()
                mock_shoonya_conn.is_connected = True
                mock_shoonya.return_value = mock_shoonya_conn

                client = TestClient(app)
                resp = client.get("/api/market/nifty/candles?timeframe=5m&limit=50")

        assert resp.status_code == 200
        assert resp.json()["timeframe"] == "5m"

    def test_nifty_candles_invalid_timeframe(self):
        """Invalid timeframe returns 400"""
        app = _make_app()

        with patch("api.candles._get_shoonya"):
            client = TestClient(app)
            resp = client.get("/api/market/nifty/candles?timeframe=30m")

        assert resp.status_code == 400
        assert "Invalid timeframe" in resp.json()["detail"]

    def test_nifty_candles_shoonya_disconnected(self):
        """Shoonya disconnected returns 503"""
        from fastapi import HTTPException

        app = _make_app()

        with patch("api.candles._get_shoonya") as mock_shoonya:
            mock_shoonya.side_effect = HTTPException(status_code=503, detail="Market data service is not ready. Try again shortly.")

            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/market/nifty/candles?timeframe=1m")

        assert resp.status_code == 503

    def test_nifty_candles_no_data_error(self):
        """When broker returns no candles, error is recorded"""
        app = _make_app()

        with patch("api.candles._candleService.get_index_candles", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (
                [],
                [{"exchange": "NSE", "token": "26000", "timeframe": "1m", "reason": "no_candle_data"}]
            )

            with patch("api.candles._get_shoonya") as mock_shoonya:
                mock_shoonya_conn = MagicMock()
                mock_shoonya_conn.is_connected = True
                mock_shoonya.return_value = mock_shoonya_conn

                client = TestClient(app)
                resp = client.get("/api/market/nifty/candles?timeframe=1m")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["candles"]) == 0
        assert len(data["errors"]) == 1
        assert data["errors"][0]["reason"] == "no_candle_data"

    def test_nifty_candles_limit_boundary(self):
        """Limit boundaries: min=1, max=500"""
        app = _make_app()

        with patch("api.candles._candleService.get_index_candles", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = ([], [])
            with patch("api.candles._get_shoonya") as mock_shoonya:
                mock_shoonya_conn = MagicMock()
                mock_shoonya_conn.is_connected = True
                mock_shoonya.return_value = mock_shoonya_conn

                client = TestClient(app)

                # Valid: limit=1
                resp = client.get("/api/market/nifty/candles?limit=1")
                assert resp.status_code == 200

                # Valid: limit=500
                resp = client.get("/api/market/nifty/candles?limit=500")
                assert resp.status_code == 200

                # Invalid: limit=0
                resp = client.get("/api/market/nifty/candles?limit=0")
                assert resp.status_code == 422

                # Invalid: limit=501
                resp = client.get("/api/market/nifty/candles?limit=501")
                assert resp.status_code == 422


# ── GET /api/market/banknifty/candles ────────────────────────────────────────

class TestBankNiftyCandles:

    def test_banknifty_candles_returns_correct_symbol(self):
        """Bank Nifty endpoint returns symbol='BANKNIFTY'"""
        app = _make_app()

        with patch("api.candles._candleService.get_index_candles", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (
                [{"timestamp": "2026-07-01T09:15:00+05:30", "open": 57573.35, "high": 57927.7, "low": 57487.85, "close": 57861.8, "volume": 100000}],
                []
            )

            with patch("api.candles._get_shoonya") as mock_shoonya:
                mock_shoonya_conn = MagicMock()
                mock_shoonya_conn.is_connected = True
                mock_shoonya.return_value = mock_shoonya_conn

                client = TestClient(app)
                resp = client.get("/api/market/banknifty/candles?timeframe=1h")

        assert resp.status_code == 200
        assert resp.json()["symbol"] == "BANKNIFTY"
        assert resp.json()["timeframe"] == "1h"


# ── GET /api/market/finnifty/candles ─────────────────────────────────────────

class TestFinNiftyCandles:

    def test_finnifty_candles_returns_correct_symbol(self):
        """Fin Nifty endpoint returns symbol='FINNIFTY'"""
        app = _make_app()

        with patch("api.candles._candleService.get_index_candles", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (
                [{"timestamp": "2026-07-01T09:15:00+05:30", "open": 26553.75, "high": 26733.1, "low": 26527.9, "close": 26716.85, "volume": 75000}],
                []
            )

            with patch("api.candles._get_shoonya") as mock_shoonya:
                mock_shoonya_conn = MagicMock()
                mock_shoonya_conn.is_connected = True
                mock_shoonya.return_value = mock_shoonya_conn

                client = TestClient(app)
                resp = client.get("/api/market/finnifty/candles?timeframe=1d")

        assert resp.status_code == 200
        assert resp.json()["symbol"] == "FINNIFTY"
        assert resp.json()["timeframe"] == "1d"


# ── Response structure validation ────────────────────────────────────────────

class TestCandlesResponseStructure:

    def test_response_always_has_required_fields(self):
        """Every response has symbol, timeframe, candles, errors, last_updated"""
        app = _make_app()

        with patch("api.candles._candleService.get_index_candles", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = ([], [])

            with patch("api.candles._get_shoonya") as mock_shoonya:
                mock_shoonya_conn = MagicMock()
                mock_shoonya_conn.is_connected = True
                mock_shoonya.return_value = mock_shoonya_conn

                client = TestClient(app)
                resp = client.get("/api/market/nifty/candles")

        assert resp.status_code == 200
        data = resp.json()
        assert "symbol" in data
        assert "exchange" in data
        assert "timeframe" in data
        assert "candles" in data
        assert "errors" in data
        assert "last_updated" in data

    def test_candle_object_structure(self):
        """Each candle has timestamp, open, high, low, close, volume"""
        app = _make_app()

        candle = {
            "timestamp": "2026-07-01T09:15:00+05:30",
            "open": 24865.75,
            "high": 24880.20,
            "low": 24850.10,
            "close": 24870.50,
            "volume": 50000
        }

        with patch("api.candles._candleService.get_index_candles", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = ([candle], [])

            with patch("api.candles._get_shoonya") as mock_shoonya:
                mock_shoonya_conn = MagicMock()
                mock_shoonya_conn.is_connected = True
                mock_shoonya.return_value = mock_shoonya_conn

                client = TestClient(app)
                resp = client.get("/api/market/nifty/candles")

        assert resp.status_code == 200
        c = resp.json()["candles"][0]
        assert "timestamp" in c
        assert "open" in c
        assert "high" in c
        assert "low" in c
        assert "close" in c
        assert "volume" in c
