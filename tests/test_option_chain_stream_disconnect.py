"""
Regression test: the option-chain SSE stream must notice when Shoonya goes
down mid-stream and tell the client, instead of silently looping forever on
the local cache serving increasingly stale data with no signal.
"""

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import api.optionChain as mod


class _FakeRequest:
    def __init__(self):
        self.app = MagicMock()

    async def is_disconnected(self):
        return False


class _FakeCache:
    """generation never advances and wait_for_next() hangs forever - so once
    the stream is back to its normal (connected) branch, the test can assert
    it goes back to blocking silently rather than re-emitting frames."""

    generation = 0

    def get(self):
        return {"spot": 100.0, "strikes": []}

    async def wait_for_next(self, after_generation):
        await asyncio.sleep(3600)
        return self.get()


@pytest.mark.asyncio
async def test_stream_signals_disconnect_then_recovers(monkeypatch):
    monkeypatch.setattr(mod, "DISCONNECTED_RECHECK_SECS", 0.01)

    shoonya = MagicMock()
    shoonya.is_connected = True
    fake_request = _FakeRequest()
    fake_request.app.state.shoonya = shoonya
    fake_cache = _FakeCache()

    with patch("api.optionChain.OptionMaster") as mock_master, \
         patch.object(mod._optionChainService, "get_cache_for_stream",
                      new=AsyncMock(return_value=(fake_cache, "2026-07-14", []))), \
         patch.object(mod._optionChainService, "release_chain"):
        mock_master.is_valid_underlying.return_value = True

        response = await mod.stream_option_chain(underlying="nifty", request=fake_request, expiry=None)
        gen = response.body_iterator

        # Frame 1: initial snapshot, sent immediately, no error.
        frame1 = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert json.loads(frame1.split("data: ", 1)[1])["errors"] == []

        # Shoonya drops mid-stream.
        shoonya.is_connected = False
        frame2 = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        body2 = json.loads(frame2.split("data: ", 1)[1])
        assert body2["errors"] == [{"reason": "shoonya_disconnected"}]
        assert body2["spot"] == 100.0  # last-known data included, not wiped out

        # Start waiting for the *next* frame without forcing cancellation -
        # while still disconnected, it must not spam another notice frame.
        pending = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.1)
        assert not pending.done(), "must not emit another frame every poll tick while still down"

        # Shoonya recovers - the pending wait should still be blocked (now on
        # cache.wait_for_next(), which never resolves in this fake), i.e. no
        # extra disconnect/recovery frame gets emitted either.
        shoonya.is_connected = True
        await asyncio.sleep(0.1)
        assert not pending.done(), "recovery must not itself emit a frame"

        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
            await pending
        await gen.aclose()
