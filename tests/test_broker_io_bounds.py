"""
Bounds on every blocking broker I/O path:
  - BrokerHttpClient: pooled keep-alive Session, default timeout, no retries,
    installed into NorenApi's module so all ~30 library REST calls use it.
  - _ShoonyaApi.subscribe/unsubscribe: wait at most ws_send_wait_secs for the
    socket instead of NorenApi's unbounded busy-wait.
  - WsSubscriptionSender: FIFO, never blocks submit(), survives failing sends.
"""

import json
import threading
import time
import types
from unittest.mock import MagicMock

import requests

from marketengine import ShoonyaConnection as shoonya_module
from marketengine.BrokerHttpClient import BrokerHttpClient
from marketengine.WsSubscriptionSender import WsSubscriptionSender


# ── BrokerHttpClient ─────────────────────────────────────────────────────

class _FakeSession:
    def __init__(self):
        self.mounted = {}
        self.calls = []
        self.closed = False

    def mount(self, prefix, adapter):
        self.mounted[prefix] = adapter

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return "resp"

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return "resp"

    def close(self):
        self.closed = True


def _client(**kwargs):
    session = _FakeSession()
    return BrokerHttpClient(session_factory=lambda: session, **kwargs), session


def test_post_applies_default_timeout():
    client, session = _client(connect_timeout=1.5, read_timeout=4.0)
    assert client.post("https://x/GetQuotes", data="jData={}") == "resp"
    _, url, kwargs = session.calls[0]
    assert url == "https://x/GetQuotes"
    assert kwargs["timeout"] == (1.5, 4.0)
    assert kwargs["data"] == "jData={}"


def test_explicit_timeout_is_preserved():
    client, session = _client()
    client.post("https://x/GenAcsTok", data="d", timeout=15)
    assert session.calls[0][2]["timeout"] == 15


def test_adapter_is_pooled_and_never_retries():
    """Orders go through this client - a POST must never be silently re-sent."""
    client, session = _client(pool_size=32)
    adapter = session.mounted["https://"]
    assert adapter.max_retries.total == 0
    assert adapter._pool_maxsize == 32
    assert session.mounted["http://"] is adapter


def test_unknown_attributes_delegate_to_requests_module():
    client, _ = _client()
    assert client.exceptions is requests.exceptions
    assert client.codes is requests.codes


def test_install_replaces_module_requests_and_is_idempotent():
    client, _ = _client()
    module = types.SimpleNamespace(requests=requests)
    assert client.install(module) is True
    assert module.requests is client
    assert client.install(module) is True
    assert module.requests is client


def test_install_refuses_module_without_requests():
    client, _ = _client()
    assert client.install(types.SimpleNamespace()) is False
    assert client.install(None) is False


def test_close_never_raises():
    client, session = _client()
    session.close = MagicMock(side_effect=RuntimeError("boom"))
    client.close()


def test_noren_library_calls_go_through_installed_client():
    """The library resolves `requests` from its module globals at call time,
    so after install() a real NorenApi method uses our session + timeout."""
    module = shoonya_module._noren_module
    original = module.requests
    client, session = _client(connect_timeout=2.0, read_timeout=5.0)
    try:
        client.install(module)
        api = shoonya_module._ShoonyaApi("https://api.shoonya.com/NorenWClientAPI/")
        api.injectOAuthHeader("tok", "UID", "AID")
        session.post = MagicMock(return_value=MagicMock(text=json.dumps({"stat": "Ok", "lp": "1"})))
        assert api.get_quotes(exchange="NSE", token="26000")["stat"] == "Ok"
        assert session.post.call_args.kwargs["timeout"] == (2.0, 5.0)
    finally:
        module.requests = original


def test_connect_installs_pooled_client(monkeypatch):
    http = MagicMock()
    fake_api = MagicMock()
    fake_api.get_quotes.return_value = {"stat": "Ok"}
    monkeypatch.setattr(shoonya_module, "_ShoonyaApi", lambda url: fake_api)
    conn = shoonya_module.ShoonyaConnection(http_client=http)
    conn._session_token = "session"
    assert conn.connect() is True
    http.install.assert_called_once_with(shoonya_module._noren_module)


