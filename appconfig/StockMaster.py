import json
from pathlib import Path

_FILE = Path(__file__).parent / "master_stocks.json"

# Structure in JSON: { "Company Name": "TICKER_SYMBOL" }
_raw: dict[str, str] = json.loads(_FILE.read_text(encoding="utf-8"))

# Invert to { "TICKER_SYMBOL": { "symbol": "TICKER_SYMBOL", "name": "Company Name" } }
# so the rest of the codebase can look up by ticker (which is what Breeze expects)
_symbol_map: dict[str, dict] = {
    ticker: {"symbol": ticker, "name": name}
    for name, ticker in _raw.items()
}


def get_all_symbols() -> list[str]:
    """All ticker symbols from the master list — used to build the Breeze subscription watchlist."""
    return list(_symbol_map.keys())


def get_symbol_map() -> dict[str, dict]:
    """Ticker → metadata map — used for name enrichment in API responses."""
    return _symbol_map


def is_valid_symbol(symbol: str) -> bool:
    return symbol in _symbol_map
