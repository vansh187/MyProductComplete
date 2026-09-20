"""
Router-facing facade for the stocks feature (Explore / Search / Quote /
Chart). Mirrors mutualfunds/service.py's shape: all dependencies are
constructor-injected, no module-level/global/static state, every public
method is a coroutine the api layer awaits directly.

Thread/async-safety notes (this module touches the same shared broker
session every other market-data module does):
  - shoonya and stock_feed are passed into each call rather than held on
    self, same convention api/marketquotes.py, api/topMovers.py, etc. already
    use - the api layer resolves them fresh from request.app.state per
    request, since a reconnect can replace app.state.shoonya's underlying
    NorenApi instance between one request and the next.
  - Every broker REST call goes through asyncio.wait_for(loop.run_in_executor(...))
    with a bounded timeout, same as CandleService/TopMoversFetcher - a slow
    or hung broker call can never stall this event loop indefinitely.
  - stock_feed.get_tick()/touch() only ever touch that object's own
    short-lived lock (see marketengine/ShoonyaStockFeed.py) and never call
    back into this service - no lock is ever held while awaiting anything
    here, so there is no path to a deadlock between this service, the stock
    feed, and the shared option-chain WebSocket feed it rides on.
  - get_explore() is guarded by an asyncio.Lock + short TTL cache: without
    it, every hit to the public, no-auth /api/stocks/explore endpoint would
    fan out a fresh REST quote call per watchlist stock to the shared
    broker session - fine for one visitor, not for concurrent ones. Holding
    the lock across the whole cache-miss fetch also collapses a thundering
    herd of concurrent requests during that window into a single broker
    round trip instead of one each.
"""

import asyncio
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from appconfig.StockSymbolMaster import StockSymbolMaster
from service.stocksService.StockCollectionsCatalog import StockCollectionsCatalog
from service.stocksService.exceptions import MarketDataUnavailableError
from service.topMovers.TopMoversService import StockWatchlist

logger = logging.getLogger("stocksService")

IST = ZoneInfo("Asia/Kolkata")

_QUOTE_TIMEOUT_SECS = 6.0
_CHART_TIMEOUT_SECS = 15.0
_EXPLORE_QUOTE_TIMEOUT_SECS = 8.0
_EXPLORE_LIST_SIZE = 10
# Matches the frontend's explore-page poll cadence (ExploreStocks.tsx polls
# every 5s per the requirements doc) - short enough that cached data is
# never visibly stale, long enough that a burst of concurrent visitors
# within the same window shares one broker round trip.
_EXPLORE_CACHE_TTL_SECS = 5.0

# period -> (Shoonya minute interval, calendar days of history to request, aggregation bucket)
# Shoonya's TPSeries endpoint is minute-granularity only (no native daily/
# weekly candles), so 6m/1y/5y are fetched at 60-minute granularity and
# aggregated here into daily/weekly buckets. "5y" is capped to ~2 years of
# lookback: the broker's own intraday history retention is finite and a
# genuine 5-year, 60-minute-candle request would either time out or come
# back empty far more often than it would succeed - a shorter, reliably
# available window beats a request that predictably fails.
_CHART_PERIODS = {
    "1d": {"interval": "5", "days": 1, "bucket": None},
    "1w": {"interval": "15", "days": 7, "bucket": None},
    "1m": {"interval": "60", "days": 30, "bucket": None},
    "6m": {"interval": "60", "days": 180, "bucket": "day"},
    "1y": {"interval": "60", "days": 365, "bucket": "day"},
    "5y": {"interval": "60", "days": 730, "bucket": "week"},
}


