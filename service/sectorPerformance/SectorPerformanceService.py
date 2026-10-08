import asyncio
import json
from pathlib import Path
import logging

from utils.safe_numbers import safe_float

logger = logging.getLogger(__name__)


class SectorIndexRegistry:
    """Loads and provides the sector → Shoonya token mapping from a JSON config file."""

    def __init__(self, config_path: str | Path):
        with open(config_path, "r") as fh:
            self._sectors: list[dict] = json.load(fh)

    def sectors(self) -> list[dict]:
        return self._sectors

    def instrument_keys(self) -> list[str]:
        """'EXCH|TOKEN' of every sector index, for pinning on the live feed."""
        return [
            f"{sector['exchange']}|{sector['token']}"
            for sector in self._sectors
            if sector.get("exchange") and sector.get("token")
        ]


class SectorPerformanceFetcher:
    """Sector index quotes: live WebSocket ticks first (in-memory), REST only
    for a sector with no usable tick yet."""

    def __init__(self, registry: SectorIndexRegistry):
        self._registry = registry

    def _quote_from_tick(self, tick_source, sector: dict) -> dict | None:
        if tick_source is None:
            return None
        try:
            tick = tick_source.get_tick(f"{sector['exchange']}|{sector['token']}")
        except Exception as exc:
            logger.debug(f"[SectorPerf] get_tick failed for {sector['sector']}: {exc}")
            return None
        if not tick:
            return None
        ltp = safe_float(tick.get("ltp"))
        prev_close = safe_float(tick.get("close"))
        if not ltp or not prev_close or ltp <= 0 or prev_close <= 0:
            return None
        change = round(ltp - prev_close, 2)
        return {
            "sector":     sector["sector"],
            "change_pct": round(change / prev_close * 100, 2),
            "change":     change,
            "ltp":        ltp,
        }

    async def fetch_all(self, shoonya, tick_source=None, rest_used: list | None = None) -> tuple[list[dict], list[dict]]:
        """tick_source: anything with get_tick('EXCH|TOKEN') (the stock tick
        cache). rest_used, when given, collects the sectors that needed REST."""
        from_ticks = {}
        for sector in self._registry.sectors():
            quote = self._quote_from_tick(tick_source, sector)
            if quote is not None:
                from_ticks[sector["sector"]] = quote
        if len(from_ticks) == len(self._registry.sectors()):
            return [from_ticks[sector["sector"]] for sector in self._registry.sectors()], []

        missing = [sector for sector in self._registry.sectors() if sector["sector"] not in from_ticks]
        if rest_used is not None:
            rest_used.extend(sector["sector"] for sector in missing)
        rest_results, errors = await self._fetch_rest(shoonya, missing, tick_source)
        by_sector = {**from_ticks, **{result["sector"]: result for result in rest_results}}
        ordered = [by_sector[sector["sector"]] for sector in self._registry.sectors() if sector["sector"] in by_sector]
        return ordered, errors

    def _remember_close(self, tick_source, sector: dict, quote: dict) -> None:
        """A tick lacking only the previous close is complete from now on."""
        remember_close = getattr(tick_source, "remember_close", None)
        if callable(remember_close):
            try:
                remember_close(f"{sector['exchange']}|{sector['token']}", quote.get("prev_close"))
            except Exception as exc:
                logger.debug(f"[SectorPerf] remember_close failed for {sector['sector']}: {exc}")

    async def _fetch_rest(self, shoonya, sectors: list[dict], tick_source=None) -> tuple[list[dict], list[dict]]:
        loop = asyncio.get_running_loop()

        async def _fetch_one(sector: dict) -> tuple[dict | None, dict | None]:
            try:
                quote = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda ex=sector["exchange"], tk=sector["token"]:
                            shoonya.get_index_quote(ex, tk)
                    ),
                    timeout=8.0
                )
                if quote is None:
                    logger.warning(f"[SectorPerf] No data for {sector['sector']} ({sector['token']})")
                    return None, {"sector": sector["sector"], "reason": "no_data"}
                self._remember_close(tick_source, sector, quote)
                return {
                    "sector":     sector["sector"],
                    "change_pct": quote["change_pct"],
                    "change":     quote["change"],
                    "ltp":        quote["ltp"],
                }, None
            except asyncio.TimeoutError:
                logger.warning(f"[SectorPerf] Timeout for {sector['sector']} ({sector['token']})")
                return None, {"sector": sector["sector"], "reason": "timeout"}
            except Exception as exc:
                logger.warning(f"[SectorPerf] Error for {sector['sector']} ({sector['token']}): {exc}")
                return None, {"sector": sector["sector"], "reason": str(exc)}

        # Fetch in batches of 2 to avoid overwhelming Shoonya with 8 concurrent requests
        results = []
        errors = []

        for i in range(0, len(sectors), 2):
            batch = sectors[i:i+2]
            pairs = await asyncio.gather(*[_fetch_one(s) for s in batch])
            results.extend([p[0] for p in pairs if p[0] is not None])
            errors.extend([p[1] for p in pairs if p[1] is not None])
            # Small delay between batches to prevent overwhelming Shoonya
            if i + 2 < len(sectors):
                await asyncio.sleep(0.2)

        return results, errors
