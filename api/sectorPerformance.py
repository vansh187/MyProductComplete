import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

from api.marketquotes import _is_market_open
from service.sectorPerformance.SectorPerformanceService import (
    SectorIndexRegistry,
    SectorPerformanceFetcher,
)

router = APIRouter(prefix="/api/market", tags=["Sector Performance"])

_CONFIG_PATH = Path(__file__).parent.parent / "appconfig" / "sector_indices.json"

_registry = SectorIndexRegistry(_CONFIG_PATH)
_fetcher  = SectorPerformanceFetcher(_registry)


def _get_shoonya(request: Request):
    shoonya = getattr(request.app.state, "shoonya", None)
    if shoonya is None or not shoonya.is_connected:
        raise HTTPException(status_code=503, detail="Shoonya market data is not connected.")
    return shoonya


@router.get("/sectors")
async def get_sector_performance(request: Request):
    """
    Returns live NSE sectoral index performance for IT, Banking, Energy,
    FMCG, Pharma, Auto, Realty, and Metal. All 8 sectors are fetched in
    parallel for minimum latency.
    """
    shoonya = _get_shoonya(request)
    sectors, errors = await _fetcher.fetch_all(shoonya)
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
      - Market open:   every 10 seconds
      - Market closed: every 60 seconds (keeps connection alive)

    Frontend usage:
        const es = new EventSource('/api/market/sectors/stream');
        es.onmessage = (e) => {
            const { market_status, sectors } = JSON.parse(e.data);
            // sectors: [{ sector, change_pct, change, ltp }, ...]
        };
    """
    async def _event_generator():
        while True:
            if await request.is_disconnected():
                break

            is_open = _is_market_open()

            shoonya = getattr(request.app.state, "shoonya", None)
            if shoonya is None or not shoonya.is_connected:
                yield f"data: {json.dumps({'market_status': 'open' if is_open else 'closed', 'sectors': [], 'errors': [{'reason': 'shoonya_disconnected'}], 'last_updated': datetime.now(timezone.utc).isoformat()})}\n\n"
                await asyncio.sleep(30)
                continue

            try:
                sectors, errors = await _fetcher.fetch_all(shoonya)
                payload = json.dumps({
                    "market_status": "open" if is_open else "closed",
                    "sectors":       sectors,
                    "errors":        errors,
                    "last_updated":  datetime.now(timezone.utc).isoformat(),
                })
                yield f"data: {payload}\n\n"
            except Exception as exc:
                print(f"[SSE/sectors] Error: {exc}")

            await asyncio.sleep(10 if is_open else 60)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