def test_exchange_code_uses_pooled_client():
    http = MagicMock()
    http.post.return_value = MagicMock(json=MagicMock(return_value={"stat": "Not_Ok"}), status_code=200, text="")
    conn = shoonya_module.ShoonyaConnection(http_client=http)
    assert conn.exchange_code("code") is None
    assert http.post.call_args.kwargs["timeout"] == 15


# ── _ShoonyaApi bounded WS send ──────────────────────────────────────────

def _api(wait_secs=0.1, connected=False):
    api = shoonya_module._ShoonyaApi("https://api.shoonya.com/NorenWClientAPI/", ws_send_wait_secs=wait_secs)
    api._NorenApi__websocket = MagicMock()
    api._NorenApi__websocket_connected = connected
    return api


def test_subscribe_gives_up_within_bound_when_socket_down():
    api = _api(wait_secs=0.1, connected=False)
    start = time.perf_counter()
    assert api.subscribe(["NSE|1"]) is False
    assert time.perf_counter() - start < 1.0
    api._NorenApi__websocket.send.assert_not_called()


def test_subscribe_sends_library_compatible_frame_when_connected():
    api = _api(connected=True)
    assert api.subscribe(["NSE|1", "NSE|2"]) is True
    assert json.loads(api._NorenApi__websocket.send.call_args.args[0]) == {"t": "t", "k": "NSE|1#NSE|2"}


def test_unsubscribe_and_snapquote_frames():
    api = _api(connected=True)
    api.unsubscribe("NSE|1")
    api.subscribe(["NSE|1"], feed_type=2)
    api.unsubscribe(["NSE|1"], feed_type=2)
    frames = [json.loads(c.args[0]) for c in api._NorenApi__websocket.send.call_args_list]
    assert frames == [{"t": "u", "k": "NSE|1"}, {"t": "d", "k": "NSE|1"}, {"t": "ud", "k": "NSE|1"}]


def test_unsubscribe_unknown_feed_type_is_rejected():
    api = _api(connected=True)
    assert api.unsubscribe(["NSE|1"], feed_type=99) is False


def test_subscribe_waits_for_reconnect_within_bound():
    api = _api(wait_secs=2.0, connected=False)
    threading.Timer(0.1, lambda: setattr(api, "_NorenApi__websocket_connected", True)).start()
    assert api.subscribe(["NSE|1"]) is True


def test_send_failure_returns_false_instead_of_raising():
    api = _api(connected=True)
    api._NorenApi__websocket.send.side_effect = RuntimeError("closed")
    assert api.subscribe(["NSE|1"]) is False


# ── WsSubscriptionSender ─────────────────────────────────────────────────

def test_sender_runs_in_fifo_order_off_the_calling_thread():
    sender = WsSubscriptionSender(thread_name="test-fifo")
    seen = []
    caller = threading.current_thread()
    try:
        for i in range(5):
            assert sender.submit(f"op{i}", lambda i=i: seen.append((i, threading.current_thread() is caller)))
        assert sender.flush(timeout=2)
    finally:
        sender.close()
    assert seen == [(i, False) for i in range(5)]


def test_sender_survives_a_failing_send():
    sender = WsSubscriptionSender(thread_name="test-fail")
    seen = []
    try:
        sender.submit("bad", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        sender.submit("good", lambda: seen.append("ok"))
        assert sender.flush(timeout=2)
    finally:
        sender.close()
    assert seen == ["ok"]


def test_submit_never_blocks_and_rejects_when_full():
    sender = WsSubscriptionSender(max_pending=2, thread_name="test-full")
    gate = threading.Event()
    try:
        sender.submit("blocker", lambda: gate.wait(5))
        time.sleep(0.05)  # let the thread take the blocker off the queue
        assert sender.submit("a", lambda: None)
        assert sender.submit("b", lambda: None)
        start = time.perf_counter()
        assert sender.submit("c", lambda: None) is False
        assert time.perf_counter() - start < 0.05
    finally:
        gate.set()
        sender.close()


def test_submit_after_close_is_rejected():
    sender = WsSubscriptionSender(thread_name="test-closed")
    sender.close()
    assert sender.submit("late", lambda: None) is False
