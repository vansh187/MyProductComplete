"""
Live option-chain endpoint for the F&O terminal. Real strikes/tokens come
from Shoonya's NFO scrip master (appconfig/OptionMaster.py); live LTP/OI
come from Shoonya's WebSocket touchline feed (marketengine/ShoonyaOptionFeed.py);
IV is computed in-house (service/optionChain/blackScholes.py) since Shoonya
provides no IV field anywhere.
"""

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import StreamingResponse

from appconfig import OptionMaster
from service.optionChain.OptionChainService import OptionChainService

router = APIRouter(prefix="/api/market", tags=["Option Chain"])

_optionChainService = OptionChainService(feed=None)

STREAM_WAIT_TIMEOUT_SECS = 15  # how often to re-check request.is_disconnected() while idle
DISCONNECTED_RECHECK_SECS = 5  # how often to poll for Shoonya reconnecting once it's gone down mid-stream


def _get_shoonya(request: Request):
    shoonya = getattr(request.app.state, "shoonya", None)
    if shoonya is None or not shoonya.is_connected:
        raise HTTPException(status_code=503, detail="Market data service is not ready. Try again shortly.")
    return shoonya


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
        "exchange": "NFO",
        "expiry": data["expiry"] if data else expiry,
        "spot": data["spot"] if data else None,
        "strikes": data["strikes"] if data else [],
        "errors": errors,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/{underlying}/optionchain")
async def get_option_chain(
    underlying: str,
    request: Request,
    expiry: str = Query(None, description="YYYY-MM-DD; defaults to the nearest available expiry"),
):
    """
    Returns the live option chain for an index underlying.

    underlying: nifty | banknifty | finnifty
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
    shoonya = _get_shoonya(request)

    data, errors = await _optionChainService.get_chain(shoonya, underlying, expiry)
    return _envelope(underlying, expiry, data, errors)


@router.get("/{underlying}/optionchain/stream")
async def stream_option_chain(
    underlying: str,
    request: Request,
    expiry: str = Query(None, description="YYYY-MM-DD; defaults to the nearest available expiry"),
):
    """
    SSE endpoint - pushes an updated option-chain snapshot every time a live
    tick lands for this (underlying, expiry), instead of polling on a fixed
    timer, so latency is bounded only by how fast Shoonya's feed delivers.

    Frontend:
        const es = new EventSource('/api/market/nifty/optionchain/stream');
        es.onmessage = (e) => { const chain = JSON.parse(e.data); };
    """
    if not OptionMaster.is_valid_underlying(underlying):
        raise HTTPException(status_code=400, detail=f"Invalid underlying: {underlying}")

    _validate_expiry_format(expiry)
    shoonya = _get_shoonya(request)

    cache, resolved_expiry, errors = await _optionChainService.get_cache_for_stream(shoonya, underlying, expiry)

    if cache is None:
        async def _error_stream():
            yield f"data: {json.dumps(_envelope(underlying, expiry, None, errors))}\n\n"

        return StreamingResponse(_error_stream(), media_type="text/event-stream")

    async def _event_generator():
        last_seen_gen = -1
        was_shoonya_disconnected = False
        try:
            # Send the current snapshot immediately so the UI doesn't wait for the next tick.
            snapshot = cache.get()
            if snapshot is not None:
                last_seen_gen = cache.generation
                data = {**snapshot, "expiry": resolved_expiry}
                yield f"data: {json.dumps(_envelope(underlying, resolved_expiry, data, []))}\n\n"

            while True:
                if await request.is_disconnected():
                    break

                # A chain that started streaming while Shoonya was connected
                # would otherwise loop here forever once it drops mid-stream
                # (this loop only touches the local cache, never Shoonya
                # directly), silently serving increasingly stale data with no
                # signal to the client that the broker session is down.
                if not shoonya.is_connected:
                    if not was_shoonya_disconnected:
                        was_shoonya_disconnected = True
                        snapshot = cache.get()
                        data = {**snapshot, "expiry": resolved_expiry} if snapshot else None
                        yield f"data: {json.dumps(_envelope(underlying, resolved_expiry, data, [{'reason': 'shoonya_disconnected'}]))}\n\n"
                    await asyncio.sleep(DISCONNECTED_RECHECK_SECS)
                    continue
                was_shoonya_disconnected = False

                try:
                    snapshot = await asyncio.wait_for(
                        cache.wait_for_next(last_seen_gen),
                        timeout=STREAM_WAIT_TIMEOUT_SECS,
                    )
                except asyncio.TimeoutError:
                    continue

                last_seen_gen = cache.generation
                if snapshot is not None:
                    data = {**snapshot, "expiry": resolved_expiry}
                    yield f"data: {json.dumps(_envelope(underlying, resolved_expiry, data, []))}\n\n"
        finally:
            _optionChainService.release_chain(underlying, resolved_expiry)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
