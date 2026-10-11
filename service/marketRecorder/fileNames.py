"""
The single source of every file and folder name the recorder, the uploader,
the Drive audit and the scripts share. Import from here; never repeat a name.
"""

JOURNAL_DIR = ".journal"                  # data/market/.journal/DD-MM-YYYY/<stream>/*.arrows
MANIFEST_NAME = "_manifest.json"          # DD-MM-YYYY/_manifest.json, uploaded last
PRUNED_NAME = "_pruned.json"              # in Drive only: streams removed by the budget
CANDLES_DIR = "candles"
CANDLES_FILE = "candles_1m.parquet"       # DD-MM-YYYY/candles/candles_1m.parquet
CANDLES_MARKER_NAME = ".candles_done.json"       # candle download finished (status inside)
CANDLES_ATTEMPTS_NAME = ".candles_attempts.json"  # failed attempts so far (retry cap)
INSTRUMENTS_DIR = "instruments"
INSTRUMENTS_FILE = "snapshot.parquet"
LEDGER_NAME = ".upload_ledger.jsonl"      # data/market/.upload_ledger.jsonl
FOLDER_CACHE_NAME = ".drive_folders.json"
HOLIDAYS_CACHE_NAME = ".nse_holidays.json"
