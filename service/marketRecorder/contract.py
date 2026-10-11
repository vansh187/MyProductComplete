"""
The recorded-data contract (spec sections 3-6): Arrow schemas per stream,
required file metadata, and validate_file() - shared with the Strategic
Engine, which reads these files for backtests. Change a schema only as the
spec's versioning rule allows (new nullable column = minor bump).
"""

import pyarrow as pa
import pyarrow.parquet as pq

# 1.1: every Shoonya field is kept (new nullable columns + `extra`), and the
# daily `candles` stream (broker 1-minute candles) was added.
SCHEMA_VERSION = "1.1"

# Market-wide fields Shoonya sends on both feeds, appended after the 1.0
# columns (a minor version bump only ever appends nullable columns).
_COMMON_EXTRA_COLUMNS = [
    ("pct_change", pa.float64()), ("prev_oi", pa.int64()), ("total_oi", pa.int64()),
    ("last_trade_time", pa.string()), ("upper_circuit", pa.float64()), ("lower_circuit", pa.float64()),
    ("high_52w", pa.float64()), ("low_52w", pa.float64()),
    ("total_buy_qty", pa.int64()), ("total_sell_qty", pa.int64()),
]

TICKS = pa.schema([
    ("ts_recv_ns", pa.int64(), False), ("ts_exch_ns", pa.int64()),
    ("exchange", pa.string(), False), ("token", pa.string(), False), ("tsym", pa.string(), False),
    ("ltp", pa.float64()), ("ltq", pa.int64()), ("volume", pa.int64()), ("oi", pa.int64()),
    ("atp", pa.float64()),
    ("bid", pa.float64()), ("bid_qty", pa.int64()), ("ask", pa.float64()), ("ask_qty", pa.int64()),
    ("day_open", pa.float64()), ("day_high", pa.float64()), ("day_low", pa.float64()), ("prev_close", pa.float64()),
    *_COMMON_EXTRA_COLUMNS,
    ("bid_orders", pa.int64()), ("ask_orders", pa.int64()),
    # Every other field of THIS frame, exactly as Shoonya sent it (JSON
    # object, not forward-filled), so nothing the broker sends is ever lost.
    ("extra", pa.string()),
])

_DEPTH_LEVELS = range(1, 6)
DEPTH = pa.schema(
    [("ts_recv_ns", pa.int64(), False), ("ts_exch_ns", pa.int64()),
     ("exchange", pa.string(), False), ("token", pa.string(), False), ("tsym", pa.string(), False)]
    + [(f"bid_px_{i}", pa.float64()) for i in _DEPTH_LEVELS]
    + [(f"bid_qty_{i}", pa.int64()) for i in _DEPTH_LEVELS]
    + [(f"bid_orders_{i}", pa.int64()) for i in _DEPTH_LEVELS]
    + [(f"ask_px_{i}", pa.float64()) for i in _DEPTH_LEVELS]
    + [(f"ask_qty_{i}", pa.int64()) for i in _DEPTH_LEVELS]
    + [(f"ask_orders_{i}", pa.int64()) for i in _DEPTH_LEVELS]
    + [("ltp", pa.float64()), ("volume", pa.int64())]
    + [("ltq", pa.int64()), ("atp", pa.float64()), ("oi", pa.int64()),
       ("day_open", pa.float64()), ("day_high", pa.float64()), ("day_low", pa.float64()), ("prev_close", pa.float64())]
    + _COMMON_EXTRA_COLUMNS
    + [("extra", pa.string())]
)

# Shoonya's official 1-minute candles (TPSeries) for every instrument recorded
# that day, fetched after the close: a minute-level safety net for anything
# the tick stream missed (e.g. while the WebSocket was down).
CANDLES = pa.schema([
    ("minute_ns", pa.int64(), False),           # candle start, ns since epoch UTC
    ("exchange", pa.string(), False), ("token", pa.string(), False), ("tsym", pa.string(), False),
    ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()), ("close", pa.float64()),
    ("vwap", pa.float64()),
    ("volume", pa.int64()),                     # traded in this minute (intv)
    ("cum_volume", pa.int64()),                 # day volume at the minute's end (v)
    ("oi", pa.int64()), ("oi_change", pa.int64()),
    ("source", pa.string(), False),
])

CHAIN = pa.schema([
    ("snapshot_ns", pa.int64(), False), ("ts_recv_ns", pa.int64(), False),
    ("underlying", pa.string(), False), ("spot", pa.float64()), ("future", pa.float64()),
    ("exchange", pa.string(), False), ("token", pa.string(), False), ("tsym", pa.string(), False),
    ("expiry", pa.date32(), False), ("strike", pa.float64(), False), ("opt_type", pa.string(), False),
    ("ltp", pa.float64()), ("bid", pa.float64()), ("ask", pa.float64()),
    ("bid_qty", pa.int64()), ("ask_qty", pa.int64()), ("volume", pa.int64()), ("oi", pa.int64()),
    ("iv", pa.float64()), ("delta", pa.float64()), ("gamma", pa.float64()), ("theta", pa.float64()), ("vega", pa.float64()),
    ("greeks_source", pa.string()),
])

