from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List
import asyncio
import json
import math
import yfinance as yf
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

router = APIRouter(prefix="/api/market", tags=["Market Quotes"])


class QuoteRequest(BaseModel):
    symbols: List[str]

IST = ZoneInfo("Asia/Kolkata")


def _is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    now_mins = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= now_mins <= (15 * 60 + 30)


def _last_trading_day() -> str:
    now = datetime.now(IST)
    candidate = now.date()

    before_open = now.hour * 60 + now.minute < 9 * 60 + 15
    if before_open:
        candidate -= timedelta(days=1)

    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)

    return candidate.strftime("%Y-%m-%d")


def _safe_float(value, default: float = 0.0) -> float:
    """Converts Breeze field to float, guarding against None, empty string, nan and inf."""
    try:
        result = float(value or default)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


async def _fetch_quotes(breeze, symbols: List[str]) -> tuple[list[dict], list[dict]]:
    trading_day = _last_trading_day()
    loop        = asyncio.get_running_loop()
    results     = []
    errors      = []

    for symbol in symbols:
        try:
            response = await loop.run_in_executor(
                None,
                lambda s=symbol: breeze.get_historical_data(
                    interval="1minute",
                    from_date=f"{trading_day}T09:15:00.000Z",
                    to_date=f"{trading_day}T15:30:00.000Z",
                    stock_code=s,
                    exchange_code="NSE",
                    product_type="cash"
                )
            )

            if not response or response.get("Status") != 200:
                reason = (response or {}).get("Error") or f"Status {(response or {}).get('Status')}"
                print(f"[Breeze] Non-200 for {symbol}: {response}")
                errors.append({"symbol": symbol, "reason": reason, "trading_day": trading_day})
                continue

            data = response.get("Success") or []
            if not data:
                print(f"[Breeze] Empty data for {symbol} on {trading_day}")
                errors.append({"symbol": symbol, "reason": "no_data", "trading_day": trading_day})
                continue

            latest     = data[-1]
            open_price = _safe_float(latest.get("open"))
            ltp        = _safe_float(latest.get("close"))
            change     = round(ltp - open_price, 2)
            change_pct = round((change / open_price) * 100, 2) if open_price else 0.0

            results.append({
                "symbol":      symbol,
                "ltp":         ltp,
                "open":        open_price,
                "change":      change,
                "change_pct":  change_pct,
                "volume":      _safe_int(latest.get("volume")),
                "price_as_of": latest.get("datetime"),
                "exchange":    "NSE",
            })

        except Exception as e:
            print(f"[Breeze] Exception for {symbol}: {e}")
            errors.append({"symbol": symbol, "reason": str(e), "trading_day": trading_day})

    return results, errors


