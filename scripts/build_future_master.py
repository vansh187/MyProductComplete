"""
One-time script: downloads Shoonya's NFO and BFO symbol masters (the same
files build_option_master.py uses) and builds appconfig/master_futures.json
with the NIFTY/BANKNIFTY/FINNIFTY (NSE, via NFO) and SENSEX (BSE, via BFO)
index futures contracts (one contract per expiry - no strikes/CE/PE, unlike
options) for every available expiry.

This exists to replace the symbol-suffix heuristic
(utils/instrumentClassifier.looks_like_future_symbol) that
service/marginengine/margin_engine.py's resolve_contract_type() previously
had to fall back on for every futures order, with token=None/expiry=None/a
guessed lot_size - which silently produced wrong margin (no near-expiry
multiplier, since expiry was always unknown) instead of using real contract
data. Run once (or periodically, e.g. daily):
    python scripts/build_future_master.py

Output: appconfig/master_futures.json
{
  "NIFTY": {
    "expiries": ["2026-07-28", "2026-08-25", ...],
    "2026-07-28": {"token": "61093", "tsym": "NIFTY28JUL26F", "lot_size": 65}
  },
  "BANKNIFTY": {...},
  "FINNIFTY": {...},
  "SENSEX": {...}
}
"""

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

NFO_MASTER_URL = "https://api.shoonya.com/NFO_symbols.txt.zip"
BFO_MASTER_URL = "https://api.shoonya.com/BFO_symbols.txt.zip"
OUTPUT_FILE = Path(__file__).parent.parent / "appconfig" / "master_futures.json"

# Mirrors NFO_UNDERLYINGS/BFO_UNDERLYINGS in build_option_master.py - same
# tracked underlyings, same exchange split (NSE indices on NFO, Sensex on BFO).
NFO_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY"}
BFO_UNDERLYINGS = {"SENSEX"}
TRACKED_UNDERLYINGS = NFO_UNDERLYINGS | BFO_UNDERLYINGS

# Unlike NFO (whose futures Symbol column is already "NIFTY"/"BANKNIFTY"/
# "FINNIFTY" verbatim), BFO's Sensex futures Symbol column is "SX50FUT" -
# confirmed from a live BFO_symbols.txt.zip - which must be remapped to our
# internal "SENSEX" name. Distinct from BFO_SYMBOL_TO_UNDERLYING in
# build_option_master.py (options use "BSXOPT") since futures and options
# use different product codes on the same exchange.
BFO_SYMBOL_TO_UNDERLYING = {
    "SX50FUT": "SENSEX",
}


def _parse_expiry(raw: str) -> str:
    """Shoonya's Expiry column is 'DD-MMM-YYYY' (e.g. '29-SEP-2026') -> ISO 'YYYY-MM-DD'."""
    return datetime.strptime(raw.strip(), "%d-%b-%Y").strftime("%Y-%m-%d")


def parse_futures_csv(content: str, tracked: set[str] = NFO_UNDERLYINGS, symbol_map: dict[str, str] | None = None) -> dict:
    """
    Parses a Shoonya *_symbols.txt CSV content, keeping only index futures
    rows (Instrument == "FUTIDX") for the given tracked underlyings.
    Returns { underlying: { expiries: [...], "<iso-expiry>": {token, tsym, lot_size} } }.

    symbol_map: raw file Symbol -> our internal underlying name (see
    BFO_SYMBOL_TO_UNDERLYING above). Rows whose (mapped) symbol isn't in
    `tracked` are skipped.

    Pure function - no I/O - so it can be unit-tested against a fixture.
    """
    reader = csv.DictReader(io.StringIO(content))
    symbol_map = symbol_map or {}

    contracts: dict[str, dict] = {u: {} for u in tracked}

    for row in reader:
        raw_symbol = (row.get("Symbol") or "").strip()
        underlying = symbol_map.get(raw_symbol, raw_symbol)
        instrument = (row.get("Instrument") or "").strip()
        if underlying not in tracked or instrument != "FUTIDX":
            continue

        try:
            expiry_iso = _parse_expiry(row["Expiry"])
            lot_size = int(row["LotSize"])
        except (KeyError, ValueError):
            continue

        contracts[underlying][expiry_iso] = {
            "token": row["Token"].strip(),
            "tsym": row["TradingSymbol"].strip(),
            "lot_size": lot_size,
        }

    result: dict[str, dict] = {}
    for underlying, expiry_map in contracts.items():
        expiries_sorted = sorted(expiry_map.keys())
        result[underlying] = {"expiries": expiries_sorted, **expiry_map}

    return result


def _download_and_parse(url: str, tracked: set[str], symbol_map: dict[str, str] | None = None) -> dict:
    """Downloads one Shoonya scrip-master zip and parses it via parse_futures_csv()."""
    print(f"Downloading Shoonya symbol master from {url}...")
    resp = urlopen(url, timeout=30)
    zip_data = ZipFile(io.BytesIO(resp.read()))

    target = zip_data.namelist()[0] if len(zip_data.namelist()) == 1 else None
    if target is None:
        candidates = [n for n in zip_data.namelist() if n.endswith("_symbols.txt")]
        target = candidates[0] if candidates else None
    if target is None or target not in zip_data.namelist():
        print(f"Available files: {zip_data.namelist()}")
        raise FileNotFoundError(f"No symbols .txt found in master ZIP from {url}")

    content = zip_data.read(target).decode("utf-8", errors="replace")
    return parse_futures_csv(content, tracked=tracked, symbol_map=symbol_map)


def download_future_master() -> dict:
    """Downloads both Shoonya scrip masters and merges their futures
    contracts into a single { underlying: {...} } map."""
    nfo_contracts = _download_and_parse(NFO_MASTER_URL, NFO_UNDERLYINGS)
    bfo_contracts = _download_and_parse(BFO_MASTER_URL, BFO_UNDERLYINGS, symbol_map=BFO_SYMBOL_TO_UNDERLYING)
    return {**nfo_contracts, **bfo_contracts}


def main():
    contracts = download_future_master()

    for underlying in TRACKED_UNDERLYINGS:
        expiries = contracts.get(underlying, {}).get("expiries", [])
        print(f"  {underlying}: {len(expiries)} expiries")

    OUTPUT_FILE.write_text(
        json.dumps(contracts, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    print(f"Written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
