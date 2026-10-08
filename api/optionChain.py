"""
Live option-chain endpoint for the F&O terminal. Real strikes/tokens come
from Shoonya's NFO scrip master (appconfig/OptionMaster.py); live LTP/OI
come from Shoonya's WebSocket touchline feed (marketengine/ShoonyaOptionFeed.py);
IV is computed in-house (service/optionChain/blackScholes.py) since Shoonya
provides no IV field anywhere.
"""

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import StreamingResponse

from appconfig import OptionMaster
from service.optionChain.ChainBroadcaster import FORMAT_DELTA, FORMAT_FULL, STREAM_FORMATS
from service.optionChain.OptionChainService import OPTIONS_EXCHANGE, OptionChainService
from utils.market_hours import IST_OFFSET

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market", tags=["Option Chain"])

_optionChainService = OptionChainService(feed=None)

STREAM_WAIT_TIMEOUT_SECS = 15  # longest silence before a keep-alive comment
DISCONNECTED_RECHECK_SECS = 5  # how often a waiting stream re-checks the broker session and the client
FIRST_FRAME_WAIT_SECS = 0.05   # a ready chain answers with data; a cold one with "connecting" after this
KEEP_ALIVE_FRAME = ": keep-alive\n\n"
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _validate_expiry_format(expiry: str | None) -> None:
    if expiry is None:
        return
    try:
        datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid expiry format: {expiry} (expected YYYY-MM-DD)")


def _envelope(underlying: str, expiry: str | None, data: dict | None, errors: list[dict]) -> dict:
    return {
        "symbol": underlying.upper(),
        "exchange": OPTIONS_EXCHANGE.get(underlying.lower(), "NFO"),
        "expiry": data["expiry"] if data else expiry,
        "spot": data["spot"] if data else None,
        "strikes": data["strikes"] if data else [],
        "errors": errors,
        "last_updated": datetime.now(IST_OFFSET).isoformat(timespec="milliseconds"),
    }


@router.get("/{underlying}/expiries")
def get_expiries(underlying: str) -> list[str]:
    """
    Still-tradable expiries for an index underlying from the broker's
    contract list, as a sorted list of YYYY-MM-DD dates. Today's expiry is
    included until the 15:30 close.

    underlying: nifty | banknifty | finnifty | sensex

    Response: ["2026-10-06", "2026-10-13", "2026-10-20", "2026-10-27", ...]
    """
    if not OptionMaster.is_valid_underlying(underlying):
        raise HTTPException(status_code=400, detail=f"Invalid underlying: {underlying}")
    return _optionChainService.list_expiries(underlying)


@router.get("/{underlying}/optionchain")
async def get_option_chain(
    underlying: str,
    request: Request,
    expiry: str = Query(None, description="YYYY-MM-DD; defaults to the nearest available expiry"),
):
    """
    Returns the live option chain for an index underlying.

    underlying: nifty | banknifty | finnifty | sensex
    expiry: optional, defaults to the nearest available expiry

    Response:
    {
      "symbol": "NIFTY", "exchange": "NFO", "expiry": "2026-07-31", "spot": 24270.85,
      "strikes": [{"strike": 24300, "ce": {...}, "pe": {...}}, ...],
      "errors": [], "last_updated": "..."
    }
    """
    if not OptionMaster.is_valid_underlying(underlying):
        raise HTTPException(status_code=400, detail=f"Invalid underlying: {underlying}")

    _validate_expiry_format(expiry)

    shoonya = getattr(request.app.state, "shoonya", None)
    if shoonya is None or not shoonya.is_connected:
        # Broker session down (after-hours token refresh, holiday, outage) -
        # still serve whatever this process last saw for this chain instead
        # of a bare 503, so the frontend can keep showing last-traded data
        # for the user to analyze ahead of the next session.
        cached, resolved_expiry = _optionChainService.peek_cached_chain(underlying, expiry)
        if cached is not None:
            return _envelope(underlying, resolved_expiry, cached, [{"reason": "shoonya_disconnected"}])
        raise HTTPException(status_code=503, detail="Market data service is not ready. Try again shortly.")

    data, errors = await _optionChainService.get_chain(shoonya, underlying, expiry)
    return _envelope(underlying, expiry, data, errors)


def _error_frame(fmt: str, underlying: str, expiry: str | None, data: dict | None, errors: list[dict]) -> str:
    """A status frame (connecting / broker down / failure) in the client's
    format. Delta clients keep their last state and just show the status."""
    if fmt == FORMAT_DELTA:
        return f"data: {json.dumps({'t': 'e', 'sym': underlying.upper(), 'exp': expiry, 'errors': errors, 'srv_ts': int(time.time() * 1000)})}\n\n"
    return f"data: {json.dumps(_envelope(underlying, expiry, data, errors))}\n\n"


