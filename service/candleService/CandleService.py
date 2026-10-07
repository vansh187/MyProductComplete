"""
OHLC candle data service for F&O trading terminal.
Fetches historical candles from Shoonya for charting.

Deliberately instance-based (no static/class methods or class-level state).
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# How many calendar days of history to request from the broker. This must be
# wide enough to reach back past weekends/holidays to the last trading day
# (e.g. a long weekend + a holiday Monday can leave 3+ closed days in a row),
# not just "1 day back from now".
_LOOKBACK_DAYS = 10

BROKER_CALL_TIMEOUT_SECS = 10.0


class CandleService:
    """Provides OHLC candle data for indices and stocks."""

    def __init__(self):
        # Shoonya's TPSeries endpoint only supports minute-granularity candles
        # (1, 3, 5, 15, 60 minutes). Sub-minute (5s/10s) and daily (1d) candles
        # are not available through this endpoint.
        self._supported_intervals = {
            "1m": "1",
            "3m": "3",
            "5m": "5",
            "15m": "15",
            "1h": "60",
        }
        # Single source of truth for API-layer validation (api/candles.py) so
        # the accepted-timeframe whitelist can't drift out of sync with what
        # this service can actually fulfill.
        self.supported_timeframes = tuple(self._supported_intervals.keys())

    def is_supported_timeframe(self, timeframe: str) -> bool:
        return timeframe in self._supported_intervals

    def _normalize_interval(self, timeframe: str) -> str | None:
        """Convert API timeframe param to Shoonya interval format, or None if unsupported."""
        return self._supported_intervals.get(timeframe)

    def _filter_to_last_trading_day(self, raw_candles: list[dict]) -> list[dict]:
        """Keep only the candles belonging to the most recent date present.

        raw_candles is assumed chronologically sorted (oldest first, as
        returned by ShoonyaConnection.get_time_price_series). Timestamps are
        ISO 8601 ('2026-07-01T09:15:00+05:30'), so the IST calendar date is
        simply the first 10 characters - no per-candle datetime parsing.
        """
        try:
            last_date = None
            for candle in reversed(raw_candles):
                ts = candle.get("timestamp")
                if isinstance(ts, str) and len(ts) >= 10:
                    last_date = ts[:10]
                    break

            if last_date is None:
                return raw_candles

            return [
                candle for candle in raw_candles
                if isinstance(candle.get("timestamp"), str) and candle["timestamp"][:10] == last_date
            ]
        except Exception as e:
            logger.warning(f"[CandleService] last-trading-day filter failed: {e}")
            return raw_candles

    def _format_candle(self, raw_candle: dict) -> dict | None:
        """Normalize candle dict to response format. None if unusable."""
        try:
            return {
                "timestamp": raw_candle.get("timestamp"),
                "open": round(float(raw_candle.get("open", 0)), 2),
                "high": round(float(raw_candle.get("high", 0)), 2),
                "low": round(float(raw_candle.get("low", 0)), 2),
                "close": round(float(raw_candle.get("close", 0)), 2),
                "volume": raw_candle.get("volume", 0),
            }
        except (TypeError, ValueError):
            return None

    async def get_index_candles(self, shoonya, exchange: str, token: str, timeframe: str, limit: int = 100) -> tuple[list[dict], list[dict]]:
        """
        Fetch OHLC candles for an index/security.

        Returns:
            (candles_list, errors_list)
        """
        candles = []
        errors = []

        if not shoonya or not shoonya.is_connected:
            errors.append({"reason": "shoonya_disconnected"})
            return candles, errors

        try:
            interval = self._normalize_interval(timeframe)
            if interval is None:
                errors.append({
                    "exchange": exchange,
                    "token": token,
                    "timeframe": timeframe,
                    "reason": "interval_not_supported_by_broker"
                })
                return candles, errors

            # Bounded like every other Shoonya REST call in this codebase
            # (OptionChainService, TopMoversService, SectorPerformanceService,
            # marketquotes._fetch_index_quote) - an un-timeout'd hang here
            # blocks this request indefinitely and leaks a threadpool worker
            # for as long as the broker call never returns.
            loop = asyncio.get_running_loop()
            raw_candles = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: shoonya.get_time_price_series(exchange, token, interval, days=_LOOKBACK_DAYS)
                ),
                timeout=BROKER_CALL_TIMEOUT_SECS
            )

            if not raw_candles:
                errors.append({
                    "exchange": exchange,
                    "token": token,
                    "timeframe": timeframe,
                    "reason": "no_candle_data"
                })
                return candles, errors

            # raw_candles may span several calendar days (weekends/holidays
            # widen the lookback window above). Keep only the most recent
            # trading day present so a Sunday/holiday request still shows a
            # full, coherent session (e.g. Friday's candles) instead of a
            # blend of the last two trading days.
            last_day_candles = self._filter_to_last_trading_day(raw_candles)

            # Trim to requested limit (most recent candles)
            result_candles = last_day_candles[-limit:] if len(last_day_candles) > limit else last_day_candles

            candles = [c for c in (self._format_candle(raw) for raw in result_candles) if c is not None]

        except Exception as e:
            logger.warning(f"[CandleService] Error fetching candles {exchange}:{token} {timeframe}: {e}")
            errors.append({
                "exchange": exchange,
                "token": token,
                "timeframe": timeframe,
                "reason": str(e) or type(e).__name__
            })

        return candles, errors
