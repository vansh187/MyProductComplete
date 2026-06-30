import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path


class StockWatchlist:
    """Loads the Nifty 50 stock list from a JSON config file."""

    def __init__(self, config_path: str | Path):
        with open(config_path, "r") as fh:
            self._stocks: list[dict] = json.load(fh)

    def stocks(self) -> list[dict]:
        return self._stocks


class TopMoversFetcher:
    """
    Fetches live quotes for all watchlist stocks in parallel via Shoonya,
    then returns the top N gainers and top N losers sorted by change_pct.
    """

    def __init__(self, watchlist: StockWatchlist, top_n: int = 5):
        self._watchlist = watchlist
        self._top_n = top_n

    async def fetch_top_movers(self, shoonya, is_open: bool) -> dict:
        loop = asyncio.get_running_loop()

        async def _fetch_one(stock: dict) -> dict | None:
            try:
                quote = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda ex=stock["exchange"], tk=stock["token"]:
                            shoonya.get_index_quote(ex, tk)
                    ),
                    timeout=5.0
                )
                if quote is None:
                    return None
                return {
                    "symbol":     stock["symbol"],
                    "name":       stock["name"],
                    "sector":     stock["sector"],
                    "ltp":        quote["ltp"],
                    "change_pct": quote["change_pct"],
                    "change":     quote["change"],
                }
            except (asyncio.TimeoutError, Exception):
                return None

        raw = await asyncio.gather(*[_fetch_one(s) for s in self._watchlist.stocks()])
        valid = [r for r in raw if r is not None]

        valid.sort(key=lambda x: x["change_pct"], reverse=True)
        n = self._top_n
        if len(valid) >= 2 * n:
            gainers = valid[:n]
            losers  = list(reversed(valid[-n:]))
        elif len(valid) > n:
            gainers = valid[:n]
            losers  = list(reversed(valid[n:]))
        else:
            gainers = valid[:]
            losers  = []

        return {
            "market_status": "open" if is_open else "closed",
            "gainers":       gainers,
            "losers":        losers,
            "total_tracked": len(valid),
            "last_updated":  datetime.now(timezone.utc).isoformat(),
        }


class TopMoversCache:
    """
    Thread-safe in-memory cache for top movers data.
    Uses asyncio.Condition so SSE clients wake up exactly when cache is refreshed.
    """

    def __init__(self):
        self._data:       dict | None = None
        self._generation: int         = 0
        self._condition               = asyncio.Condition()

    def get(self) -> dict | None:
        return self._data

    @property
    def generation(self) -> int:
        return self._generation

    async def update(self, data: dict) -> None:
        async with self._condition:
            self._data       = data
            self._generation += 1
            self._condition.notify_all()

    async def wait_for_next(self, after_generation: int) -> dict:
        """Blocks until the cache is updated past `after_generation`."""
        async with self._condition:
            await self._condition.wait_for(lambda: self._generation > after_generation)
            return self._data