@router.get("/{underlying}/optionchain/stream")
async def stream_option_chain(
    underlying: str,
    request: Request,
    expiry: str = Query(None, description="YYYY-MM-DD; defaults to the nearest available expiry"),
    stream_format: str = Query(FORMAT_FULL, alias="format", pattern="^(full|delta)$",
                               description="full (default): whole chain per update; delta: snapshot then changed fields only"),
):
    """
    SSE endpoint. Ticks are batched by the chain's broadcaster and sent at
    most once per flush (CHAIN_FLUSH_MS, default 250 ms), serialized once
    for all clients - see service/optionChain/ChainBroadcaster.py.

    format=full (default): every message is the whole chain, same shape as
    before plus seq/srv_ts.
    format=delta: {"t":"s"} snapshot on connect, then {"t":"d"} messages with
    only the changed fields; {"t":"e"} carries status (connecting, broker
    down). A gap in seq means a missed message: reconnect.

    Frontend:
        const es = new EventSource('/api/market/nifty/optionchain/stream?format=delta');
        es.onmessage = (e) => { const msg = JSON.parse(e.data); };
    """
    if not OptionMaster.is_valid_underlying(underlying):
        raise HTTPException(status_code=400, detail=f"Invalid underlying: {underlying}")

    _validate_expiry_format(expiry)
    # Called directly (not via FastAPI), the default is the Query object.
    fmt = stream_format if stream_format in STREAM_FORMATS else FORMAT_FULL

    shoonya = getattr(request.app.state, "shoonya", None)
    if shoonya is None or not shoonya.is_connected:
        # Broker session already down before this stream even opened - serve
        # whatever was last cached (if anything) as a single frame and close,
        # rather than a bare 503. EventSource auto-reconnects a few seconds
        # later per the SSE spec, so once Shoonya comes back the next retry
        # picks up live data automatically without any client-side changes.
        cached, resolved_expiry = _optionChainService.peek_cached_chain(underlying, expiry)

        async def _cached_only_stream():
            yield _error_frame(fmt, underlying, resolved_expiry, cached, [{"reason": "shoonya_disconnected"}])

        return StreamingResponse(_cached_only_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    async def _event_generator():
        loop = asyncio.get_running_loop()
        cache = None
        resolved_expiry = None
        subscriber = None
        # Acquired exactly once per stream and released in finally.
        init_task = asyncio.create_task(_optionChainService.get_cache_for_stream(shoonya, underlying, expiry))
        try:
            done, _ = await asyncio.wait({init_task}, timeout=FIRST_FRAME_WAIT_SECS)
            if not done:
                # A new chain is still seeding: tell the client right away
                # instead of leaving it (and any proxy) waiting on silence.
                yield _error_frame(fmt, underlying, expiry, None, [{"reason": "connecting"}])
            try:
                cache, resolved_expiry, errors = await init_task
            except Exception as exc:
                logger.error(f"[optionChain] stream init failed for {underlying} {expiry}: {exc!r}")
                yield _error_frame(fmt, underlying, expiry, None, [{"reason": "initialization_failed"}])
                return
            if cache is None:
                yield _error_frame(fmt, underlying, expiry, None, errors)
                return

            subscriber = _optionChainService.subscribe_stream(underlying, resolved_expiry, fmt)
            if subscriber is None:
                yield _error_frame(fmt, underlying, resolved_expiry, None, [{"reason": "option_chain_failed"}])
                return

            was_shoonya_disconnected = False
            last_write = loop.time()
            while True:
                if await request.is_disconnected():
                    break

                # The broadcaster only reads the local cache; without this a
                # broker outage would look like a quiet market to the client.
                if not shoonya.is_connected:
                    if not was_shoonya_disconnected:
                        was_shoonya_disconnected = True
                        snapshot = cache.get()
                        data = {**snapshot, "expiry": resolved_expiry} if snapshot else None
                        yield _error_frame(fmt, underlying, resolved_expiry, data, [{"reason": "shoonya_disconnected"}])
                    else:
                        # A reverse proxy treats a byte-silent connection as
                        # dead; an outage can last minutes.
                        yield KEEP_ALIVE_FRAME
                    last_write = loop.time()
                    await asyncio.sleep(DISCONNECTED_RECHECK_SECS)
                    continue
                if was_shoonya_disconnected:
                    was_shoonya_disconnected = False
                    # Whatever changed during the outage: start from a fresh snapshot.
                    _optionChainService.resync_stream(underlying, resolved_expiry, subscriber)

                try:
                    frame = await asyncio.wait_for(subscriber.queue.get(), timeout=DISCONNECTED_RECHECK_SECS)
                except asyncio.TimeoutError:
                    if loop.time() - last_write >= STREAM_WAIT_TIMEOUT_SECS:
                        yield KEEP_ALIVE_FRAME
                        last_write = loop.time()
                    continue
                last_write = loop.time()
                yield frame
        finally:
            if subscriber is not None:
                _optionChainService.unsubscribe_stream(underlying, resolved_expiry, subscriber)
            if cache is None and init_task.done() and not init_task.cancelled() and init_task.exception() is None:
                # The client left between the chain being acquired and us
                # reading the result: still release that hold.
                cache, resolved_expiry, _ = init_task.result()
            if not init_task.done():
                init_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await init_task
            if cache is not None and resolved_expiry is not None:
                await _optionChainService.release_chain(underlying, resolved_expiry)

    return StreamingResponse(_event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS)