class StocksService:

    def __init__(self, symbol_master: StockSymbolMaster, explore_watchlist: StockWatchlist,
                 collections_catalog: StockCollectionsCatalog):
        self._symbol_master = symbol_master
        self._explore_watchlist = explore_watchlist
        self._collections_catalog = collections_catalog
        self._explore_cache_data: dict | None = None
        self._explore_cache_at: float = 0.0
        self._explore_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Facets / Search
    # ------------------------------------------------------------------

    async def get_facets(self) -> dict:
        return self._symbol_master.facets()

    async def search(self, query: str | None, exchange: str | None, sector: str | None,
                      page: int, page_size: int, stock_feed) -> list[dict]:
        records = self._symbol_master.search(query, exchange, sector, page, page_size)
        return [self._record_to_summary(record, stock_feed) for record in records]

    def _record_to_summary(self, record: dict, stock_feed) -> dict:
        tick = self._safe_get_tick(stock_feed, record["exchange"], record["token"])
        ltp = self._as_float(tick.get("ltp")) if tick else 0.0
        prev_close = self._as_float(tick.get("close")) if tick else 0.0
        change = round(ltp - prev_close, 2) if (ltp and prev_close) else 0.0
        change_pct = round((change / prev_close) * 100, 2) if prev_close else 0.0
        return {
            "symbol": record["symbol"],
            "exchange": record["exchange"],
            "name": record["name"],
            "ltp": round(ltp, 2),
            "change": change,
            "change_pct": change_pct,
            "volume": self._as_int(tick.get("volume")) if tick else 0,
            "sector": record.get("sector"),
        }

    def _safe_get_tick(self, stock_feed, exchange: str, token: str) -> dict | None:
        if stock_feed is None:
            return None
        try:
            return stock_feed.get_tick(f"{exchange}|{token}")
        except Exception as exc:
            logger.warning(f"[StocksService] get_tick failed for {exchange}:{token}: {exc}")
            return None

    def _as_float(self, value, default: float = 0.0) -> float:
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def _as_int(self, value, default: int = 0) -> int:
        try:
            return int(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # Explore
    # ------------------------------------------------------------------

    async def get_explore(self, shoonya) -> dict:
        """TTL-cached wrapper around _build_explore_page - see the class
        docstring's note on why this exists. The lock is held for the whole
        cache-miss fetch (not just the cache check), so concurrent callers
        during a miss all converge on one broker round trip."""
        async with self._explore_lock:
            now = time.monotonic()
            if self._explore_cache_data is not None and (now - self._explore_cache_at) < _EXPLORE_CACHE_TTL_SECS:
                return self._explore_cache_data

            page = await self._build_explore_page(shoonya)
            self._explore_cache_data = page
            self._explore_cache_at = time.monotonic()
            return page

    async def _build_explore_page(self, shoonya) -> dict:
        watchlist_stocks = self._explore_watchlist.stocks()
        summaries = await self._fetch_watchlist_summaries(shoonya, watchlist_stocks)

        by_volume = sorted(summaries, key=lambda s: s["volume"], reverse=True)
        by_gain = sorted(summaries, key=lambda s: s["change_pct"], reverse=True)
        by_loss = sorted(summaries, key=lambda s: s["change_pct"])

        gainers = [s for s in by_gain if s["change_pct"] > 0][:_EXPLORE_LIST_SIZE]
        losers = [s for s in by_loss if s["change_pct"] < 0][:_EXPLORE_LIST_SIZE]

        return {
            "market_status": self._explore_market_status(),
            "trending": by_volume[:_EXPLORE_LIST_SIZE],
            "top_gainers": gainers,
            "top_losers": losers,
            "most_active": by_volume[:_EXPLORE_LIST_SIZE],
            "collections": self._collections_catalog.all(),
        }

    async def _fetch_watchlist_summaries(self, shoonya, watchlist_stocks: list[dict]) -> list[dict]:
        if shoonya is None or not shoonya.is_connected:
            logger.warning("[StocksService] Shoonya not connected - explore lists will be empty")
            return []

        loop = asyncio.get_running_loop()

        async def _fetch_one(stock: dict) -> dict | None:
            try:
                quote = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda ex=stock["exchange"], tk=stock["token"]: shoonya.get_stock_quote(ex, tk)
                    ),
                    timeout=_EXPLORE_QUOTE_TIMEOUT_SECS,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[StocksService] explore quote timeout for {stock.get('symbol')}")
                return None
            except Exception as exc:
                logger.warning(f"[StocksService] explore quote failed for {stock.get('symbol')}: {exc}")
                return None

            if quote is None:
                return None

            ltp = self._as_float(quote.get("ltp"))
            prev_close = self._as_float(quote.get("close"))
            change = round(ltp - prev_close, 2) if prev_close else 0.0
            change_pct = round((change / prev_close) * 100, 2) if prev_close else 0.0
            return {
                "symbol": stock["symbol"],
                "exchange": stock["exchange"],
                "name": stock["name"],
                "ltp": round(ltp, 2),
                "change": change,
                "change_pct": change_pct,
                "volume": self._as_int(quote.get("volume")),
                "sector": stock.get("sector"),
            }

        results = await asyncio.gather(*[_fetch_one(s) for s in watchlist_stocks], return_exceptions=True)
        summaries = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"[StocksService] explore fetch raised: {result}")
                continue
            if result is not None:
                summaries.append(result)
        return summaries

    def _explore_market_status(self) -> str:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            return "CLOSED"
        minutes = now.hour * 60 + now.minute
        if 9 * 60 <= minutes < 9 * 60 + 15:
            return "PRE_OPEN"
        if 9 * 60 + 15 <= minutes <= 15 * 60 + 30:
            return "OPEN"
        return "CLOSED"

    # ------------------------------------------------------------------
    # Quote
    # ------------------------------------------------------------------

    async def get_quote(self, shoonya, stock_feed, exchange: str, symbol: str) -> dict | None:
        """Returns None if the symbol/exchange pair is unknown (caller returns
        404). Raises MarketDataUnavailableError if the symbol is valid but no
        data could be produced from either the tick cache or a bounded REST
        fallback (caller returns 503)."""
        record = self._symbol_master.resolve(exchange, symbol)
        if record is None:
            return None

        instrument_key = f"{record['exchange']}|{record['token']}"

        if stock_feed is not None:
            try:
                # touch() is idempotent and only subscribes once per token
                # (see marketengine/ShoonyaStockFeed.py) - safe to call on
                # every poll of this endpoint without growing the shared
                # feed's ref-count on every request; idle tokens are
                # released automatically by evict_idle_loop().
                stock_feed.touch(instrument_key)
            except Exception as exc:
                logger.warning(f"[StocksService] touch failed for {instrument_key}: {exc}")

        tick = self._safe_get_tick(stock_feed, record["exchange"], record["token"])
        if tick is not None and tick.get("ltp"):
            return self._build_quote_from_tick(record, tick)

        rest_quote = await self._fetch_rest_quote(shoonya, record)
        if rest_quote is not None:
            return rest_quote

        raise MarketDataUnavailableError(f"No quote data available for {exchange}:{symbol}")

    async def _fetch_rest_quote(self, shoonya, record: dict) -> dict | None:
        if shoonya is None or not shoonya.is_connected:
            return None
        loop = asyncio.get_running_loop()
        try:
            quote = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: shoonya.get_stock_quote(record["exchange"], record["token"])
                ),
                timeout=_QUOTE_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[StocksService] quote REST timeout for {record['exchange']}:{record['symbol']}")
            return None
        except Exception as exc:
            logger.warning(f"[StocksService] quote REST failed for {record['exchange']}:{record['symbol']}: {exc}")
            return None

        if quote is None:
            return None
        return self._build_quote_from_tick(record, quote)

    def _build_quote_from_tick(self, record: dict, tick: dict) -> dict:
        ltp = self._as_float(tick.get("ltp"))
        prev_close = self._as_float(tick.get("close"))
        change = round(ltp - prev_close, 2) if prev_close else 0.0
        change_pct = round((change / prev_close) * 100, 2) if prev_close else 0.0

        depth = tick.get("depth") or {}
        bids = self._pad_depth(depth.get("bids"))
        asks = self._pad_depth(depth.get("asks"))

        return {
            "symbol": record["symbol"],
            "exchange": record["exchange"],
            "name": tick.get("name") or record["name"],
            "ltp": round(ltp, 2),
            "change": change,
            "change_pct": change_pct,
            "open": round(self._as_float(tick.get("open")), 2),
            "high": round(self._as_float(tick.get("high")), 2),
            "low": round(self._as_float(tick.get("low")), 2),
            "close": round(prev_close, 2),
            "volume": self._as_int(tick.get("volume")),
            "avg_price": round(self._as_float(tick.get("avg_price")), 2),
            "upper_circuit": round(self._as_float(tick.get("upper_circuit")), 2),
            "lower_circuit": round(self._as_float(tick.get("lower_circuit")), 2),
            # Not exposed by Shoonya's touchline/quote payload - defaulting to
            # 0.0 (never null/missing) so a numeric-typed frontend field never
            # crashes rendering; a real fundamentals source can populate these
            # later without any response-shape change (see requirements doc
            # section 5).
            "week_52_high": 0.0,
            "week_52_low": 0.0,
            "market_cap": 0.0,
            "pe_ratio": 0.0,
            "depth": {"bids": bids, "asks": asks},
            "is_market_open": self._explore_market_status() == "OPEN",
            "last_updated": datetime.now(IST).isoformat(),
        }

    def _pad_depth(self, levels) -> list[dict]:
        levels = list(levels) if levels else []
        padded = []
        for i in range(5):
            level = levels[i] if i < len(levels) else {}
            padded.append({
                "price": round(self._as_float(level.get("price")), 2),
                "qty": self._as_int(level.get("qty")),
                "orders": self._as_int(level.get("orders")),
            })
        return padded

    # ------------------------------------------------------------------
    # Chart
    # ------------------------------------------------------------------

    async def get_chart(self, shoonya, exchange: str, symbol: str, period: str) -> dict | None:
        """Returns None if the symbol/exchange pair is unknown (caller returns
        404). A valid symbol with no broker data available (disconnected,
        timeout, no candles) returns an empty candle list rather than raising
        - a chart with no data is a normal, renderable state for the
        frontend, unlike a missing quote."""
        record = self._symbol_master.resolve(exchange, symbol)
        if record is None:
            return None

        config = _CHART_PERIODS.get(period)
        if config is None:
            config = _CHART_PERIODS["1d"]
            period = "1d"

        candles = await self._fetch_candles(shoonya, record, config)
        return {"symbol": record["symbol"], "period": period, "candles": candles}

    async def _fetch_candles(self, shoonya, record: dict, config: dict) -> list[dict]:
        if shoonya is None or not shoonya.is_connected:
            return []

        loop = asyncio.get_running_loop()
        try:
            raw_candles = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: shoonya.get_time_price_series(
                        record["exchange"], record["token"], config["interval"], days=config["days"]
                    )
                ),
                timeout=_CHART_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[StocksService] chart timeout for {record['exchange']}:{record['symbol']}")
            return []
        except Exception as exc:
            logger.warning(f"[StocksService] chart fetch failed for {record['exchange']}:{record['symbol']}: {exc}")
            return []

        if not raw_candles:
            return []

        parsed = self._parse_raw_candles(raw_candles)
        if not parsed:
            return []

        bucket = config.get("bucket")
        if bucket == "day":
            return self._aggregate(parsed, self._day_key)
        if bucket == "week":
            return self._aggregate(parsed, self._week_key)
        return [self._candle_to_dict(c) for c in parsed]

    def _parse_raw_candles(self, raw_candles: list[dict]) -> list[dict]:
        parsed = []
        for candle in raw_candles:
            timestamp = candle.get("timestamp")
            try:
                dt = datetime.strptime(timestamp, "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)
            except (TypeError, ValueError):
                continue
            parsed.append({
                "dt": dt,
                "open": self._as_float(candle.get("open")),
                "high": self._as_float(candle.get("high")),
                "low": self._as_float(candle.get("low")),
                "close": self._as_float(candle.get("close")),
                "volume": self._as_int(candle.get("volume")),
            })
        return parsed

    def _day_key(self, dt: datetime) -> date:
        return dt.date()

    def _week_key(self, dt: datetime) -> tuple[int, int]:
        iso = dt.isocalendar()
        return (iso[0], iso[1])

    def _aggregate(self, candles: list[dict], key_fn) -> list[dict]:
        buckets: dict = {}
        order: list = []
        for candle in candles:
            key = key_fn(candle["dt"])
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(candle)

        result = []
        for key in order:
            group = buckets[key]
            result.append({
                "timestamp": int(group[0]["dt"].timestamp()),
                "open": round(group[0]["open"], 2),
                "high": round(max(c["high"] for c in group), 2),
                "low": round(min(c["low"] for c in group if c["low"] > 0), 2) if any(c["low"] > 0 for c in group) else 0.0,
                "close": round(group[-1]["close"], 2),
                "volume": sum(c["volume"] for c in group),
            })
        return result

    def _candle_to_dict(self, candle: dict) -> dict:
        return {
            "timestamp": int(candle["dt"].timestamp()),
            "open": round(candle["open"], 2),
            "high": round(candle["high"], 2),
            "low": round(candle["low"], 2),
            "close": round(candle["close"], 2),
            "volume": candle["volume"],
        }
