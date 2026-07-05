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
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import StreamingResponse

from appconfig import OptionMaster
from service.optionChain.OptionChainService import OPTIONS_EXCHANGE, OptionChainService

router = APIRouter(prefix="/api/market", tags=["Option Chain"])

_optionChainService = OptionChainService(feed=None)

STREAM_WAIT_TIMEOUT_SECS = 15  # how often to re-check request.is_disconnected() while idle
DISCONNECTED_RECHECK_SECS = 5  # how often to poll for Shoonya reconnecting once it's gone down mid-stream


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

    shoonya = getattr(request.app.state, "shoonya", None)
    if shoonya is None or not shoonya.is_connected:
        # Broker session already down before this stream even opened - serve
        # whatever was last cached (if anything) as a single frame and close,
        # rather than a bare 503. EventSource auto-reconnects a few seconds
        # later per the SSE spec, so once Shoonya comes back the next retry
        # picks up live data automatically without any client-side changes.
        cached, resolved_expiry = _optionChainService.peek_cached_chain(underlying, expiry)

        async def _cached_only_stream():
            yield f"data: {json.dumps(_envelope(underlying, resolved_expiry, cached, [{'reason': 'shoonya_disconnected'}]))}\n\n"

        return StreamingResponse(_cached_only_stream(), media_type="text/event-stream")

    cache, resolved_expiry, errors = await _optionChainService.get_cache_for_stream(shoonya, underlying, expiry)

    if cache is None:
        async def _error_stream():
            yield f"data: {json.dumps(_envelope(underlying, expiry, None, errors))}\n\n"

        return StreamingResponse(_error_stream(), media_type="text/event-stream")

    async def _event_generator():
        last_seen_gen = -1
        was_shoonya_disconnected = False
        cache = None
        resolved_expiry_value = None
        errors: list[dict] = [{"reason": "connecting"}]
        init_task = asyncio.create_task(_optionChainService.get_cache_for_stream(shoonya, underlying, expiry))

        try:
            await asyncio.sleep(0)
            if init_task.done():
                try:
                    cache, resolved_expiry_value, errors = init_task.result()
                except Exception as exc:  # pragma: no cover - defensive guard
                    errors = [{"reason": "initialization_failed", "detail": str(exc)}]
                    yield f"data: {json.dumps(_envelope(underlying, expiry, None, errors))}\n\n"
                    return

                if cache is None:
                    yield f"data: {json.dumps(_envelope(underlying, expiry, None, errors))}\n\n"
                    return

                resolved_expiry_value = resolved_expiry_value or expiry
                snapshot = cache.get()
                if snapshot is not None:
                    last_seen_gen = cache.generation
                    data = {**snapshot, "expiry": resolved_expiry_value}
                    yield f"data: {json.dumps(_envelope(underlying, resolved_expiry_value, data, []))}\n\n"
            else:
                # Send a lightweight placeholder immediately so the client and any
                # upstream proxy don't wait for the cache warm-up to finish.
                yield f"data: {json.dumps(_envelope(underlying, expiry, None, errors))}\n\n"

            while True:
                if await request.is_disconnected():
                    break

                if cache is None:
                    if init_task.done():
                        try:
                            cache, resolved_expiry_value, errors = init_task.result()
                        except Exception as exc:  # pragma: no cover - defensive guard
                            errors = [{"reason": "initialization_failed", "detail": str(exc)}]
                            yield f"data: {json.dumps(_envelope(underlying, expiry, None, errors))}\n\n"
                            break

                        if cache is None:
                            yield f"data: {json.dumps(_envelope(underlying, expiry, None, errors))}\n\n"
                            break

                        resolved_expiry_value = resolved_expiry_value or expiry
                        snapshot = cache.get()
                        if snapshot is not None:
                            last_seen_gen = cache.generation
                            data = {**snapshot, "expiry": resolved_expiry_value}
                            yield f"data: {json.dumps(_envelope(underlying, resolved_expiry_value, data, []))}\n\n"
                    else:
                        await asyncio.sleep(0.2)
                        continue

                # A chain that started streaming while Shoonya was connected
                # would otherwise loop here forever once it drops mid-stream
                # (this loop only touches the local cache, never Shoonya
                # directly), silently serving increasingly stale data with no
                # signal to the client that the broker session is down.
                if not shoonya.is_connected:
                    if not was_shoonya_disconnected:
                        was_shoonya_disconnected = True
                        snapshot = cache.get()
                        data = {**snapshot, "expiry": resolved_expiry_value} if snapshot else None
                        yield f"data: {json.dumps(_envelope(underlying, resolved_expiry_value, data, [{'reason': 'shoonya_disconnected'}]))}\n\n"
                    else:
                        # Same reverse-proxy read-timeout concern as the
                        # tick-wait branch below - a broker outage can last
                        # minutes (auto-login retries every RETRY_DELAY), so
                        # this must not go byte-silent on the wire that whole
                        # time.
                        yield ": keep-alive\n\n"
                    await asyncio.sleep(DISCONNECTED_RECHECK_SECS)
                    continue
                was_shoonya_disconnected = False

                try:
                    snapshot = await asyncio.wait_for(
                        cache.wait_for_next(last_seen_gen),
                        timeout=STREAM_WAIT_TIMEOUT_SECS,
                    )
                except asyncio.TimeoutError:
                    # No live tick for a full STREAM_WAIT_TIMEOUT_SECS (thin
                    # option volume, or the feed reconnecting) must not mean
                    # total silence on the wire - a reverse proxy's own read
                    # timeout (commonly 30-60s) will treat a byte-less
                    # connection as dead and return 504 even though this
                    # coroutine is still alive and holding it open correctly.
                    yield ": keep-alive\n\n"
                    continue

                last_seen_gen = cache.generation
                if snapshot is not None:
                    data = {**snapshot, "expiry": resolved_expiry_value}
                    yield f"data: {json.dumps(_envelope(underlying, resolved_expiry_value, data, []))}\n\n"
        finally:
            if init_task is not None:
                init_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await init_task
            if cache is not None and resolved_expiry_value is not None:
                await _optionChainService.release_chain(underlying, resolved_expiry_value)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
