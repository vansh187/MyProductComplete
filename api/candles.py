"""
OHLC candle data endpoint for F&O trading terminal.
Provides historical candles for charting at multiple timeframes.
"""

import asyncio
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException, Query

from service.candleService.CandleService import CandleService

router = APIRouter(prefix="/api/market", tags=["Candles"])

_candleService = CandleService()


def _get_shoonya(request: Request):
    """Extract Shoonya connection from app state."""
    shoonya = getattr(request.app.state, "shoonya", None)
    if shoonya is None or not shoonya.is_connected:
        raise HTTPException(status_code=503, detail="Market data service is not ready. Try again shortly.")
    return shoonya


@router.get("/nifty/candles")
async def get_nifty_candles(
    request: Request,
    timeframe: str = Query("1m", description="1m, 3m, 5m, 15m, 1h, 1d"),
    limit: int = Query(100, ge=1, le=500, description="Number of candles to return"),
):
    """
    Returns OHLC candle data for Nifty 50 at the specified timeframe.

    Timeframes: 1m, 3m, 5m, 15m, 1h, 1d
    Limit: 1-500 candles (default 100)

    Response:
    {
      "symbol": "NIFTY",
      "timeframe": "1m",
      "candles": [
        {"timestamp": "2026-07-01T09:15:00+05:30", "open": 24865.75, "high": 24880.20, ...},
        ...
      ],
      "errors": []
    }
    """
    if timeframe not in ("1m", "3m", "5m", "15m", "1h", "1d"):
        raise HTTPException(status_code=400, detail=f"Invalid timeframe: {timeframe}")

    shoonya = _get_shoonya(request)

    candles, errors = await _candleService.get_index_candles(
        shoonya,
        exchange="NSE",
        token="26000",  # Nifty 50
        timeframe=timeframe,
        limit=limit,
    )

    return {
        "symbol": "NIFTY",
        "exchange": "NSE",
        "timeframe": timeframe,
        "candles": candles,
        "errors": errors,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/banknifty/candles")
async def get_banknifty_candles(
    request: Request,
    timeframe: str = Query("1m", description="1m, 3m, 5m, 15m, 1h, 1d"),
    limit: int = Query(100, ge=1, le=500, description="Number of candles to return"),
):
    """
    Returns OHLC candle data for Bank Nifty at the specified timeframe.

    Timeframes: 1m, 3m, 5m, 15m, 1h, 1d
    Limit: 1-500 candles (default 100)
    """
    if timeframe not in ("1m", "3m", "5m", "15m", "1h", "1d"):
        raise HTTPException(status_code=400, detail=f"Invalid timeframe: {timeframe}")

    shoonya = _get_shoonya(request)

    candles, errors = await _candleService.get_index_candles(
        shoonya,
        exchange="NSE",
        token="26009",  # Bank Nifty
        timeframe=timeframe,
        limit=limit,
    )

    return {
        "symbol": "BANKNIFTY",
        "exchange": "NSE",
        "timeframe": timeframe,
        "candles": candles,
        "errors": errors,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/finnifty/candles")
async def get_finnifty_candles(
    request: Request,
    timeframe: str = Query("1m", description="1m, 3m, 5m, 15m, 1h, 1d"),
    limit: int = Query(100, ge=1, le=500, description="Number of candles to return"),
):
    """
    Returns OHLC candle data for Fin Nifty at the specified timeframe.

    Timeframes: 1m, 3m, 5m, 15m, 1h, 1d
    Limit: 1-500 candles (default 100)
    """
    if timeframe not in ("1m", "3m", "5m", "15m", "1h", "1d"):
        raise HTTPException(status_code=400, detail=f"Invalid timeframe: {timeframe}")

    shoonya = _get_shoonya(request)

    candles, errors = await _candleService.get_index_candles(
        shoonya,
        exchange="NSE",
        token="26037",  # Fin Nifty
        timeframe=timeframe,
        limit=limit,
    )

    return {
        "symbol": "FINNIFTY",
        "exchange": "NSE",
        "timeframe": timeframe,
        "candles": candles,
        "errors": errors,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
