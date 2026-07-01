from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List
import asyncio
import json
import math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

router = APIRouter(prefix="/api/market", tags=["Market Quotes"])


class QuoteRequest(BaseModel):
    symbols: List[str]

IST = ZoneInfo("Asia/Kolkata")

# ── Last Traded Price Cache (for closed market display) ──────────────
_LAST_PRICE_CACHE = {}


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


_ALL_INDICES = [
    {
        "display_name": "Nifty 50",
        "stock_code": "NIFTY", "exchange_code": "NSE",
        "shoonya_exchange": "NSE", "shoonya_token": "26000",
    },
    {
        "display_name": "Sensex",
        "stock_code": "BSESEN", "exchange_code": "BSE",
        "shoonya_exchange": "BSE", "shoonya_token": "1",
    },
    {
        "display_name": "Bank Nifty",
        "stock_code": "CNXBAN", "exchange_code": "NSE",
        "shoonya_exchange": "NSE", "shoonya_token": "26009",
    },
    {
        "display_name": "India VIX",
        "stock_code": "INDVIX", "exchange_code": "NSE",
        "shoonya_exchange": "NSE", "shoonya_token": "26017",
    },
    {
        "display_name": "Fin Nifty",
        "stock_code": "NIFFIN", "exchange_code": "NSE",
        "shoonya_exchange": "NSE", "shoonya_token": "26037",
    },
    {
        "display_name": "Midcap Nifty",
        "stock_code": "NIFSEL", "exchange_code": "NSE",
        "shoonya_exchange": "NSE", "shoonya_token": "26074",
    },
]


def _get_market_clients(request: Request) -> tuple:
    """Returns (shoonya, breeze). Raises 503 only when both are unavailable."""
    shoonya = getattr(request.app.state, "shoonya", None)
    breeze  = getattr(request.app.state, "breeze", None)
    if shoonya is None and breeze is None:
        raise HTTPException(status_code=503, detail="Market data service is not ready. Try again shortly.")
    return shoonya, breeze


def _get_breeze(request: Request):
    """Raises 503 if Breeze session was not initialized at startup."""
    breeze = getattr(request.app.state, "breeze", None)
    if breeze is None:
        raise HTTPException(status_code=503, detail="Market data service is not ready. Try again shortly.")
    return breeze


def _parse_get_quotes(q: dict) -> dict | None:
    """
    Parse a Breeze get_quotes record.
    Returns None when ltp is zero (Breeze returned no real data, e.g. BSESEN on BSE).
    Correct field names from live response: ltp, open, high, low, previous_close, ltp_percent_change.
    """
    ltp = _safe_float(q.get("ltp"))
    if ltp == 0:
        return None
    prev_close = _safe_float(q.get("previous_close"))
    change     = round(ltp - prev_close, 2)
    raw_pct    = q.get("ltp_percent_change")
    change_pct = round(float(raw_pct) if raw_pct not in (None, "") else ((change / prev_close * 100) if prev_close else 0.0), 2)
    return {
        "ltp":        ltp,
        "open":       _safe_float(q.get("open")),
        "high":       _safe_float(q.get("high")),
        "low":        _safe_float(q.get("low")),
        "change":     change,
        "change_pct": change_pct,
        "as_of":      q.get("ltt"),
    }


def _parse_historical(data: list) -> dict | None:
    """
    Parse Breeze get_historical_data 1-minute candles into a quote dict.
    Uses the first candle's open as day open, last candle's close as current price,
    and max/min across all candles for day high/low.
    """
    if not data:
        return None
    day_open  = _safe_float(data[0].get("open"))
    close_val = _safe_float(data[-1].get("close"))
    if close_val == 0:
        return None
    day_high   = max((_safe_float(c.get("high")) for c in data), default=0.0)
    low_vals   = [_safe_float(c.get("low")) for c in data if _safe_float(c.get("low")) > 0]
    day_low    = min(low_vals) if low_vals else 0.0
    change     = round(close_val - day_open, 2)
    change_pct = round((change / day_open) * 100, 2) if day_open else 0.0
    return {
        "ltp":        close_val,
        "open":       day_open,
        "high":       day_high,
        "low":        day_low,
        "change":     change,
        "change_pct": change_pct,
        "as_of":      data[-1].get("datetime"),
    }


