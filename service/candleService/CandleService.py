"""
OHLC candle data service for F&O trading terminal.
Fetches historical candles from Shoonya for charting.
"""

from decimal import Decimal


class CandleService:
    """Provides OHLC candle data for indices and stocks."""

    def __init__(self):
        pass

    # Shoonya's TPSeries endpoint only supports minute-granularity candles
    # (1, 3, 5, 15, 60 minutes). Sub-minute (5s/10s) and daily (1d) candles
    # are not available through this endpoint.
    _SUPPORTED_INTERVALS = {
        "1m": "1",
        "3m": "3",
        "5m": "5",
        "15m": "15",
        "1h": "60",
    }

    # Single source of truth for API-layer validation (api/candles.py) so the
    # accepted-timeframe whitelist can't drift out of sync with what this
    # service can actually fulfill.
    SUPPORTED_TIMEFRAMES = tuple(_SUPPORTED_INTERVALS.keys())

    @classmethod
    def _normalize_interval(cls, timeframe: str) -> str | None:
        """Convert API timeframe param to Shoonya interval format, or None if unsupported."""
        return cls._SUPPORTED_INTERVALS.get(timeframe)

    @staticmethod
    def _format_candle(raw_candle: dict) -> dict:
        """Normalize candle dict to response format."""
        return {
            "timestamp": raw_candle.get("timestamp"),
            "open": round(float(raw_candle.get("open", 0)), 2),
            "high": round(float(raw_candle.get("high", 0)), 2),
            "low": round(float(raw_candle.get("low", 0)), 2),
            "close": round(float(raw_candle.get("close", 0)), 2),
            "volume": raw_candle.get("volume", 0),
        }

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

            raw_candles = shoonya.get_time_price_series(exchange, token, interval, days=1)

            if not raw_candles:
                errors.append({
                    "exchange": exchange,
                    "token": token,
                    "timeframe": timeframe,
                    "reason": "no_candle_data"
                })
                return candles, errors

            # Trim to requested limit (most recent candles)
            result_candles = raw_candles[-limit:] if len(raw_candles) > limit else raw_candles

            # Normalize each candle
            candles = [self._format_candle(c) for c in result_candles]

        except Exception as e:
            print(f"[CandleService] Error fetching candles {exchange}:{token} {timeframe}: {e}")
            errors.append({
                "exchange": exchange,
                "token": token,
                "timeframe": timeframe,
                "reason": str(e)
            })

        return candles, errors
