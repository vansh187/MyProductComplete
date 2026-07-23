"""
Unit tests for OHLC candle endpoints (api/candles.py) and the backing
CandleService. All DB calls, Shoonya, and broker APIs are fully mocked.
"""

import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.candles import router
from service.candleService.CandleService import CandleService


# ── App fixture ──────────────────────────────────────────────────────────────

def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


# ── CandleService unit tests ─────────────────────────────────────────────────

class TestCandleServiceNormalizeInterval:

    def test_normalize_interval_1m(self):
        assert CandleService._normalize_interval("1m") == "1"

    def test_normalize_interval_3m(self):
        assert CandleService._normalize_interval("3m") == "3"

    def test_normalize_interval_5m(self):
        assert CandleService._normalize_interval("5m") == "5"

    def test_normalize_interval_15m(self):
        assert CandleService._normalize_interval("15m") == "15"

    def test_normalize_interval_1h(self):
        assert CandleService._normalize_interval("1h") == "60"

    def test_normalize_interval_1d_unsupported(self):
        """1d is not available via TPSeries (minute-granularity only)."""
        assert CandleService._normalize_interval("1d") is None

    def test_normalize_interval_invalid_returns_none(self):
        assert CandleService._normalize_interval("30m") is None

    def test_supported_timeframes_matches_interval_keys(self):
        assert set(CandleService.SUPPORTED_TIMEFRAMES) == {"1m", "3m", "5m", "15m", "1h"}


class TestFormatCandle:

    def test_format_candle_valid(self):
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
        raw = {"timestamp": "2026-07-01T09:15:00+05:30"}
        result = CandleService._format_candle(raw)
        assert result["open"] == 0.0
        assert result["high"] == 0.0
        assert result["low"] == 0.0
        assert result["close"] == 0.0
        assert result["volume"] == 0