async def _fetch_index_quote(idx: dict, shoonya, breeze, trading_day: str, loop) -> tuple[dict | None, str | None]:
    """Fetch a single index quote with fallback logic. Returns (quote_dict, source_name).

    Caches successful quotes for display when market is closed.
    """
    stock_code    = idx["stock_code"]
    exchange_code = idx["exchange_code"]
    quote         = None
    source        = None

    # ── Primary: Shoonya with 3s timeout ──────────────────────────────
    if shoonya is not None and shoonya.is_connected:
        try:
            quote = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda ex=idx["shoonya_exchange"], tk=idx["shoonya_token"]:
                        shoonya.get_index_quote(ex, tk)
                ),
                timeout=3.0
            )
            if quote:
                source = "shoonya"
                # Cache successful live price
                _LAST_PRICE_CACHE[stock_code] = {"quote": quote, "source": source}
                return quote, source
        except asyncio.TimeoutError:
            print(f"[Shoonya] Timeout for {stock_code}")
        except Exception as e:
            print(f"[Shoonya] Exception for {stock_code}: {e}")

    # ── Fallback 1: Breeze get_quotes with 2s timeout ──────────────────
    if breeze is not None:
        try:
            resp = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda sc=stock_code, ex=exchange_code: breeze.get_quotes(
                        stock_code=sc, exchange_code=ex, product_type="cash"
                    )
                ),
                timeout=2.0
            )
            data = (resp or {}).get("Success") or [] if (resp or {}).get("Status") == 200 else []
            if data:
                quote = _parse_get_quotes(data[0])
                if quote:
                    source = "breeze"
                    # Cache successful live price
                    _LAST_PRICE_CACHE[stock_code] = {"quote": quote, "source": source}
                    return quote, source
        except asyncio.TimeoutError:
            print(f"[Breeze] get_quotes timeout for {stock_code}")
        except Exception as e:
            print(f"[Breeze] get_quotes failed for {stock_code}: {e}")

    # ── Fallback 2: Breeze historical (NSE only) with 2s timeout ──────────
    if breeze is not None and exchange_code == "NSE":
        try:
            resp2 = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda sc=stock_code: breeze.get_historical_data(
                        interval="1minute",
                        from_date=f"{trading_day}T09:15:00.000Z",
                        to_date=f"{trading_day}T15:30:00.000Z",
                        stock_code=sc,
                        exchange_code="NSE",
                        product_type="cash"
                    )
                ),
                timeout=2.0
            )
            hist = (resp2 or {}).get("Success") or [] if (resp2 or {}).get("Status") == 200 else []
            quote = _parse_historical(hist)
            if quote:
                source = "breeze_historical"
                # Cache successful price
                _LAST_PRICE_CACHE[stock_code] = {"quote": quote, "source": source}
                return quote, source
        except asyncio.TimeoutError:
            print(f"[Breeze] Historical timeout for {stock_code}")
        except Exception as e:
            print(f"[Breeze] Historical fallback failed for {stock_code}: {e}")

    # ── Last Resort: Return cached last price ONLY when market is closed ──
    if stock_code in _LAST_PRICE_CACHE and not _is_market_open():
        cached = _LAST_PRICE_CACHE[stock_code]
        return cached["quote"], f"{cached['source']} (cached)"

    return None, None


