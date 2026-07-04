"""
One-time script: downloads Shoonya's NFO symbol master and rebuilds
appconfig/master_options.json with the NIFTY/BANKNIFTY/FINNIFTY option chain
(strike -> CE/PE token/tradingsymbol/lot-size) for every available expiry.

Run once (or periodically, e.g. daily, since Shoonya adds new expiries and
occasionally reshuffles tokens):
    python scripts/build_option_master.py

Output: appconfig/master_options.json
{
  "NIFTY": {
    "expiries": ["2026-07-07", "2026-07-14", ...],
    "2026-07-07": {
      "24300": {"ce_token": "...", "ce_tsym": "...", "pe_token": "...", "pe_tsym": "...", "lot_size": 65}
    }
  },
  "BANKNIFTY": {...},
  "FINNIFTY": {...}
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
OUTPUT_FILE = Path(__file__).parent.parent / "appconfig" / "master_options.json"

TRACKED_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY"}


def _parse_expiry(raw: str) -> str:
    """Shoonya's Expiry column is 'DD-MMM-YYYY' (e.g. '29-SEP-2026') -> ISO 'YYYY-MM-DD'."""
    return datetime.strptime(raw.strip(), "%d-%b-%Y").strftime("%Y-%m-%d")


def parse_master_csv(content: str) -> dict:
    """
    Parses Shoonya's NFO_symbols.txt CSV content (Exchange,Token,LotSize,Symbol,
    TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize).
    Returns { underlying: { expiries: [...], "<iso-expiry>": { strike: {...} } } }
    for NIFTY/BANKNIFTY/FINNIFTY index options (Instrument == "OPTIDX") only.
    Pure function - no I/O - so it can be unit-tested against a fixture.
    """
    reader = csv.DictReader(io.StringIO(content))

    chains: dict[str, dict] = {u: {} for u in TRACKED_UNDERLYINGS}

    for row in reader:
        symbol = (row.get("Symbol") or "").strip()
        instrument = (row.get("Instrument") or "").strip()
        if symbol not in TRACKED_UNDERLYINGS or instrument != "OPTIDX":
            continue

        option_type = (row.get("OptionType") or "").strip().upper()
        if option_type not in ("CE", "PE"):
            continue

        try:
            expiry_iso = _parse_expiry(row["Expiry"])
            strike = str(int(float(row["StrikePrice"])))
            lot_size = int(row["LotSize"])
        except (KeyError, ValueError):
            continue

        expiry_chain = chains[symbol].setdefault(expiry_iso, {})
        strike_entry = expiry_chain.setdefault(strike, {"lot_size": lot_size})

        if option_type == "CE":
            strike_entry["ce_token"] = row["Token"].strip()
            strike_entry["ce_tsym"] = row["TradingSymbol"].strip()
        else:
            strike_entry["pe_token"] = row["Token"].strip()
            strike_entry["pe_tsym"] = row["TradingSymbol"].strip()

    result: dict[str, dict] = {}
    for underlying, expiry_map in chains.items():
        expiries_sorted = sorted(expiry_map.keys())
        result[underlying] = {"expiries": expiries_sorted, **expiry_map}

    return result


def download_option_master() -> dict:
    """Downloads Shoonya's NFO_symbols.txt.zip and parses it via parse_master_csv()."""
    print("Downloading Shoonya NFO symbol master...")
    resp = urlopen(NFO_MASTER_URL, timeout=30)
    zip_data = ZipFile(io.BytesIO(resp.read()))

    target = "NFO_symbols.txt"
    if target not in zip_data.namelist():
        print(f"Available files: {zip_data.namelist()}")
        raise FileNotFoundError(f"{target} not found in NFO master ZIP")

    content = zip_data.read(target).decode("utf-8", errors="replace")
    return parse_master_csv(content)


def main():
    chains = download_option_master()

    for underlying in TRACKED_UNDERLYINGS:
        expiries = chains.get(underlying, {}).get("expiries", [])
        total_strikes = sum(len(chains[underlying][e]) for e in expiries)
        print(f"  {underlying}: {len(expiries)} expiries, {total_strikes} strike entries")

    OUTPUT_FILE.write_text(
        json.dumps(chains, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    print(f"Written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
