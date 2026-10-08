import asyncio
import contextlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException, Response
from fastapi.responses import StreamingResponse

from api.marketquotes import _is_market_open
from service.sectorPerformance.SectorPerformanceService import (
    SectorIndexRegistry,
    SectorPerformanceFetcher,
)
from utils.fastjson import json_encoder
from utils.streamMetrics import stream_metrics
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market", tags=["Sector Performance"])

_CONFIG_PATH = Path(__file__).parent.parent / "appconfig" / "sector_indices.json"

_registry = SectorIndexRegistry(_CONFIG_PATH)
_fetcher  = SectorPerformanceFetcher(_registry)

# Single-flight snapshot shared by every request and stream client. From
# live ticks it is an in-memory read; if any sector needed REST, the next
# rebuild waits longer so requests never queue up broker calls.
_SECTORS_TICK_TTL_SECS = 0.5
_SECTORS_REST_TTL_SECS = 10.0
_sectors_snapshot: dict = {"at": 0.0, "data": None, "ttl": _SECTORS_TICK_TTL_SECS}
_sectors_snapshot_lock = asyncio.Lock()

SECTORS_STREAM_INTERVAL_SECS = 1.0
SECTORS_STREAM_CLOSED_INTERVAL_SECS = 30.0
SECTORS_KEEP_ALIVE_SECS = 15.0


def sector_instrument_keys() -> list[str]:
    """Pinned on the live feed at startup (app.py) so sectors come from ticks."""
    return _registry.instrument_keys()


def _get_shoonya(request: Request):
    shoonya = getattr(request.app.state, "shoonya", None)
    if shoonya is None or not shoonya.is_connected:
        raise HTTPException(status_code=503, detail="Shoonya market data is not connected.")
    return shoonya


def _snapshot_fresh() -> bool:
    return (
        _sectors_snapshot["data"] is not None
        and (time.monotonic() - _sectors_snapshot["at"]) < _sectors_snapshot["ttl"]
    )


async def _get_sectors_snapshot(shoonya, tick_source) -> tuple[list[dict], list[dict]]:
    if _snapshot_fresh():
        return _sectors_snapshot["data"]
    async with _sectors_snapshot_lock:
        if _snapshot_fresh():
            return _sectors_snapshot["data"]
        rest_used: list[str] = []
        data = await _fetcher.fetch_all(shoonya, tick_source, rest_used)
        _sectors_snapshot["data"] = data
        _sectors_snapshot["at"] = time.monotonic()
        _sectors_snapshot["ttl"] = _SECTORS_REST_TTL_SECS if rest_used else _SECTORS_TICK_TTL_SECS
        return data


@router.get("/sectors")
async def get_sector_performance(request: Request, response: Response):
    """
    Live NSE sectoral index performance for IT, Banking, Energy, FMCG,
    Pharma, Auto, Realty, and Metal - served from the live tick cache
    (REST only for a sector with no tick yet).
    """
    shoonya = _get_shoonya(request)
    sectors, errors = await _get_sectors_snapshot(shoonya, getattr(request.app.state, "stock_feed", None))
    response.headers["Cache-Control"] = "public, max-age=1"
    return {
        "market_status": "open" if _is_market_open() else "closed",
        "sectors":       sectors,
        "errors":        errors,
        "last_updated":  datetime.now(timezone.utc).isoformat(),
    }


@router.get("/sectors/stream")
async def stream_sector_performance(request: Request):
    """
    SSE endpoint — pushes live NSE sector performance to the frontend.
      - First frame immediately on connect.
      - Market open: a new frame within ~1 s of any sector changing.
      - Nothing changed: an SSE comment every 15 s keeps the connection open.

    Frontend usage:
        const es = new EventSource('/api/market/sectors/stream');
        es.onmessage = (e) => {
            const { market_status, sectors } = JSON.parse(e.data);
            // sectors: [{ sector, change_pct, change, ltp }, ...]
        };
    """
    async def _event_generator():
        stream_metrics.subscriber_added("sectors")
        try:
            async with contextlib.aclosing(_sector_frames()) as frames:
                async for frame in frames:
                    yield frame
        finally:
            stream_metrics.subscriber_removed("sectors")

    async def _sector_frames():
        loop = asyncio.get_running_loop()
        last_key = None
        last_write = loop.time()
        while True:
            if await request.is_disconnected():
                break

            is_open = _is_market_open()

            shoonya = getattr(request.app.state, "shoonya", None)
            if shoonya is None or not shoonya.is_connected:
                yield f"data: {json.dumps({'market_status': 'open' if is_open else 'closed', 'sectors': [], 'errors': [{'reason': 'shoonya_disconnected'}], 'last_updated': datetime.now(timezone.utc).isoformat()})}\n\n"
                last_key = None
                last_write = loop.time()
                await asyncio.sleep(30)
                continue

            try:
                sectors, errors = await _get_sectors_snapshot(shoonya, getattr(request.app.state, "stock_feed", None))
                values_key = (is_open, tuple((s["sector"], s["ltp"], s["change_pct"]) for s in sectors), len(errors))
                if values_key != last_key:
                    last_key = values_key
                    frame = json_encoder.sse_data({
                        "market_status": "open" if is_open else "closed",
                        "sectors":       sectors,
                        "errors":        errors,
                        "last_updated":  datetime.now(timezone.utc).isoformat(),
                    })
                    stream_metrics.record_send("sectors", len(frame))
                    last_write = loop.time()
                    yield frame
                elif loop.time() - last_write >= SECTORS_KEEP_ALIVE_SECS:
                    last_write = loop.time()
                    yield ": keep-alive\n\n"
            except Exception as exc:
                logger.warning(f"[SSE/sectors] Error: {exc}")

            await asyncio.sleep(SECTORS_STREAM_INTERVAL_SECS if is_open else SECTORS_STREAM_CLOSED_INTERVAL_SECS)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
