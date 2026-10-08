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
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market", tags=["Top Movers"])

_CONFIG_PATH = Path(__file__).parent.parent / "appconfig" / "nifty50_watchlist.json"

_watchlist = StockWatchlist(_CONFIG_PATH)
_fetcher   = TopMoversFetcher(_watchlist, top_n=5)
_cache     = TopMoversCache()

TICK_REFRESH_SECS = 5
REST_REFRESH_SECS = 300
CLOSED_REFRESH_SECS = 600
STREAM_CHECK_SECS = 5


def _movers_content(data: dict | None) -> tuple | None:
    """What viewers see, without the always-new last_updated stamp: the cache
    (and so every stream client) is only updated when this changes."""
    if data is None:
        return None
    return data.get("market_status"), data.get("gainers"), data.get("losers")


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
    Cache is rebuilt from live ticks every few seconds by a background task.
    On the very first request (cache empty), data is fetched live.
    """
    data = _cache.get()
    if data is None:
        shoonya = _require_shoonya(request)
        data = await _fetcher.fetch_top_movers(shoonya, _is_market_open(), getattr(request.app.state, "stock_feed", None))
        await _cache.update(data)
    return data


@router.get("/top-movers/stream")
async def stream_top_movers(request: Request):
    """
    SSE endpoint — pushes updated top movers data to the frontend
    every time the background cache refreshes (every few seconds during
    market hours from live ticks, every 10 minutes after close).

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

            await asyncio.sleep(STREAM_CHECK_SECS)

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
    Market open: rebuilt from the live tick cache every TICK_REFRESH_SECS
    (in-memory, no broker calls); if stocks had to come from REST (feed
    not up yet) the next refresh waits REST_REFRESH_SECS so the broker is
    never hammered. Market closed: every CLOSED_REFRESH_SECS.
    Initial refresh happens 15 seconds after startup.
    """
    await asyncio.sleep(15)

    while True:
        is_open = _is_market_open()
        rest_used: list[str] = []
        try:
            shoonya = getattr(app.state, "shoonya", None)
            if shoonya and shoonya.is_connected:
                data = await _fetcher.fetch_top_movers(
                    shoonya, is_open, getattr(app.state, "stock_feed", None), rest_used
                )
                if _movers_content(data) != _movers_content(_cache.get()):
                    await _cache.update(data)
                if rest_used:
                    logger.info(f"[TopMovers] Refreshed — {data.get('total_tracked', 0)} stocks tracked, "
                                f"{len(rest_used)} via REST")
        except Exception as exc:
            logger.warning(f"[TopMovers] Refresh error: {exc}")

        if not is_open:
            await asyncio.sleep(CLOSED_REFRESH_SECS)
        else:
            await asyncio.sleep(REST_REFRESH_SECS if rest_used else TICK_REFRESH_SECS)
