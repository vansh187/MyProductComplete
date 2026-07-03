"""
Unit tests for ShoonyaConnection.get_time_price_series.
Verifies correct parsing of the real NorenApi TPSeries response shape
(plain list, into/inth/intl/intc/intv fields, newest-first ordering).
"""

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


def test_missing_interval_returns_none_without_calling_broker():
    class _FakeApiShouldNotBeCalled:
        def get_time_price_series(self, *a, **kw):
            raise AssertionError("should not be called when interval is falsy")

    conn = _make_connection(_FakeApiShouldNotBeCalled())
    result = conn.get_time_price_series("NSE", "26000", None, days=1)
    assert result is None
