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
                    print(f"[SectorPerf] No data for {sector['sector']} ({sector['token']})")
                    return None, {"sector": sector["sector"], "reason": "no_data"}
                return {
                    "sector":     sector["sector"],
                    "change_pct": quote["change_pct"],
                    "change":     quote["change"],
                    "ltp":        quote["ltp"],
                }, None
            except asyncio.TimeoutError:
                print(f"[SectorPerf] Timeout for {sector['sector']} ({sector['token']})")
                return None, {"sector": sector["sector"], "reason": "timeout"}
            except Exception as exc:
                print(f"[SectorPerf] Error for {sector['sector']} ({sector['token']}): {exc}")
                return None, {"sector": sector["sector"], "reason": str(exc)}

        # Fetch in batches of 2 to avoid overwhelming Shoonya with 8 concurrent requests
        sectors = self._registry.sectors()
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
