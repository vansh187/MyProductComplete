"""
The option-chain SSE stream end to end, against a real OptionChainService,
OptionChainCache and ChainBroadcaster (only Shoonya and the request are
faked):

- a cold chain answers "connecting" at once instead of silence;
- a ready chain sends its snapshot at once;
- a burst of ticks becomes ONE batched frame per flush, not one per tick;
- format=delta sends a snapshot, then only the changed fields, seq +1 each;
- a broker outage is reported once, then keep-alives, and recovery resends
  a fresh snapshot;
- the chain is acquired once and released once per stream.
"""

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import api.optionChain as mod
import service.optionChain.ChainBroadcaster as broadcaster_module
from service.optionChain.OptionChainCache import OptionChainCache
from service.optionChain.OptionChainService import OptionChainService

EXPIRY = "2099-12-31"
STRIKE_CHAIN = {
    "24300": {"ce_token": "111", "pe_token": "222", "lot_size": 65},
    "24350": {"ce_token": "333", "pe_token": "444", "lot_size": 65},
}


class _FakeRequest:
    def __init__(self, shoonya):
        self.app = MagicMock()
        self.app.state.shoonya = shoonya

    async def is_disconnected(self):
        return False


def _body(frame) -> dict:
    text = frame.decode() if isinstance(frame, bytes) else frame
    return json.loads(text.split("data: ", 1)[1])


@pytest.fixture
def service(monkeypatch):
    """A fresh service with one seeded NIFTY chain, wired in as the
    endpoint's service; flushes every 20 ms so tests run fast."""
    monkeypatch.setattr(broadcaster_module, "CHAIN_FLUSH_SECS", 0.02)
    svc = OptionChainService(feed=None)
    cache = OptionChainCache("NIFTY", EXPIRY, STRIKE_CHAIN)
    cache.set_spot(24270.0)
    cache.seed_leg("24300", "ce", {"ltp": 62.0, "bid": 61.9, "ask": 62.1, "oi": 1000, "volume": 10})
    cache.take_changes()  # steady state: the seed was already flushed
    svc._caches[svc._cache_key("nifty", EXPIRY)] = cache
    svc.get_cache_for_stream = AsyncMock(return_value=(cache, EXPIRY, []))
    svc.release_chain = AsyncMock()
    monkeypatch.setattr(mod, "_optionChainService", svc)
    with patch("api.optionChain.OptionMaster") as mock_master:
        mock_master.is_valid_underlying.return_value = True
        yield svc, cache
    # release_chain is mocked here, so stop the flush tasks it would have.
    for broadcaster in svc._broadcasters.values():
        broadcaster.stop()


async def _open(fmt="full", shoonya=None):
    shoonya = shoonya or MagicMock(is_connected=True)
    response = await mod.stream_option_chain(underlying="nifty", request=_FakeRequest(shoonya),
                                             expiry=None, stream_format=fmt)
    return response.body_iterator, shoonya


async def _next(gen, timeout=1.0):
    return await asyncio.wait_for(gen.__anext__(), timeout=timeout)


@pytest.mark.asyncio
async def test_cold_chain_answers_connecting_immediately(monkeypatch):
    svc = OptionChainService(feed=None)

    async def slow_init(*args, **kwargs):
        await asyncio.sleep(0.5)
        return None, None, [{"reason": "no_option_data"}]

    svc.get_cache_for_stream = AsyncMock(side_effect=slow_init)
    monkeypatch.setattr(mod, "_optionChainService", svc)
    with patch("api.optionChain.OptionMaster") as mock_master:
        mock_master.is_valid_underlying.return_value = True
        gen, _ = await _open()
        assert _body(await _next(gen, 0.3))["errors"] == [{"reason": "connecting"}]
        await gen.aclose()


@pytest.mark.asyncio
async def test_ready_chain_sends_snapshot_at_once(service):
    gen, _ = await _open()
    body = _body(await _next(gen, 0.3))
    assert body["errors"] == []
    assert body["symbol"] == "NIFTY" and body["expiry"] == EXPIRY
    assert body["strikes"][0]["ce"]["ltp"] == 62.0
    assert "seq" in body and "srv_ts" in body
    await gen.aclose()


@pytest.mark.asyncio
async def test_tick_burst_becomes_one_batched_frame(service):
    svc, cache = service
    gen, _ = await _open()
    await _next(gen)  # snapshot

    for i in range(50):
        await cache.apply_tick("NFO|111", {"ltp": 63.0 + i * 0.05})

    body = _body(await _next(gen))
    assert body["strikes"][0]["ce"]["ltp"] == pytest.approx(63.0 + 49 * 0.05)
    # Nothing else is queued: 50 ticks cost one frame, not 50.
    pending = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.1)
    assert not pending.done()
    pending.cancel()
    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
        await pending
    await gen.aclose()


@pytest.mark.asyncio
async def test_delta_format_sends_snapshot_then_changed_fields_only(service):
    svc, cache = service
    gen, _ = await _open("delta")

    snapshot = _body(await _next(gen))
    assert snapshot["t"] == "s"
    assert snapshot["rows"][0][:3] == [24300.0, "111", "222"]
    assert snapshot["rows"][0][3]["ltp"] == 62.0

    await cache.apply_tick("NFO|111", {"ltp": 64.0})
    delta = _body(await _next(gen))
    assert delta["t"] == "d"
    assert delta["seq"] == snapshot["seq"] + 1
    token, fields = delta["u"][0]
    assert token == "111"
    assert fields["ltp"] == 64.0
    assert "oi" not in fields  # unchanged fields are not resent
    await gen.aclose()


@pytest.mark.asyncio
async def test_broker_outage_reported_once_then_keep_alive_then_fresh_snapshot(service, monkeypatch):
    monkeypatch.setattr(mod, "DISCONNECTED_RECHECK_SECS", 0.01)
    gen, shoonya = await _open()
    assert _body(await _next(gen))["errors"] == []

    shoonya.is_connected = False
    notice = _body(await _next(gen))
    assert notice["errors"] == [{"reason": "shoonya_disconnected"}]
    assert notice["spot"] == 24270.0  # last-known data kept
    for _ in range(3):
        assert await _next(gen) == mod.KEEP_ALIVE_FRAME

    shoonya.is_connected = True
    recovered = _body(await _next(gen))
    assert recovered["errors"] == [] and recovered["strikes"]
    await gen.aclose()


@pytest.mark.asyncio
async def test_chain_acquired_once_and_released_once(service):
    svc, cache = service
    gen, _ = await _open()
    await _next(gen)
    await gen.aclose()

    assert svc.get_cache_for_stream.await_count == 1
    svc.release_chain.assert_awaited_once_with("nifty", EXPIRY)
    assert svc._broadcasters[svc._cache_key("nifty", EXPIRY)].subscriber_count == 0


@pytest.mark.asyncio
async def test_broker_down_before_open_serves_cached_frame():
    shoonya = MagicMock(is_connected=False)
    with patch("api.optionChain.OptionMaster") as mock_master, \
         patch.object(mod._optionChainService, "peek_cached_chain", return_value=(None, None)):
        mock_master.is_valid_underlying.return_value = True
        response = await mod.stream_option_chain(underlying="nifty", request=_FakeRequest(shoonya), expiry=None)
        frames = [frame async for frame in response.body_iterator]
    assert len(frames) == 1
    assert _body(frames[0])["errors"] == [{"reason": "shoonya_disconnected"}]