EVENTS = pa.schema([
    ("ts_recv_ns", pa.int64(), False), ("event_type", pa.string(), False),
    ("stream", pa.string()), ("token", pa.string()),
    ("from_ns", pa.int64()), ("to_ns", pa.int64()), ("detail", pa.string()),
])

INSTRUMENTS = pa.schema([
    ("exchange", pa.string(), False), ("token", pa.string(), False), ("tsym", pa.string(), False),
    ("underlying", pa.string()), ("instrument_type", pa.string(), False),
    ("expiry", pa.date32()), ("strike", pa.float64()), ("opt_type", pa.string()),
    ("lot_size", pa.int32()), ("tick_size", pa.float64()), ("freeze_qty", pa.int64()),
])

# Windowed streams (one part-HHMM.parquet per window) and their sort key.
WINDOWED_SCHEMAS = {"ticks": TICKS, "depth": DEPTH, "chain": CHAIN, "events": EVENTS}
SORT_KEY = {"ticks": "ts_recv_ns", "depth": "ts_recv_ns", "chain": "snapshot_ns", "events": "ts_recv_ns",
            "candles": "minute_ns"}
# Daily (unwindowed) files: instruments/snapshot.parquet, candles/candles_1m.parquet.
DAILY_SCHEMAS = {"instruments": INSTRUMENTS, "candles": CANDLES}
ALL_SCHEMAS = {**WINDOWED_SCHEMAS, **DAILY_SCHEMAS}

EVENT_TYPES = frozenset({
    "RECORDER_START", "RECORDER_STOP", "WS_DISCONNECT", "WS_RECONNECT", "GAP",
    "SUBSCRIBE", "UNSUBSCRIBE", "QUEUE_OVERFLOW", "CLOCK_WARNING", "ERROR",
})

REQUIRED_METADATA = (
    "schema_version", "stream", "trading_date", "window_start_ns", "window_end_ns",
    "window_minutes", "row_count", "recorder_version", "vm_hostname",
)
# Daily files (instruments snapshot, candles) have no window.
REQUIRED_DAILY_METADATA = ("schema_version", "stream", "trading_date", "row_count", "recorder_version", "vm_hostname")
REQUIRED_INSTRUMENTS_METADATA = REQUIRED_DAILY_METADATA


def validate_file(path) -> list[str]:
    """Returns the contract violations in one recorded Parquet file (empty
    list = valid): schema, required metadata, row count, rows sorted and
    inside the file's window, no nulls in non-nullable columns."""
    try:
        return _validate(path)
    except Exception as exc:
        return [f"validation failed: {exc!r}"]


def _validate(path) -> list[str]:
    errors: list[str] = []
    try:
        table = pq.read_table(path)
    except Exception as exc:
        return [f"unreadable: {exc}"]

    meta = {k.decode(): v.decode() for k, v in (table.schema.metadata or {}).items()}
    stream = meta.get("stream")
    expected = ALL_SCHEMAS.get(stream)
    if expected is None:
        return [f"unknown or missing stream metadata: {stream!r}"]

    required = REQUIRED_DAILY_METADATA if stream in DAILY_SCHEMAS else REQUIRED_METADATA
    errors += [f"missing metadata key {key}" for key in required if key not in meta]

    actual = [(f.name, f.type) for f in table.schema]
    wanted = [(f.name, f.type) for f in expected]
    if actual != wanted:
        errors.append(f"schema mismatch: got {actual}, expected {wanted}")
        return errors

    if meta.get("row_count") != str(table.num_rows):
        errors.append(f"row_count metadata {meta.get('row_count')} != actual {table.num_rows}")

    for field in expected:
        if not field.nullable and table.column(field.name).null_count:
            errors.append(f"column {field.name} has {table.column(field.name).null_count} nulls")

    sort_key = SORT_KEY.get(stream)
    if sort_key and table.num_rows:
        values = table.column(sort_key).to_pylist()
        if any(b < a for a, b in zip(values, values[1:])):
            errors.append(f"rows not sorted by {sort_key}")
        try:
            start, end = int(meta["window_start_ns"]), int(meta["window_end_ns"])
            outside = sum(1 for v in values if not start <= v < end)
            if outside:
                errors.append(f"{outside} rows outside the window [{start}, {end})")
        except (KeyError, ValueError):
            pass

    if stream == "events" and table.num_rows:
        unknown = set(table.column("event_type").to_pylist()) - EVENT_TYPES
        if unknown:
            errors.append(f"unknown event types: {sorted(unknown)}")
    return errors
