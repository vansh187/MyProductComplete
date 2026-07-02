"""
One-time script: downloads the Breeze/ICICI Direct security master CSV and
rebuilds appconfig/master_stocks.json with correct Breeze stock codes.

Run once (or whenever Breeze updates their security master):
    python scripts/build_stock_master.py

Output: appconfig/master_stocks.json  { "Company Name": "BREEZE_CODE" }
"""

import csv
import json
import io
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

SECURITY_MASTER_URL = "https://directlink.icicidirect.com/MotherAppMaster/SecurityMaster.zip"
OUTPUT_FILE = Path(__file__).parent.parent / "appconfig" / "master_stocks.json"


def download_security_master() -> dict[str, str]:
    """
    Downloads and parses Breeze's SecurityMaster.zip.
    Returns { "Company Name": "BREEZE_STOCK_CODE" } for all NSE cash equities.
    """
    print("Downloading Breeze Security Master...")
    resp = urlopen(SECURITY_MASTER_URL, timeout=30)
    zip_data = ZipFile(io.BytesIO(resp.read()))

    # The ZIP contains multiple .txt files — we want NSEScripMaster.txt
    target = "NSEScripMaster.txt"
    if target not in zip_data.namelist():
        print(f"Available files: {zip_data.namelist()}")
        raise FileNotFoundError(f"{target} not found in security master ZIP")

    content = zip_data.read(target).decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(content))

    stocks: dict[str, str] = {}
    for row in reader:
        if len(row) < 4:
            continue
        # Columns: Token, StockCode, ?, CompanyName, ...
        stock_code   = row[1].strip().strip('"')
        company_name = row[3].strip().strip('"')

        if not stock_code or not company_name:
            continue
        # Skip header rows and non-equity instruments (futures/options have "-" in code)
        if stock_code in ("ShortName", "stock_code") or "-" in stock_code:
            continue

        stocks[company_name] = stock_code

    return stocks


def main():
    stocks = download_security_master()
    print(f"Found {len(stocks)} NSE equity entries.")

    OUTPUT_FILE.write_text(
        json.dumps(stocks, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    print(f"Written to {OUTPUT_FILE}")

    # Quick sanity check
    for name in ("Infosys", "Tata Consultancy", "Reliance"):
        matches = {k: v for k, v in stocks.items() if name.lower() in k.lower()}
        print(f"  {name}: {matches}")


if __name__ == "__main__":
    main()
