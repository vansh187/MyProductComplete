"""
Unit tests for ShoonyaConnection.get_time_price_series.
Verifies correct parsing of the real NorenApi TPSeries response shape
(plain list, into/inth/intl/intc/intv fields, newest-first ordering) and
that failures are surfaced as None rather than raising.
"""

from unittest.mock import MagicMock

import pytest

from marketengine.ShoonyaConnection import ShoonyaConnection


def _make_connection(fake_api):
    conn = ShoonyaConnection.__new__(ShoonyaConnection)
    conn._connected = True
    conn._api = fake_api
    return conn


class _FakeApiNewestFirst:
    """Mimics NorenApi's real TPSeries response: newest candle first."""

    def get_time_price_series(self, exchange, token, starttime=None, endtime=None, interval=None):
        assert "lastn" not in dir(self)  # sanity: no such kwarg exists on real API
        return [
            {"time": "03-07-2026 09:17:00", "into": "24888.15", "inth": "24900.00", "intl": "24880.00", "intc": "24895.00", "intv": "60000"},
            {"time": "03-07-2026 09:16:00", "into": "24870.50", "inth": "24895.00", "intl": "24865.30", "intc": "24888.15", "intv": "55000"},
            {"time": "03-07-2026 09:15:00", "into": "24865.75", "inth": "24880.20", "intl": "24850.10", "intc": "24870.50", "intv": "50000"},
        ]


def test_candles_are_sorted_oldest_first():
    """Broker returns newest-first; result must be re-sorted ascending."""
    conn = _make_connection(_FakeApiNewestFirst())
    result = conn.get_time_price_series("NSE", "26000", "1", days=1)

    assert result is not None
    timestamps = [c["timestamp"] for c in result]
    assert timestamps == [
        "03-07-2026 09:15:00",
        "03-07-2026 09:16:00",
        "03-07-2026 09:17:00",
    ]


def test_fields_mapped_from_broker_names():
    """into/inth/intl/intc/intv map to open/high/low/close/volume."""
    conn = _make_connection(_FakeApiNewestFirst())
    result = conn.get_time_price_series("NSE", "26000", "1", days=1)

    first = result[0]  # oldest candle after sort: 09:15:00
    assert first["open"] == 24865.75
    assert first["high"] == 24880.20
    assert first["low"] == 24850.10
    assert first["close"] == 24870.50
    assert first["volume"] == 50000


def test_non_list_response_returns_none():
    """A dict response (e.g. {'stat': 'Not_Ok', ...}) means failure -> None."""

    class _FakeApiError:
        def get_time_price_series(self, exchange, token, starttime=None, endtime=None, interval=None):
            return {"stat": "Not_Ok", "emsg": "Session Expired"}

    conn = _make_connection(_FakeApiError())
    result = conn.get_time_price_series("NSE", "26000", "1", days=1)
    assert result is None


def test_empty_list_response_returns_none():
    """An empty (but valid) list means no candles -> None, not []."""

    class _FakeApiEmpty:
        def get_time_price_series(self, exchange, token, starttime=None, endtime=None, interval=None):
            return []

    conn = _make_connection(_FakeApiEmpty())
    result = conn.get_time_price_series("NSE", "26000", "1", days=1)
    assert result is None


def test_missing_interval_returns_none_without_calling_broker():
    class _FakeApiShouldNotBeCalled:
        def get_time_price_series(self, *a, **kw):
            raise AssertionError("should not be called when interval is falsy")

    conn = _make_connection(_FakeApiShouldNotBeCalled())
    result = conn.get_time_price_series("NSE", "26000", None, days=1)
    assert result is None


def test_not_connected_returns_none_without_calling_broker():
    class _FakeApiShouldNotBeCalled:
        def get_time_price_series(self, *a, **kw):
            raise AssertionError("should not be called when disconnected")

    conn = _make_connection(_FakeApiShouldNotBeCalled())
    conn._connected = False
    result = conn.get_time_price_series("NSE", "26000", "1", days=1)
    assert result is None


def test_api_none_returns_none_without_raising():
    conn = ShoonyaConnection.__new__(ShoonyaConnection)
    conn._connected = True
    conn._api = None
    result = conn.get_time_price_series("NSE", "26000", "1", days=1)
    assert result is None


def test_broker_exception_returns_none():
    class _FakeApiRaises:
        def get_time_price_series(self, *a, **kw):
            raise ConnectionError("broker unreachable")

    conn = _make_connection(_FakeApiRaises())
    result = conn.get_time_price_series("NSE", "26000", "1", days=1)
    assert result is None


def test_malformed_candle_entries_are_skipped_not_fatal():
    """A single bad entry (unparseable time) must not fail the whole batch -
    the good candles around it should still come back."""

    class _FakeApiPartiallyBad:
        def get_time_price_series(self, exchange, token, starttime=None, endtime=None, interval=None):
            return [
                {"time": "not-a-real-timestamp", "into": "1", "inth": "1", "intl": "1", "intc": "1", "intv": "1"},
                {"time": "03-07-2026 09:15:00", "into": "24865.75", "inth": "24880.20", "intl": "24850.10", "intc": "24870.50", "intv": "50000"},
            ]

    conn = _make_connection(_FakeApiPartiallyBad())
    result = conn.get_time_price_series("NSE", "26000", "1", days=1)

    assert result is not None
    assert len(result) == 1
    assert result[0]["timestamp"] == "03-07-2026 09:15:00"


def test_calls_broker_with_expected_kwargs():
    fake_api = MagicMock()
    fake_api.get_time_price_series.return_value = [
        {"time": "03-07-2026 09:15:00", "into": "1", "inth": "1", "intl": "1", "intc": "1", "intv": "1"},
    ]
    conn = _make_connection(fake_api)

    conn.get_time_price_series("NSE", "26000", "5", days=3)

    _, kwargs = fake_api.get_time_price_series.call_args
    assert kwargs["exchange"] == "NSE"
    assert kwargs["token"] == "26000"
    assert kwargs["interval"] == "5"
    assert "starttime" in kwargs
