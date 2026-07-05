"""
One-time script: downloads Shoonya's NFO and BFO symbol masters and rebuilds
appconfig/master_options.json with the NIFTY/BANKNIFTY/FINNIFTY (NSE, via NFO)
and SENSEX (BSE, via BFO) option chains (strike -> CE/PE token/tradingsymbol/
lot-size) for every available expiry.

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
OUTPUT_FILE = Path(__file__).parent.parent / "appconfig" / "master_options.json"

# NSE index options are listed under the NFO segment; Sensex (BSE) options
# are a completely separate scrip master (BFO) - Shoonya publishes them as
# two different zip files, so each underlying is tied to the exchange file
# it actually comes from.
NFO_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY"}
BFO_UNDERLYINGS = {"SENSEX"}
TRACKED_UNDERLYINGS = NFO_UNDERLYINGS | BFO_UNDERLYINGS

# Unlike NFO (whose Symbol column is already "NIFTY"/"BANKNIFTY"/"FINNIFTY"
# verbatim), BFO's Symbol column uses its own product codes per index -
# confirmed from a live BFO_symbols.txt.zip: 'BSXOPT' (Sensex), 'BKXOPT'
# (Bankex), 'SX50OPT' (Sensex 50), 'BITOPT' - none of which match our
# internal underlying names, so they must be remapped during parsing.
BFO_SYMBOL_TO_UNDERLYING = {
    "BSXOPT": "SENSEX",
}


def _parse_expiry(raw: str) -> str:
    """Shoonya's Expiry column is 'DD-MMM-YYYY' (e.g. '29-SEP-2026') -> ISO 'YYYY-MM-DD'."""
    return datetime.strptime(raw.strip(), "%d-%b-%Y").strftime("%Y-%m-%d")


def parse_master_csv(content: str, tracked: set[str] = NFO_UNDERLYINGS, symbol_map: dict[str, str] | None = None) -> dict:
    """
    Parses a Shoonya *_symbols.txt CSV content (Exchange,Token,LotSize,Symbol,
    TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize).
    Returns { underlying: { expiries: [...], "<iso-expiry>": { strike: {...} } } }
    for the given tracked index underlyings (Instrument == "OPTIDX") only.

    symbol_map: raw file Symbol -> our internal underlying name, for masters
    (like BFO) whose Symbol column doesn't already match 1:1 (see
    BFO_SYMBOL_TO_UNDERLYING). Rows whose (mapped) symbol isn't in `tracked`
    are skipped.

    Pure function - no I/O - so it can be unit-tested against a fixture.
    """
    reader = csv.DictReader(io.StringIO(content))
    symbol_map = symbol_map or {}

    chains: dict[str, dict] = {u: {} for u in tracked}

    for row in reader:
        raw_symbol = (row.get("Symbol") or "").strip()
        underlying = symbol_map.get(raw_symbol, raw_symbol)
        instrument = (row.get("Instrument") or "").strip()
        if underlying not in tracked or instrument != "OPTIDX":
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

        expiry_chain = chains[underlying].setdefault(expiry_iso, {})
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


def _download_and_parse(url: str, tracked: set[str], symbol_map: dict[str, str] | None = None) -> dict:
    """Downloads one Shoonya scrip-master zip and parses it via parse_master_csv()."""
    print(f"Downloading Shoonya symbol master from {url}...")
    resp = urlopen(url, timeout=30)
    zip_data = ZipFile(io.BytesIO(resp.read()))

    target = zip_data.namelist()[0] if len(zip_data.namelist()) == 1 else None
    if target is None:
        # Fall back to matching the conventional "<SEGMENT>_symbols.txt" name.
        candidates = [n for n in zip_data.namelist() if n.endswith("_symbols.txt")]
        target = candidates[0] if candidates else None
    if target is None or target not in zip_data.namelist():
        print(f"Available files: {zip_data.namelist()}")
        raise FileNotFoundError(f"No symbols .txt found in master ZIP from {url}")

    content = zip_data.read(target).decode("utf-8", errors="replace")
    return parse_master_csv(content, tracked=tracked, symbol_map=symbol_map)


def download_option_master() -> dict:
    """Downloads both Shoonya scrip masters (NFO for NSE indices, BFO for Sensex)
    and merges them into a single { underlying: {...} } chain map."""
    nfo_chains = _download_and_parse(NFO_MASTER_URL, NFO_UNDERLYINGS)
    bfo_chains = _download_and_parse(BFO_MASTER_URL, BFO_UNDERLYINGS, symbol_map=BFO_SYMBOL_TO_UNDERLYING)
    return {**nfo_chains, **bfo_chains}


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