class TestFilterToLastTradingDay:

    def test_filter_to_last_trading_day_drops_stale_days(self):
        """When the lookback window spans a weekend, only the most recent
        day's candles (e.g. Friday) should survive, not a blend with Thursday."""
        raw = [
            {"timestamp": "02-07-2026 15:29:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},  # Thursday
            {"timestamp": "03-07-2026 09:15:00", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},  # Friday
            {"timestamp": "03-07-2026 09:16:00", "open": 3, "high": 3, "low": 3, "close": 3, "volume": 3},  # Friday
        ]
        result = CandleService._filter_to_last_trading_day(raw)
        assert len(result) == 2
        assert all(c["timestamp"].startswith("03-07-2026") for c in result)

    def test_filter_to_last_trading_day_single_day_unaffected(self):
        raw = [
            {"timestamp": "03-07-2026 09:15:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            {"timestamp": "03-07-2026 09:16:00", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
        ]
        result = CandleService._filter_to_last_trading_day(raw)
        assert result == raw

    def test_filter_to_last_trading_day_empty_input(self):
        assert CandleService._filter_to_last_trading_day([]) == []

    def test_filter_to_last_trading_day_skips_unparseable_timestamps(self):
        raw = [
            {"timestamp": "garbage", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            {"timestamp": "03-07-2026 09:15:00", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
        ]
        result = CandleService._filter_to_last_trading_day(raw)
        assert result == [raw[1]]


class TestGetIndexCandles:

    def test_get_index_candles_returns_last_trading_day_over_weekend(self):
        """Simulates opening the app on a Sunday: broker (given a wide lookback)
        returns Friday's candles even though 'today' has no data, and the
        service must surface them instead of reporting no_candle_data."""
        friday_candles = [
            {"timestamp": "03-07-2026 09:15:00", "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000},
            {"timestamp": "03-07-2026 09:16:00", "open": 100.5, "high": 102, "low": 100, "close": 101.5, "volume": 1200},
        ]

        fake_shoonya = MagicMock()
        fake_shoonya.is_connected = True
        fake_shoonya.get_time_price_series.return_value = friday_candles

        service = CandleService()
        candles, errors = asyncio.run(
            service.get_index_candles(fake_shoonya, "NSE", "26000", "1m", limit=100)
        )

        assert errors == []
        assert len(candles) == 2
        # Lookback window must be wide enough to survive a closed weekend,
        # not just "1 day back from now".
        _, kwargs = fake_shoonya.get_time_price_series.call_args
        assert kwargs["days"] > 1

    def test_get_index_candles_shoonya_disconnected(self):
        fake_shoonya = MagicMock()
        fake_shoonya.is_connected = False

        service = CandleService()
        candles, errors = asyncio.run(
            service.get_index_candles(fake_shoonya, "NSE", "26000", "1m", limit=100)
        )

        assert candles == []
        assert errors == [{"reason": "shoonya_disconnected"}]

    def test_get_index_candles_shoonya_none(self):
        service = CandleService()
        candles, errors = asyncio.run(
            service.get_index_candles(None, "NSE", "26000", "1m", limit=100)
        )
        assert candles == []
        assert errors == [{"reason": "shoonya_disconnected"}]

    def test_get_index_candles_unsupported_timeframe(self):
        fake_shoonya = MagicMock()
        fake_shoonya.is_connected = True

        service = CandleService()
        candles, errors = asyncio.run(
            service.get_index_candles(fake_shoonya, "NSE", "26000", "30m", limit=100)
        )

        assert candles == []
        assert errors == [{
            "exchange": "NSE", "token": "26000", "timeframe": "30m",
            "reason": "interval_not_supported_by_broker",
        }]
        fake_shoonya.get_time_price_series.assert_not_called()

    def test_get_index_candles_no_data_from_broker(self):
        fake_shoonya = MagicMock()
        fake_shoonya.is_connected = True
        fake_shoonya.get_time_price_series.return_value = None

        service = CandleService()
        candles, errors = asyncio.run(
            service.get_index_candles(fake_shoonya, "NSE", "26000", "1m", limit=100)
        )

        assert candles == []
        assert errors == [{
            "exchange": "NSE", "token": "26000", "timeframe": "1m",
            "reason": "no_candle_data",
        }]

    def test_get_index_candles_trims_to_limit_keeping_most_recent(self):
        raw = [
            {"timestamp": f"03-07-2026 09:{i:02d}:00", "open": i, "high": i, "low": i, "close": i, "volume": i}
            for i in range(10, 20)
        ]
        fake_shoonya = MagicMock()
        fake_shoonya.is_connected = True
        fake_shoonya.get_time_price_series.return_value = raw

        service = CandleService()
        candles, errors = asyncio.run(
            service.get_index_candles(fake_shoonya, "NSE", "26000", "1m", limit=3)
        )

        assert errors == []
        assert len(candles) == 3
        # Most recent 3 of the 10 candles (09:17, 09:18, 09:19)
        assert candles[0]["timestamp"] == "03-07-2026 09:17:00"
        assert candles[-1]["timestamp"] == "03-07-2026 09:19:00"

    def test_get_index_candles_broker_exception_is_captured_as_error(self):
        fake_shoonya = MagicMock()
        fake_shoonya.is_connected = True
        fake_shoonya.get_time_price_series.side_effect = RuntimeError("boom")

        service = CandleService()
        candles, errors = asyncio.run(
            service.get_index_candles(fake_shoonya, "NSE", "26000", "1m", limit=100)
        )

        assert candles == []
        assert len(errors) == 1
        assert errors[0]["reason"] == "boom"


# ── GET /api/market/nifty/candles ───────────────────────────────────────────

class TestNiftyCandles:

    def test_nifty_candles_1m_success(self):
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
        assert data["exchange"] == "NSE"
        assert data["timeframe"] == "1m"
        assert len(data["candles"]) == 2
        assert data["errors"] == []
        assert "last_updated" in data

        mock_fetch.assert_called_once()
        _, kwargs = mock_fetch.call_args
        assert kwargs["exchange"] == "NSE"
        assert kwargs["token"] == "26000"
        assert kwargs["timeframe"] == "1m"
        assert kwargs["limit"] == 100

    def test_nifty_candles_5m(self):
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
        app = _make_app()

        with patch("api.candles._get_shoonya"):
            client = TestClient(app)
            resp = client.get("/api/market/nifty/candles?timeframe=30m")

        assert resp.status_code == 400
        assert "Invalid timeframe" in resp.json()["detail"]

    @pytest.mark.parametrize("timeframe", ["5s", "10s", "1d"])
    def test_nifty_candles_broker_unsupported_timeframe_rejected(self, timeframe):
        """5s/10s/1d aren't fulfillable via Shoonya's TPSeries (minute-only
        granularity) so they must be rejected at validation, not silently
        return an empty candle list."""
        app = _make_app()

        with patch("api.candles._get_shoonya"):
            client = TestClient(app)
            resp = client.get(f"/api/market/nifty/candles?timeframe={timeframe}")

        assert resp.status_code == 400
        assert "Invalid timeframe" in resp.json()["detail"]

    def test_nifty_candles_default_timeframe_is_1m(self):
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
        assert resp.json()["timeframe"] == "1m"

    def test_nifty_candles_shoonya_disconnected(self):
        app = _make_app()

        with patch("api.candles._get_shoonya") as mock_shoonya:
            mock_shoonya.side_effect = HTTPException(status_code=503, detail="Market data service is not ready. Try again shortly.")

            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/market/nifty/candles?timeframe=1m")

        assert resp.status_code == 503

    def test_nifty_candles_no_data_error(self):
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
        app = _make_app()

        with patch("api.candles._candleService.get_index_candles", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = ([], [])
            with patch("api.candles._get_shoonya") as mock_shoonya:
                mock_shoonya_conn = MagicMock()
                mock_shoonya_conn.is_connected = True
                mock_shoonya.return_value = mock_shoonya_conn

                client = TestClient(app)

                resp = client.get("/api/market/nifty/candles?limit=1")
                assert resp.status_code == 200

                resp = client.get("/api/market/nifty/candles?limit=500")
                assert resp.status_code == 200

                resp = client.get("/api/market/nifty/candles?limit=0")
                assert resp.status_code == 422

                resp = client.get("/api/market/nifty/candles?limit=501")
                assert resp.status_code == 422


# ── GET /api/market/banknifty/candles ────────────────────────────────────────

class TestBankNiftyCandles:

    def test_banknifty_candles_returns_correct_symbol_and_token(self):
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
        assert resp.json()["exchange"] == "NSE"
        assert resp.json()["timeframe"] == "1h"

        _, kwargs = mock_fetch.call_args
        assert kwargs["token"] == "26009"


# ── GET /api/market/finnifty/candles ─────────────────────────────────────────

class TestFinNiftyCandles:

    def test_finnifty_candles_returns_correct_symbol_and_token(self):
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
                resp = client.get("/api/market/finnifty/candles?timeframe=1h")

        assert resp.status_code == 200
        assert resp.json()["symbol"] == "FINNIFTY"
        assert resp.json()["exchange"] == "NSE"
        assert resp.json()["timeframe"] == "1h"

        _, kwargs = mock_fetch.call_args
        assert kwargs["token"] == "26037"


# ── GET /api/market/sensex/candles ──────────────────────────────────────────

class TestSensexCandles:

    def test_sensex_candles_returns_correct_symbol_and_exchange(self):
        app = _make_app()

        with patch("api.candles._candleService.get_index_candles", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (
                [{"timestamp": "2026-07-01T09:15:00+05:30", "open": 76545.21, "high": 77019.8, "low": 76538.37, "close": 76967.87, "volume": 200000}],
                []
            )

            with patch("api.candles._get_shoonya") as mock_shoonya:
                mock_shoonya_conn = MagicMock()
                mock_shoonya_conn.is_connected = True
                mock_shoonya.return_value = mock_shoonya_conn

                client = TestClient(app)
                resp = client.get("/api/market/sensex/candles?timeframe=5m&limit=50")

        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "SENSEX"
        assert data["exchange"] == "BSE"
        assert data["timeframe"] == "5m"
        assert len(data["candles"]) == 1
        assert data["errors"] == []

        _, kwargs = mock_fetch.call_args
        assert kwargs["exchange"] == "BSE"
        assert kwargs["token"] == "1"

    def test_sensex_candles_multiple_timeframes(self):
        app = _make_app()

        for timeframe in ["1m", "3m", "5m", "15m", "1h"]:
            with patch("api.candles._candleService.get_index_candles", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ([], [])

                with patch("api.candles._get_shoonya") as mock_shoonya:
                    mock_shoonya_conn = MagicMock()
                    mock_shoonya_conn.is_connected = True
                    mock_shoonya.return_value = mock_shoonya_conn

                    client = TestClient(app)
                    resp = client.get(f"/api/market/sensex/candles?timeframe={timeframe}")

            assert resp.status_code == 200
            assert resp.json()["timeframe"] == timeframe


# ── Response structure validation ────────────────────────────────────────────

class TestCandlesResponseStructure:

    def test_response_always_has_required_fields(self):
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

    def test_get_shoonya_raises_503_when_shoonya_missing_from_app_state(self):
        """No app.state.shoonya at all (never connected at startup)."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/market/nifty/candles")
        assert resp.status_code == 503

    def test_get_shoonya_raises_503_when_not_connected(self):
        app = _make_app()

        @app.middleware("http")
        async def _inject_shoonya(request, call_next):
            mock_shoonya = MagicMock()
            mock_shoonya.is_connected = False
            request.app.state.shoonya = mock_shoonya
            return await call_next(request)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/market/nifty/candles")
        assert resp.status_code == 503
