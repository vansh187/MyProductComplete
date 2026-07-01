import asyncio
import json
from pathlib import Path


class SectorIndexRegistry:
    """Loads and provides the sector → Shoonya token mapping from a JSON config file."""

    def __init__(self, config_path: str | Path):
        with open(config_path, "r") as fh:
            self._sectors: list[dict] = json.load(fh)

    def sectors(self) -> list[dict]:
        return self._sectors


class SectorPerformanceFetcher:
    """Fetches live NSE sector index quotes in parallel via Shoonya."""

    def __init__(self, registry: SectorIndexRegistry):
        self._registry = registry

    async def fetch_all(self, shoonya) -> tuple[list[dict], list[dict]]:
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
                    return None, {"sector": sector["sector"], "reason": "no_data"}
                return {
                    "sector":     sector["sector"],
                    "change_pct": quote["change_pct"],
                    "change":     quote["change"],
                    "ltp":        quote["ltp"],
                }, None
            except asyncio.TimeoutError:
                return None, {"sector": sector["sector"], "reason": "timeout"}
            except Exception as exc:
                return None, {"sector": sector["sector"], "reason": str(exc)}

        pairs = await asyncio.gather(*[_fetch_one(s) for s in self._registry.sectors()])
        results = [p[0] for p in pairs if p[0] is not None]
        errors  = [p[1] for p in pairs if p[1] is not None]
        return results, errors
