"""
OHLC candle data service for F&O trading terminal.
Fetches historical candles from Shoonya for charting.
"""

from decimal import Decimal


class CandleService:
    """Provides OHLC candle data for indices and stocks."""

    def __init__(self):
        pass

    @staticmethod
    def _normalize_interval(timeframe: str) -> str:
        """Convert API timeframe param to Shoonya interval format."""
        mapping = {
            "5s": "5second",
            "10s": "10second",
            "1m": "1minute",
            "3m": "3minute",
            "5m": "5minute",
            "15m": "15minute",
            "1h": "1hour",
            "1d": "1day",
        }
        return mapping.get(timeframe, "1minute")

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
