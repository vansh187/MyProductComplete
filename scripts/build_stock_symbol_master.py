"""
Downloads Shoonya's daily NSE + BSE equity scrip master and rebuilds
appconfig/stock_symbol_master.json - the file appconfig/StockSymbolMaster.py
loads to back /api/stocks/search, /api/stocks/facets and every symbol ->
token lookup the stocks feature needs.

Run manually, or on a schedule (pre-market), whenever the master should be
refreshed:
    python scripts/build_stock_symbol_master.py

Output: appconfig/stock_symbol_master.json
    [ { "symbol", "exchange", "token", "name", "sector", "lot_size", "tick_size" }, ... ]

Sectors are not present in Shoonya's scrip master, so this backfills them
from appconfig/nifty50_watchlist.json's curated symbol -> sector mapping
where available; everything else is left without a sector (StockSymbolMaster
treats that as "uncategorised" - it is simply excluded from the sectors
facet and from sector-filtered searches, never a crash).

Deliberately a standalone, manually-run script (same convention as
scripts/build_option_master.py / scripts/build_future_master.py) rather than
something imported at app startup - a network hiccup or a malformed CSV row
here must never be able to prevent the API process itself from starting.
"""

import csv
import io
import json
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

_OUTPUT_FILE = Path(__file__).parent.parent / "appconfig" / "stock_symbol_master.json"
_SECTOR_MAP_FILE = Path(__file__).parent.parent / "appconfig" / "nifty50_watchlist.json"

_SCRIP_MASTER_URLS = {
    "NSE": "https://api.shoonya.com/NSE_symbols.txt.zip",
    "BSE": "https://api.shoonya.com/BSE_symbols.txt.zip",
}

# Shoonya's own equity-cash instrument marker in the "Instrument" column.
_EQUITY_INSTRUMENT = "EQ"


class StockSymbolMasterBuilder:
    """Instance-based (no static/class methods) so the download + parse +
    sector-enrichment pipeline can be exercised end to end from a test with
    fakes injected through the constructor, same convention as the rest of
    this codebase's newer modules (mutualfunds/*, marketengine/ShoonyaStockFeed.py)."""

    def __init__(self, urls: dict[str, str] | None = None, sector_map_file: Path | None = None,
                 output_file: Path | None = None, timeout_secs: float = 60.0):
        self._urls = urls or _SCRIP_MASTER_URLS
        self._sector_map_file = sector_map_file or _SECTOR_MAP_FILE
        self._output_file = output_file or _OUTPUT_FILE
        self._timeout_secs = timeout_secs

    def build(self) -> list[dict]:
        sector_map = self._load_sector_map()
        records: dict[tuple[str, str], dict] = {}

        for exchange, url in self._urls.items():
            try:
                rows = self._download_and_parse(exchange, url)
            except Exception as exc:
                print(f"[build_stock_symbol_master] {exchange} download/parse failed, skipping: {exc}")
                continue

            for row in rows:
                key = (row["exchange"], row["symbol"])
                row["sector"] = sector_map.get(row["symbol"])
                records[key] = row

            print(f"[build_stock_symbol_master] {exchange}: {len(rows)} equity symbols parsed")

        return list(records.values())

    def write(self, records: list[dict]) -> None:
        self._output_file.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[build_stock_symbol_master] Wrote {len(records)} symbols to {self._output_file}")

    def _load_sector_map(self) -> dict[str, str]:
        try:
            entries = json.loads(self._sector_map_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            print(f"[build_stock_symbol_master] Could not read sector map {self._sector_map_file}: {exc}")
            return {}

        sector_map = {}
        for entry in entries if isinstance(entries, list) else []:
            symbol = entry.get("symbol")
            sector = entry.get("sector")
            if symbol and sector:
                sector_map[str(symbol).strip().upper()] = str(sector).strip()
        return sector_map

    def _download_and_parse(self, exchange: str, url: str) -> list[dict]:
        response = urlopen(url, timeout=self._timeout_secs)
        archive = ZipFile(io.BytesIO(response.read()))

        namelist = archive.namelist()
        if not namelist:
            raise ValueError(f"{url} returned an empty archive")

        content = archive.read(namelist[0]).decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(content))

        parsed = []
        for row in reader:
            record = self._parse_row(exchange, row)
            if record is not None:
                parsed.append(record)
        return parsed

    def _parse_row(self, exchange: str, row: dict) -> dict | None:
        instrument = (row.get("Instrument") or "").strip().upper()
        if instrument and instrument != _EQUITY_INSTRUMENT:
            return None

        symbol = (row.get("Symbol") or "").strip().upper()
        token = (row.get("Token") or "").strip()
        if not symbol or not token:
            return None

        name = (row.get("TradingSymbol") or symbol).strip()
        lot_size = self._safe_int(row.get("LotSize"), 1)
        tick_size = self._safe_float(row.get("TickSize"), 0.05)

        return {
            "symbol": symbol,
            "exchange": exchange,
            "token": token,
            "name": name,
            "lot_size": lot_size,
            "tick_size": tick_size,
        }

    def _safe_int(self, value, default: int) -> int:
        try:
            return int(float(value)) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def _safe_float(self, value, default: float) -> float:
        try:
            return float(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default


def main() -> None:
    builder = StockSymbolMasterBuilder()
    records = builder.build()
    if not records:
        print("[build_stock_symbol_master] No records produced - leaving existing output file untouched")
        return
    builder.write(records)


if __name__ == "__main__":
    main()