async def _fetch_indices(shoonya, breeze) -> tuple[list[dict], list[dict]]:
    """Fetch ALL indices in parallel with per-call timeouts."""
    trading_day = _last_trading_day()
    loop        = asyncio.get_running_loop()
    results     = []
    errors      = []

    # Fetch all indices in parallel (not sequentially)
    tasks = [
        _fetch_index_quote(idx, shoonya, breeze, trading_day, loop)
        for idx in _ALL_INDICES
    ]

    responses = await asyncio.gather(*tasks, return_exceptions=True)

    for idx, response in zip(_ALL_INDICES, responses):
        stock_code = idx["stock_code"]
        exchange_code = idx["exchange_code"]

        if isinstance(response, Exception):
            print(f"[Exception] {stock_code}: {response}")
            errors.append({"index": idx["display_name"], "stock_code": stock_code, "reason": str(response)})
            continue

        quote, source = response
        if quote is None:
            print(f"No data for {stock_code} from any provider")
            errors.append({"index": idx["display_name"], "stock_code": stock_code, "reason": "no_data"})
            continue

        results.append({
            "name":       idx["display_name"],
            "stock_code": stock_code,
            "exchange":   exchange_code,
            "value":      quote["ltp"],
            "open":       quote["open"],
            "high":       quote["high"],
            "low":        quote["low"],
            "change":     quote["change"],
            "change_pct": quote["change_pct"],
            "as_of":      quote.get("as_of"),
            "source":     source,
        })

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
    Returns the latest values for Nifty 50, Sensex, Bank Nifty, India VIX,
    Fin Nifty, and Midcap Nifty.
    Shoonya is the primary provider; Breeze is the fallback.
    """
    shoonya, breeze = _get_market_clients(request)
    indices, errors = await _fetch_indices(shoonya, breeze)
    return {
        "market_status": "open" if _is_market_open() else "closed",
        "indices":       indices,
        "errors":        errors,
        "last_updated":  datetime.now(timezone.utc).isoformat(),
    }


@router.get("/indices/stream")
async def stream_market_indices(request: Request):
    """
    SSE endpoint — pushes live Nifty 50, Sensex, Bank Nifty, India VIX,
    Fin Nifty, and Midcap Nifty to the frontend automatically.
      - Market open:   every 10 seconds
      - Market closed: every 60 seconds (keeps connection alive)

    Frontend:
        const es = new EventSource('/api/market/indices/stream');
        es.onmessage = (e) => {
            const { market_status, indices } = JSON.parse(e.data);
        };
    """
    async def _event_generator():
        while True:
            if await request.is_disconnected():
                break

            is_open = _is_market_open()

            shoonya = getattr(request.app.state, "shoonya", None)
            breeze  = getattr(request.app.state, "breeze",  None)
            if shoonya is None and breeze is None:
                yield f"data: {json.dumps({'market_status': 'open' if is_open else 'closed', 'indices': [], 'errors': [{'reason': 'market_data_unavailable'}], 'last_updated': datetime.now(timezone.utc).isoformat()})}\n\n"
                await asyncio.sleep(30)
                continue

            try:
                indices, errors = await _fetch_indices(shoonya, breeze)
                payload = json.dumps({
                    "market_status": "open" if is_open else "closed",
                    "indices":       indices,
                    "errors":        errors,
                    "last_updated":  datetime.now(timezone.utc).isoformat(),
                })
                yield f"data: {payload}\n\n"
            except Exception as exc:
                print(f"[SSE/indices] Error: {exc}")

            await asyncio.sleep(5 if is_open else 60)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/marquee")
async def get_marquee(
    request: Request,
    symbols: List[str] = Query(default=[], description="Optional stock symbols to include after indices"),
):
    """
    Single call for the ticker marquee.
    Always returns Nifty 50, Sensex, Bank Nifty, India VIX, Fin Nifty,
    and Midcap Nifty first, followed by any additional stock symbols passed as query params.
    Both are fetched concurrently.

    GET /api/market/marquee
    GET /api/market/marquee?symbols=RELIANCE&symbols=INFY&symbols=HDFCBANK
    """
    shoonya, breeze = _get_market_clients(request)

    async def _no_stocks():
        return [], []

    index_task = _fetch_indices(shoonya, breeze)
    stock_task = _fetch_quotes(breeze, symbols) if (symbols and breeze) else _no_stocks()

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
