import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

from api.marketquotes import _is_market_open
from service.topMovers.TopMoversService import (
    StockWatchlist,
    TopMoversFetcher,
    TopMoversCache,
)

router = APIRouter(prefix="/api/market", tags=["Top Movers"])

_CONFIG_PATH = Path(__file__).parent.parent / "appconfig" / "nifty50_watchlist.json"

_watchlist = StockWatchlist(_CONFIG_PATH)
_fetcher   = TopMoversFetcher(_watchlist, top_n=5)
_cache     = TopMoversCache()


def _require_shoonya(request: Request):
    shoonya = getattr(request.app.state, "shoonya", None)
    if shoonya is None or not shoonya.is_connected:
        raise HTTPException(status_code=503, detail="Shoonya market data is not connected.")
    return shoonya


@router.get("/top-movers")
async def get_top_movers(request: Request):
    """
    Returns top 5 gainers and top 5 losers from Nifty 50 stocks.
    Served from in-memory cache — response time < 1ms.
    Cache is refreshed every 5 minutes by a background task.
    On the very first request (cache empty), data is fetched live.
    """
    data = _cache.get()
    if data is None:
        shoonya = _require_shoonya(request)
        data = await _fetcher.fetch_top_movers(shoonya, _is_market_open())
        await _cache.update(data)
    return data


@router.get("/top-movers/stream")
async def stream_top_movers(request: Request):
    """
    SSE endpoint — pushes updated top movers data to the frontend
    every time the background cache refreshes (every 5 minutes during
    market hours, every 10 minutes after close).

    Sends current cached data immediately on connect so the UI
    does not have to make a separate one-shot request.

    Frontend:
        const es = new EventSource('/api/market/top-movers/stream');
        es.onmessage = (e) => {
            const { gainers, losers, market_status } = JSON.parse(e.data);
        };
    """
    async def _event_generator():
        last_seen_gen = -1

        while True:
            if await request.is_disconnected():
                break

            current_gen = _cache.generation
            current_data = _cache.get()

            if current_gen > last_seen_gen and current_data is not None:
                last_seen_gen = current_gen
                yield f"data: {json.dumps(current_data)}\n\n"

            # Check every 30s — fine-grained enough for a 5-min refresh cycle
            await asyncio.sleep(30)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def start_background_refresh(app):
    """
    Background task started from app.py lifespan.
    Refreshes the top movers cache every 5 minutes when market is open,
    every 10 minutes when closed (keeps data current across the trading day).
    Initial refresh happens 15 seconds after startup.
    """
    await asyncio.sleep(15)

    while True:
        is_open = _is_market_open()
        try:
            shoonya = getattr(app.state, "shoonya", None)
            if shoonya and shoonya.is_connected:
                data = await _fetcher.fetch_top_movers(shoonya, is_open)
                await _cache.update(data)
                print(
                    f"[TopMovers] Refreshed — "
                    f"{len(data.get('gainers', []))} gainers, "
                    f"{len(data.get('losers', []))} losers, "
                    f"{data.get('total_tracked', 0)} stocks tracked"
                )
        except Exception as exc:
            print(f"[TopMovers] Refresh error: {exc}")

        await asyncio.sleep(300 if is_open else 600)
