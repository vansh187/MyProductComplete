"""
OHLC candle data endpoint for F&O trading terminal.
Provides historical candles for charting at multiple timeframes.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Query

from service.candleService.CandleService import CandleService
from service.optionChain.OptionChainService import UNDERLYING_SPOT_TOKENS
from utils.market_hours import IST_OFFSET

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market", tags=["Candles"])

_candleService = CandleService()


def _get_shoonya(request: Request):
    """Extract Shoonya connection from app state."""
    shoonya = getattr(request.app.state, "shoonya", None)
    if shoonya is None or not shoonya.is_connected:
        raise HTTPException(status_code=503, detail="Market data service is not ready. Try again shortly.")
    return shoonya


async def _index_candles(request: Request, symbol: str, timeframe: str, limit: int) -> dict:
    if not _candleService.is_supported_timeframe(timeframe):
        raise HTTPException(status_code=400, detail=f"Invalid timeframe: {timeframe}")

    shoonya = _get_shoonya(request)
    exchange, token = UNDERLYING_SPOT_TOKENS[symbol.lower()]

    try:
        candles, errors = await _candleService.get_index_candles(
            shoonya, exchange=exchange, token=token, timeframe=timeframe, limit=limit,
        )
    except Exception as exc:
        logger.error(f"[Candles] {symbol} {timeframe} failed: {exc}", exc_info=True)
        candles, errors = [], [{"reason": "candles_failed"}]

    return {
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": timeframe,
        "candles": candles,
        "errors": errors,
        "last_updated": datetime.now(IST_OFFSET).isoformat(timespec="milliseconds"),
    }


@router.get("/nifty/candles")
async def get_nifty_candles(
    request: Request,
    timeframe: str = Query("1m", description="Supported: 1m, 3m, 5m, 15m, 1h"),
    limit: int = Query(100, ge=1, le=500, description="Number of candles to return"),
):
    """
    Returns OHLC candle data for Nifty 50 at the specified timeframe.

    Timeframes: 1m, 3m, 5m, 15m, 1h (Shoonya's TPSeries has no sub-minute or daily granularity)
    Limit: 1-500 candles (default 100)

    Response:
    {
      "symbol": "NIFTY",
      "timeframe": "5m",
      "candles": [
        {"timestamp": "2026-07-01T09:15:00+05:30", "open": 24865.75, "high": 24880.20, ...},
        ...
      ],
      "errors": [],
      "last_updated": "2026-07-01T15:30:00.000+05:30"
    }
    """
    return await _index_candles(request, "NIFTY", timeframe, limit)


@router.get("/banknifty/candles")
async def get_banknifty_candles(
    request: Request,
    timeframe: str = Query("1m", description="Supported: 1m, 3m, 5m, 15m, 1h"),
    limit: int = Query(100, ge=1, le=500, description="Number of candles to return"),
):
    """
    Returns OHLC candle data for Bank Nifty at the specified timeframe.

    Timeframes: 1m, 3m, 5m, 15m, 1h (Shoonya's TPSeries has no sub-minute or daily granularity)
    Limit: 1-500 candles (default 100)
    """
    return await _index_candles(request, "BANKNIFTY", timeframe, limit)


@router.get("/finnifty/candles")
async def get_finnifty_candles(
    request: Request,
    timeframe: str = Query("1m", description="Supported: 1m, 3m, 5m, 15m, 1h"),
    limit: int = Query(100, ge=1, le=500, description="Number of candles to return"),
):
    """
    Returns OHLC candle data for Fin Nifty at the specified timeframe.

    Timeframes: 1m, 3m, 5m, 15m, 1h (Shoonya's TPSeries has no sub-minute or daily granularity)
    Limit: 1-500 candles (default 100)
    """
    return await _index_candles(request, "FINNIFTY", timeframe, limit)


@router.get("/sensex/candles")
async def get_sensex_candles(
    request: Request,
    timeframe: str = Query("1m", description="Supported: 1m, 3m, 5m, 15m, 1h"),
    limit: int = Query(100, ge=1, le=500, description="Number of candles to return"),
):
    """
    Returns OHLC candle data for Sensex (BSE) at the specified timeframe.

    Timeframes: 1m, 3m, 5m, 15m, 1h (Shoonya's TPSeries has no sub-minute or daily granularity)
    Limit: 1-500 candles (default 100)
    """
    return await _index_candles(request, "SENSEX", timeframe, limit)
