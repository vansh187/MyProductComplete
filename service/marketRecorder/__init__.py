"""
Market data recorder: records the live Shoonya feed (ticks, 5-level depth,
option-chain snapshots, events, daily instruments) to hourly Parquet files
under data/market/DD-MM-YYYY/, and uploads each finished day to Google Drive
after the close. Spec: docs/DATA_RECORDER_SPEC.md.
"""
