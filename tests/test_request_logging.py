import logging
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
from marketengine import ShoonyaConnection as shoonya_module
from utils.logging_config import _parse_level


def _client():
    return TestClient(app_module.app)  # lifespan (broker login) deliberately not entered


def test_every_request_gets_timing_headers_and_one_log_line(caplog, monkeypatch):
    monkeypatch.setattr(app_module.app.state, "stocks_service", None, raising=False)
    with caplog.at_level(logging.INFO, logger="latency"):
        response = _client().get("/api/stocks/facets?q=1")

    assert response.headers["X-Request-ID"]
    assert float(response.headers["X-Response-Time-Ms"]) >= 0
    assert response.headers["Server-Timing"].startswith("app;dur=")
    lines = [r for r in caplog.records if r.name == "latency"]
    assert len(lines) == 1
    assert f"rid={response.headers['X-Request-ID']}" in lines[0].getMessage()
    assert "GET /api/stocks/facets?q=1 503" in lines[0].getMessage()
    assert lines[0].levelno == logging.WARNING and "UNAVAILABLE" in lines[0].getMessage()


def test_incoming_request_id_is_propagated():
    response = _client().get("/", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"


def test_unsafe_incoming_request_id_is_replaced(caplog):
    forged = "abc GET /admin 200 1.0ms ip=10.0.0.1"
    with caplog.at_level(logging.INFO, logger="latency"):
        response = _client().get("/api/stocks/facets", headers={"X-Request-ID": forged})
    assert response.headers["X-Request-ID"] != forged
    assert all(forged not in r.getMessage() for r in caplog.records)


class _Req:
    def __init__(self, peer, headers):
        self.client = type("C", (), {"host": peer})() if peer else None
        self.headers = {k.lower(): v for k, v in headers.items()}


def test_client_ip_ignores_proxy_headers_from_untrusted_peer():
    req = _Req("203.0.113.9", {"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"})
    assert app_module._client_ip(req) == "203.0.113.9"


def test_client_ip_uses_last_forwarded_hop_from_local_proxy():
    req = _Req("127.0.0.1", {"X-Forwarded-For": "1.2.3.4, 198.51.100.7"})
    assert app_module._client_ip(req) == "198.51.100.7"


def test_client_ip_rejects_garbage_header_values():
    req = _Req("127.0.0.1", {"X-Forwarded-For": "not-an-ip GET /x", "X-Real-IP": "also bad"})
    assert app_module._client_ip(req) == "127.0.0.1"
    assert app_module._client_ip(_Req(None, {})) == "-"


def test_health_path_logged_at_debug_only(caplog):
    with caplog.at_level(logging.INFO, logger="latency"):
        _client().get("/")
    assert not [r for r in caplog.records if r.name == "latency"]


def test_slow_request_logged_as_warning(caplog):
    with patch.object(app_module, "SLOW_REQUEST_THRESHOLD_MS", -1), caplog.at_level(logging.INFO, logger="latency"):
        _client().get("/api/stocks/explore")
    record = next(r for r in caplog.records if r.name == "latency")
    assert record.levelno == logging.WARNING


class _Conn:
    @shoonya_module._timed_broker_call
    def get_stock_quote(self, exchange, token):
        return {"ltp": 1}

    @shoonya_module._timed_broker_call
    def failing(self):
        raise RuntimeError("boom")


TIMING_LOGGER = "marketengine.ShoonyaConnection.timing"


def test_broker_call_timing_debug_and_slow_warning(caplog):
    with caplog.at_level(logging.DEBUG, logger=TIMING_LOGGER):
        assert _Conn().get_stock_quote("NSE", "1") == {"ltp": 1}
        with patch.object(shoonya_module, "SLOW_BROKER_CALL_MS", -1):
            _Conn().get_stock_quote("NSE", "2")
    records = [r for r in caplog.records if r.name == TIMING_LOGGER]
    assert [r.levelno for r in records] == [logging.DEBUG, logging.WARNING]
    assert "get_stock_quote('NSE', '1')" in records[0].getMessage()


def test_broker_call_timing_skips_formatting_when_debug_disabled(caplog):
    timing_logger = logging.getLogger(TIMING_LOGGER)
    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER), patch.object(timing_logger, "debug") as debug:
        _Conn().get_stock_quote("NSE", "1")
    debug.assert_not_called()


def test_timing_logger_is_child_of_module_logger():
    assert shoonya_module.broker_timing_logger.parent is logging.getLogger("marketengine.ShoonyaConnection")


def test_broker_call_timing_does_not_swallow_exceptions():
    try:
        _Conn().failing()
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("exception was swallowed")


def test_level_parsing_falls_back_on_garbage():
    assert _parse_level("debug", logging.INFO) == logging.DEBUG
    assert _parse_level("nonsense", logging.INFO) == logging.INFO
