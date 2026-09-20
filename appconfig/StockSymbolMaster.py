"""
In-memory equity symbol master backing /api/stocks/search, /api/stocks/facets
and every symbol -> Shoonya token lookup the stocks feature needs.

Primary source: appconfig/stock_symbol_master.json, built by
scripts/build_stock_symbol_master.py from Shoonya's daily NSE/BSE scrip
master dump. That file is large and refreshed daily, so it is NOT bundled
in the repo - a fresh checkout (or a checkout where the refresh script has
never run) has no such file, and the whole stocks feature must still start
and answer requests. In that case this class falls back to the small,
already-committed appconfig/nifty50_watchlist.json list (the same file
service/topMovers/TopMoversService.py already trusts) so /explore, /search
etc. keep working with a reduced universe instead of the app failing to
start or every stocks endpoint 500ing.

Instance-based (constructor injection of the file paths) - no module-level
globals, no static/class methods - so tests can point it at fixture files.
"""

import asyncio
import json
from pathlib import Path


class StockSymbolMaster:

    def __init__(self, master_file: Path | str | None = None, fallback_file: Path | str | None = None):
        base_dir = Path(__file__).parent
        self._master_file = Path(master_file) if master_file else base_dir / "stock_symbol_master.json"
        self._fallback_file = Path(fallback_file) if fallback_file else base_dir / "nifty50_watchlist.json"
        self._records: list[dict] = []
        self._by_key: dict[tuple[str, str], dict] = {}
        self.reload()

    def reload(self) -> None:
        """Re-reads the master (or fallback) file from disk without restarting the process.
        Never raises - a missing/corrupt file degrades to an empty universe rather than
        taking the rest of the app down, matching appconfig/OptionMaster.py's convention."""
        records = self._load_file(self._master_file)
        source = "master"
        if not records:
            records = self._load_fallback()
            source = "fallback"

        by_key: dict[tuple[str, str], dict] = {}
        for record in records:
            try:
                key = (str(record["exchange"]).upper(), str(record["symbol"]).upper())
            except (KeyError, TypeError):
                continue
            by_key[key] = record

        self._records = list(by_key.values())
        self._by_key = by_key
        print(f"[StockSymbolMaster] Loaded {len(self._records)} symbols from {source}")

    def _load_file(self, path: Path) -> list[dict]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            print(f"[StockSymbolMaster] Could not read {path}: {exc}")
            return []

        if not isinstance(raw, list):
            print(f"[StockSymbolMaster] {path} did not contain a JSON array - ignoring")
            return []

        cleaned = []
        for entry in raw:
            normalized = self._normalize_record(entry)
            if normalized is not None:
                cleaned.append(normalized)
        return cleaned

    def _load_fallback(self) -> list[dict]:
        try:
            raw = json.loads(self._fallback_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            print(f"[StockSymbolMaster] Could not read fallback {self._fallback_file}: {exc}")
            return []

        cleaned = []
        for entry in raw if isinstance(raw, list) else []:
            normalized = self._normalize_record(entry)
            if normalized is not None:
                cleaned.append(normalized)
        return cleaned

    def _normalize_record(self, entry: dict) -> dict | None:
        if not isinstance(entry, dict):
            return None
        symbol = str(entry.get("symbol") or "").strip().upper()
        exchange = str(entry.get("exchange") or "").strip().upper()
        token = str(entry.get("token") or "").strip()
        if not symbol or not exchange or not token:
            return None
        return {
            "symbol": symbol,
            "exchange": exchange,
            "token": token,
            "name": str(entry.get("name") or symbol).strip(),
            "sector": (str(entry.get("sector")).strip() if entry.get("sector") else None),
            "lot_size": self._safe_int(entry.get("lot_size"), 1),
            "tick_size": self._safe_float(entry.get("tick_size"), 0.05),
        }

    def _safe_int(self, value, default: int) -> int:
        try:
            return int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def _safe_float(self, value, default: float) -> float:
        try:
            return float(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def resolve(self, exchange: str, symbol: str) -> dict | None:
        if not exchange or not symbol:
            return None
        return self._by_key.get((exchange.strip().upper(), symbol.strip().upper()))

    def facets(self) -> dict:
        exchanges = sorted({r["exchange"] for r in self._records})
        sectors = sorted({r["sector"] for r in self._records if r.get("sector")})
        return {"exchanges": exchanges, "sectors": sectors}

    def search(self, query: str | None, exchange: str | None, sector: str | None,
               page: int, page_size: int) -> list[dict]:
        page = max(page, 1)
        page_size = max(min(page_size, 200), 1)

        exchange_filter = exchange.strip().upper() if exchange else None
        sector_filter = sector.strip() if sector else None

        candidates = [
            r for r in self._records
            if (exchange_filter is None or r["exchange"] == exchange_filter)
            and (sector_filter is None or (r.get("sector") or "").lower() == sector_filter.lower())
        ]

        query_norm = query.strip().upper() if query else None
        if query_norm:
            prefix_matches = []
            name_matches = []
            for record in candidates:
                if record["symbol"].startswith(query_norm):
                    prefix_matches.append(record)
                elif query_norm in record["name"].upper():
                    name_matches.append(record)
            prefix_matches.sort(key=lambda r: r["symbol"])
            name_matches.sort(key=lambda r: r["symbol"])
            ranked = prefix_matches + name_matches
        else:
            ranked = sorted(candidates, key=lambda r: r["symbol"])

        offset = (page - 1) * page_size
        return ranked[offset:offset + page_size]

    def all_records(self) -> list[dict]:
        return list(self._records)

    @property
    def count(self) -> int:
        return len(self._records)


async def schedule_daily_refresh(app) -> None:
    """
    Background task (started from app.py lifespan, same convention as
    appconfig/OptionMaster.py's own schedule_daily_refresh): re-downloads
    Shoonya's NSE/BSE scrip master once a day and reloads
    app.state.stock_symbol_master from the result, so newly listed symbols
    show up without a process restart.

    A plain module-level function rather than a method on StockSymbolMaster
    itself - it orchestrates a *different* object's lifecycle (the builder
    script) across the whole app's lifespan, which isn't state any single
    StockSymbolMaster instance owns; mirrors every other *_daily_refresh
    function already in this codebase (appconfig/OptionMaster.py,
    marketengine/ShoonyaConnection.py).
    """
    from scripts.build_stock_symbol_master import StockSymbolMasterBuilder

    while True:
        await asyncio.sleep(24 * 3600)
        try:
            builder = StockSymbolMasterBuilder()
            loop = asyncio.get_running_loop()
            records = await loop.run_in_executor(None, builder.build)
            if not records:
                print("[StockSymbolMaster] Daily refresh produced no records - keeping existing master")
                continue
            await loop.run_in_executor(None, builder.write, records)
            master = getattr(app.state, "stock_symbol_master", None)
            if master is not None:
                master.reload()
            print(f"[StockSymbolMaster] Daily refresh complete - {len(records)} symbols")
        except Exception as exc:
            print(f"[StockSymbolMaster] Daily refresh failed: {exc}")