def _build_response(stocks: list[dict], errors: list[dict] | None = None) -> dict:
    return {
        "market_status": "open" if _is_market_open() else "closed",
        "stocks": stocks,
        "errors": errors or [],
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


_INDICES = [
    {"display_name": "Nifty 50",   "stock_code": "NIFTY",   "exchange_code": "NSE"},
    {"display_name": "Bank Nifty", "stock_code": "CNXBAN",  "exchange_code": "NSE"},
    {"display_name": "India VIX",  "stock_code": "INDVIX",  "exchange_code": "NSE"},
]


def _get_breeze(request: Request):
    """Raises 503 if Breeze session was not initialized at startup."""
    breeze = getattr(request.app.state, "breeze", None)
    if breeze is None:
        raise HTTPException(status_code=503, detail="Market data service is not ready. Try again shortly.")
    return breeze


async def _fetch_sensex(loop) -> tuple[dict | None, dict | None]:
    """Fetches Sensex via yfinance (^BSESN) — Breeze does not support BSE historical data."""
    def _get() -> dict:
        fi = yf.Ticker("^BSESN").fast_info
        # Access all attributes inside the executor — fast_info is lazily loaded
        # and touching attributes outside would block the event loop thread.
        return {
            "last_price":     fi.last_price,
            "previous_close": fi.previous_close,
            "open":           fi.open,
            "day_high":       fi.day_high,
            "day_low":        fi.day_low,
        }

    try:
        raw        = await loop.run_in_executor(None, _get)
        ltp        = _safe_float(raw["last_price"])
        prev_close = _safe_float(raw["previous_close"])
        open_val   = _safe_float(raw["open"])
        change     = round(ltp - prev_close, 2)
        change_pct = round((change / prev_close) * 100, 2) if prev_close else 0.0

        return {
            "name":        "Sensex",
            "stock_code":  "^BSESN",
            "exchange":    "BSE",
            "value":       ltp,
            "open":        open_val,
            "high":        _safe_float(raw["day_high"]),
            "low":         _safe_float(raw["day_low"]),
            "change":      change,
            "change_pct":  change_pct,
            "as_of":       None,
        }, None

    except Exception as e:
        print(f"[yfinance] Exception for Sensex: {e}")
        return None, {"index": "Sensex", "stock_code": "^BSESN", "reason": str(e)}


async def _fetch_indices(breeze) -> tuple[list[dict], list[dict]]:
    trading_day = _last_trading_day()
    loop        = asyncio.get_running_loop()
    results     = []
    errors      = []

    for idx in _INDICES:
        stock_code    = idx["stock_code"]
        exchange_code = idx["exchange_code"]
        try:
            response = await loop.run_in_executor(
                None,
                lambda sc=stock_code, ex=exchange_code: breeze.get_historical_data(
                    interval="1minute",
                    from_date=f"{trading_day}T09:15:00.000Z",
                    to_date=f"{trading_day}T15:30:00.000Z",
                    stock_code=sc,
                    exchange_code=ex,
                    product_type="cash"
                )
            )

            if not response or response.get("Status") != 200:
                reason = (response or {}).get("Error") or f"Status {(response or {}).get('Status')}"
                print(f"[Breeze] Non-200 for index {stock_code}: {response}")
                errors.append({"index": idx["display_name"], "stock_code": stock_code, "reason": reason})
                continue

            data = response.get("Success") or []
            if not data:
                print(f"[Breeze] Empty data for index {stock_code} on {trading_day}")
                errors.append({"index": idx["display_name"], "stock_code": stock_code, "reason": "no_data"})
                continue

            latest     = data[-1]
            open_val   = _safe_float(latest.get("open"))
            close_val  = _safe_float(latest.get("close"))
            change     = round(close_val - open_val, 2)
            change_pct = round((change / open_val) * 100, 2) if open_val else 0.0

            results.append({
                "name":        idx["display_name"],
                "stock_code":  stock_code,
                "exchange":    exchange_code,
                "value":       close_val,
                "open":        open_val,
                "high":        _safe_float(latest.get("high")),
                "low":         _safe_float(latest.get("low")),
                "change":      change,
                "change_pct":  change_pct,
                "as_of":       latest.get("datetime"),
            })

        except Exception as e:
            print(f"[Breeze] Exception for index {stock_code}: {e}")
            errors.append({"index": idx["display_name"], "stock_code": stock_code, "reason": str(e)})

    # Fetch Sensex via yfinance — Breeze does not support BSE exchange
    sensex, sensex_err = await _fetch_sensex(loop)
    if sensex:
        results.insert(1, sensex)   # slot it after Nifty 50
    if sensex_err:
        errors.append(sensex_err)

    return results, errors


def _normalize_index(item: dict) -> dict:
    return {
        "label":       item.get("name", ""),
        "value":       item.get("value", 0.0),
        "change":      item.get("change", 0.0),
        "change_pct":  item.get("change_pct", 0.0),
        "type":        "index",
    }


def _normalize_stock(item: dict) -> dict:
    return {
        "label":       item.get("symbol", ""),
        "value":       item.get("ltp", 0.0),
        "change":      item.get("change", 0.0),
        "change_pct":  item.get("change_pct", 0.0),
        "type":        "stock",
    }


@router.get("/indices")
async def get_market_indices(request: Request):
    """
    Returns the latest values for Nifty 50, Sensex, Bank Nifty, and India VIX.
    When market is closed, returns the last available candle from the previous session.
    """
    breeze = _get_breeze(request)
    indices, errors = await _fetch_indices(breeze)
    return {
        "market_status": "open" if _is_market_open() else "closed",
        "indices":       indices,
        "errors":        errors,
        "last_updated":  datetime.now(timezone.utc).isoformat(),
    }


@router.get("/marquee")
async def get_marquee(
    request: Request,
    symbols: List[str] = Query(default=[], description="Optional stock symbols to include after indices"),
):
    """
    Single call for the ticker marquee.
    Always returns Nifty 50, Sensex, Bank Nifty, and India VIX first,
    followed by any additional stock symbols passed as query params.
    Both are fetched concurrently.

    GET /api/market/marquee
    GET /api/market/marquee?symbols=RELIANCE&symbols=INFY&symbols=HDFCBANK
    """
    breeze = _get_breeze(request)

    async def _no_stocks():
        return [], []

    index_task = _fetch_indices(breeze)
    stock_task = _fetch_quotes(breeze, symbols) if symbols else _no_stocks()

    results = await asyncio.gather(index_task, stock_task, return_exceptions=True)

    indices, idx_errors = results[0] if not isinstance(results[0], Exception) else ([], [{"reason": str(results[0])}])
    stocks,  stk_errors = results[1] if not isinstance(results[1], Exception) else ([], [{"reason": str(results[1])}])

    items = [_normalize_index(i) for i in indices] + [_normalize_stock(s) for s in stocks]

    return {
        "market_status": "open" if _is_market_open() else "closed",
        "items":         items,
        "errors":        idx_errors + stk_errors,
        "last_updated":  datetime.now(timezone.utc).isoformat(),
    }


@router.post("/quotes")
async def get_market_quotes(request: Request, body: QuoteRequest):
    """
    Returns live prices fetched directly from Breeze for the requested symbols.
    When market is closed, returns the closing price of the last trading session.

    Body: { "symbols": ["RELIANCE", "INFY", "HDFCBANK"] }
    """
    breeze  = _get_breeze(request)
    if not body.symbols:
        raise HTTPException(status_code=400, detail="No symbols provided.")

    stocks, errors = await _fetch_quotes(breeze, body.symbols)
    return _build_response(stocks, errors)


@router.get("/quotes/stream")
async def stream_market_quotes(
    request: Request,
    symbols: List[str] = Query(..., description="e.g. ?symbols=RELIND&symbols=INFTEC")
):
    """
    SSE endpoint — pushes fresh prices from Breeze to the frontend automatically.
      - Market open:   every 12 seconds
      - Market closed: every 60 seconds (price won't change, keeps connection alive)

    Frontend:
        const es = new EventSource('/api/market/quotes/stream?symbols=RELIND&symbols=INFTEC');
        es.onmessage = (e) => {
            const { market_status, stocks } = JSON.parse(e.data);
        };
    """
    breeze  = _get_breeze(request)
    if not symbols:
        raise HTTPException(status_code=400, detail="No symbols provided.")

    async def event_generator():
        while True:
            # Stop the loop as soon as the client closes the browser tab
            if await request.is_disconnected():
                print("[SSE] Client disconnected. Stopping stream.")
                break

            try:
                stocks, errors = await _fetch_quotes(breeze, symbols)
                payload = json.dumps(_build_response(stocks, errors))
                yield f"data: {payload}\n\n"
            except Exception as e:
                print(f"[SSE] Error generating event: {e}")

            await asyncio.sleep(12 if _is_market_open() else 60)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
