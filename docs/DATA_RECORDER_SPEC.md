# PrimePip Market Data Recorder — Specification (Steps 0, 1, 2)

| | |
|---|---|
| **Audience** | MyProductComplete backend team (who build the recorder) and the Strategic Engine team (who read the data) |
| **Status** | v1.1 — Step 1 implemented in MyProductComplete (`service/marketRecorder/`), not yet run on a live session |
| **Owner of the data contract** | Strategic Engine team (`PrimePipStrategicEngine`) |
| **Where the recorder runs** | Inside MyProductComplete, on the Mumbai VM, during market hours |
| **Storage** | Hourly files on the VM in the project's `data/market/` folder; each finished day uploaded to the **primepiptrade.com Google account's Drive** (15 GB) after the close |

### Changes from v1.0

| Area | v1.0 | v1.1 |
|---|---|---|
| Folder layout | `<stream>/date=YYYY-MM-DD/part-HHMM.parquet` | **`DD-MM-YYYY/<stream>/part-HHMM.parquet`**: one folder per day, easy to find and to upload as a unit |
| Upload | every hour | **once per day after the close (16:15 IST)**, after the broker candles are in, plus a catch-up on startup for any day missed |
| Recording hours | 09:00–15:30 | **09:00–16:00**: closing and post-close updates are captured too (still 7 hourly files) |
| Fields kept | a subset | **every field Shoonya sends**: all known fields as columns, anything else in an `extra` JSON column; every frame is a row |
| Candles | — | new daily **`candles`** stream: Shoonya's official 1-minute OHLCV + OI + VWAP for every recorded instrument, plus a tick-vs-candle check in the manifest |
| Late frames | dropped and counted | **none**: an hour's file is written only after every frame stamped in that hour |
| Day completeness | inferred from files | **`DD-MM-YYYY/_manifest.json`** uploaded last; the engine syncs only days that have it |
| Drive account | personal Gmail | the **primepiptrade.com** Google account, enforced with `RECORDER_DRIVE_ACCOUNT` |
| `chain` ordering | by `ts_recv_ns` | by **`snapshot_ns`** (`ts_recv_ns` there is each option's last tick time, so it isn't monotonic) |
| Depth source | unspecified | Shoonya's **depth feed** (`d` subscription, `dk`/`df` frames); touchline only carries level 1 |
| Instruments snapshot | ~08:45 | at session start (09:00); `lot_size`/`tick_size` are null for indices; `freeze_qty` null (not in the broker master) |
| Empty days | — | a day with zero `ticks` rows (e.g. an unlisted holiday) is not uploaded |

---

## 1. Why we need this

The Strategic Engine will choose which trading strategy to run each day based on how each strategy performed on recent market data. To do that it needs a **complete, accurate record of every market session**: every price tick, order-book depth, and option-chain state for Nifty and BankNifty.

Shoonya does not provide historical option-chain or order-book data. **The only way to get it is to record it live ourselves.** Any trading day we don't record is lost permanently.

**What the recorder does:** from 09:00 to 16:00 it captures every frame Shoonya sends on MyProductComplete's existing session for the recorded instruments: every field, every update. Each frame is stamped with the VM's receive time in nanoseconds. At the end of every hour it writes one Parquet file per stream into that day's folder in the project (`data/market/DD-MM-YYYY/`). After the close it also downloads Shoonya's official 1-minute candles for every recorded instrument, as a minute-level record of anything the live stream could not deliver (e.g. during a WebSocket outage). At 16:15 the whole day's folder is uploaded to Google Drive, so it's there that evening and the next session starts clean. Files are never merged or modified after they are written. The Strategic Engine downloads the days and replays them for backtests.

```
Shoonya WS (MyProductComplete's existing session)
        │  raw-frame tap (O(1) append, never blocks the product)
        ▼
  Recorder thread ─► journal every 30 s ─► hourly Parquet (atomic) in data/market/DD-MM-YYYY/
                                           after 16:00: broker 1-minute candles ─► candles/
                                                                     │  16:15 IST, whole day
                                                                     ▼
                                              Google Drive (primepiptrade.com): PrimePipData/DD-MM-YYYY/
                                                                     │
                                     Strategic Engine (Step 2): sync ◄┘ ─► validate ─► replay
```

**Timing precision:** `ts_recv_ns` is our receive time with nanosecond resolution. Its real accuracy is limited by the network and the VM clock (sub-millisecond on a chrony-synced VM). Shoonya's own feed time (`ft`) has **1-second** resolution, so the exchange cannot give us anything finer.

**What "every point" means, and its limits:**
- **Recorded:** every frame Shoonya sends for the recorded instruments, with every field, from 09:00 to 16:00, plus the broker's 1-minute candles for each of them. A dropped frame (queue overflow), a WebSocket outage and a restart are each an `events` row, never silent.
- **Not recordable:** Shoonya's touchline is the broker's update stream, not the exchange's full trade-by-trade feed. Several trades inside one update interval reach us as one update (the candle's volume still counts all of them). Ticks sent while our WebSocket is down can't be fetched again: for those minutes the `candles` row is the record.
- **Not subscribed:** options outside ATM ± `RECORDER_STRIKES_EACH_SIDE` (default 10) and expiries beyond the 2 nearest. Raise the setting to record more, within Shoonya's subscription limit (open question 1).

## 2. Steps and ownership

| Step | What | Built by | Done when |
|---|---|---|---|
| **0** | Data contract (§ 3–§ 7) plus a validator | Engine team (validator also in `service/marketRecorder/contract.py`) | Both teams agree to this spec |
| **1** | The recorder inside MyProductComplete (§ 8–§ 12) | **Backend team — implemented** | 3 consecutive full sessions in Drive, 7 files per stream per day, all files valid, no unexplained missing hours |
| **2** | Engine-side reader: download, validate, replay (§ 13) | Engine team | Engine syncs the recorded days and replays them deterministically |

**Nothing is uploaded to Drive until the contract is signed off:** keep `RECORDER_UPLOAD_ENABLED=false` until then. Recording to local disk can start immediately.

---

# STEP 0 — Data Contract

## 3. General rules

| Rule | Value |
|---|---|
| File format | Apache Parquet, written with `pyarrow`, **zstd** compression, row groups of ~100,000 rows |
| Timestamps | `int64`, **nanoseconds since Unix epoch, UTC** |
| Prices | `float64`, in rupees |
| Quantities / volume / OI | `int64` |
| Text | UTF-8 `string` |
| Dates (expiry) | Arrow `date32` |
| Time zone for folder names, file names and windows | IST (Asia/Kolkata) |
| Missing value | Arrow `null`, never `0`, `-1` or `NaN` as a placeholder. A price of 0 from the broker (no trade, empty book level) is recorded as null. |
| Row order within a file | Sorted by `ts_recv_ns` ascending (`chain`: by `snapshot_ns`; `candles`: by `minute_ns`, then exchange, token) |
| Immutability | **Once uploaded, a file is never modified or deleted**, except by the retention policy (§ 12) or a re-upload of the same file after a crash (§ 11) |

Every row carries **two timestamps**:
- `ts_exch_ns`: Shoonya feed time (`ft`, epoch seconds) × 1e9. Null when the frame has no `ft`. Never forward-filled.
- `ts_recv_ns`: our local receive time, `time.time_ns()` on the VM, taken on the broker's WebSocket thread the moment the frame arrives. **The VM clock must be NTP/chrony-synced.** This is the primary ordering key and decides which window a row belongs to.

## 4. Folder layout (same locally and in Drive)

```
data/market/                          ← on the VM, inside the project folder (git-ignored)
PrimePipData/                         ← in Drive, created BY THE RECORDER APP (see § 7)
├── 12-10-2026/                       ← one folder per trading day, DD-MM-YYYY (IST)
│   ├── ticks/
│   │   ├── part-0900.parquet         ← 09:00–10:00 IST
│   │   ├── part-1000.parquet         ← 10:00–11:00
│   │   ├── …
│   │   └── part-1500.parquet         ← 15:00–16:00 (market closes 15:30; post-close updates included)
│   ├── depth/part-HHMM.parquet
│   ├── chain/part-HHMM.parquet
│   ├── events/part-HHMM.parquet
│   ├── instruments/snapshot.parquet  ← written at session start
│   ├── candles/candles_1m.parquet    ← broker 1-minute candles, downloaded after 16:00
│   └── _manifest.json                ← written and uploaded LAST: the day is complete
└── 13-10-2026/ …
```

- `part-HHMM` is the **IST start** of the window.
- Window length is configurable (`RECORDER_WINDOW_MINUTES`, **default 60**), aligned to clock boundaries from the session start.
- The recording session is **09:00–16:00 IST** (`RECORDER_SESSION_START`/`END`), so the hourly default gives **7 files per stream per day**.
- Every window the recorder was running in gets a file, **even an empty one**, so "nothing happened" can be told apart from "not recorded".
- **Exactly one file per stream per window.** No duplicate names in a Drive folder (Drive allows them, so the uploader prevents them; see § 11).
- `DD-MM-YYYY` doesn't sort chronologically as text. Readers must parse the folder name (or use `trading_date` in the file metadata or manifest) to order days.

**`_manifest.json`** (one per day):
```json
{
  "trading_date": "2026-10-12", "folder": "12-10-2026", "recorder_version": "mpc-recorder 1.0.0",
  "rows_by_stream": {"ticks": 4512330, "depth": 380114, "chain": 812400, "events": 412, "instruments": 1842, "candles": 73500},
  "files": [{"stream": "ticks", "name": "part-0900.parquet", "md5": "…", "size": 21345678, "rows": 702113,
             "window_start_ns": "…", "window_end_ns": "…"}, …],
  "candles": {"status": "complete", "instruments": 196, "failed": [], "candles": 73500,
              "check": {"checked_minutes": 41210, "matched": 41188, "missing_in_known_gaps": 22,
                        "missing_unexplained": 0, "unexplained_by_instrument": {}}}
}
```

`candles.check` compares every traded candle minute (every minute for indices) while an instrument was subscribed against the recorded ticks: `matched` = at least one tick in that minute; `missing_in_known_gaps` = no tick, but the minute overlaps a `GAP` event (expected; the candle is the record); `missing_unexplained` should be **0**, and anything else is investigated.

## 5. Streams and schemas

### 5.1 `ticks`: every touchline update for every recorded instrument

| Column | Type | Null? | Shoonya field |
|---|---|---|---|
| `ts_recv_ns` | int64 | no | — |
| `ts_exch_ns` | int64 | yes | `ft` |
| `exchange` | string | no | `e` (`NSE` index, `NFO` F&O) |
| `token` | string | no | `tk` |
| `tsym` | string | no | `ts` (first frame only; otherwise from the scrip master) |
| `ltp` | float64 | yes | `lp` |
| `ltq` | int64 | yes | `ltq` |
| `volume` | int64 | yes | `v` |
| `oi` | int64 | yes | `oi` |
| `atp` | float64 | yes | `ap` |
| `bid`, `bid_qty`, `ask`, `ask_qty` | float64 / int64 | yes | `bp1`, `bq1`, `sp1`, `sq1` (always null for indices) |
| `day_open`, `day_high`, `day_low`, `prev_close` | float64 | yes | `o`, `h`, `l`, `c` |
| `pct_change` | float64 | yes | `pc` (may be 0 or negative) |
| `prev_oi`, `total_oi` | int64 | yes | `poi`, `toi` |
| `last_trade_time` | string | yes | `ltt` |
| `upper_circuit`, `lower_circuit` | float64 | yes | `uc`, `lc` |
| `high_52w`, `low_52w` | float64 | yes | `52h`, `52l` |
| `total_buy_qty`, `total_sell_qty` | int64 | yes | `tbq`, `tsq` |
| `bid_orders`, `ask_orders` | int64 | yes | `bo1`, `so1` (null for indices) |
| `extra` | string | yes | **every other field of this frame, exactly as sent** (JSON object; not forward-filled) |

**Partial updates:** Shoonya sends only the fields that changed. The recorder keeps the **last known value per token** and writes a **complete, forward-filled row for every frame** that carries anything beyond its envelope (`t`, `e`, `tk`). A field never received for that token is `null`. Nothing a frame carries is dropped: a field without its own column is kept in `extra`.

India VIX, Nifty 50 and Nifty Bank are recorded in this stream.

### 5.2 `depth`: 5-level order book (front-month futures)

From Shoonya's **depth feed** (subscription type `d`, frames `dk`/`df`), forward-filled the same way.

| Column | Type | Null? | Shoonya field |
|---|---|---|---|
| `ts_recv_ns`, `ts_exch_ns` | int64 | ts_exch nullable | —, `ft` |
| `exchange`, `token`, `tsym` | string | no | |
| `bid_px_1` … `bid_px_5` | float64 | yes | `bp1`…`bp5` |
| `bid_qty_1` … `bid_qty_5` | int64 | yes | `bq1`…`bq5` |
| `bid_orders_1` … `bid_orders_5` | int64 | yes | `bo1`…`bo5` |
| `ask_px_1` … `ask_px_5` | float64 | yes | `sp1`…`sp5` |
| `ask_qty_1` … `ask_qty_5` | int64 | yes | `sq1`…`sq5` |
| `ask_orders_1` … `ask_orders_5` | int64 | yes | `so1`…`so5` |
| `ltp` | float64 | yes | `lp` |
| `volume` | int64 | yes | `v` |
| `ltq`, `atp`, `oi` | int64 / float64 / int64 | yes | `ltq`, `ap`, `oi` |
| `day_open`, `day_high`, `day_low`, `prev_close` | float64 | yes | `o`, `h`, `l`, `c` |
| `pct_change` … `total_sell_qty` | as in `ticks` | yes | `pc`, `poi`, `toi`, `ltt`, `uc`, `lc`, `52h`, `52l`, `tbq`, `tsq` |
| `extra` | string | yes | every other field of this frame, as sent |

### 5.3 `chain`: option-chain snapshot every 5 seconds

One snapshot = one row per recorded option that has had at least one tick, all sharing the same `snapshot_ns`. Built from the recorder's in-memory latest state (no REST calls). **Sorted and windowed by `snapshot_ns`.**

| Column | Type | Null? | Notes |
|---|---|---|---|
| `snapshot_ns` | int64 | no | snapshot time (local) |
| `ts_recv_ns` | int64 | no | when this option's latest tick was received (shows staleness) |
| `underlying` | string | no | `NIFTY` / `BANKNIFTY` |
| `spot` | float64 | yes | index LTP at snapshot |
| `future` | float64 | yes | front-month future LTP at snapshot |
| `exchange`, `token`, `tsym` | string | no | |
| `expiry` | date32 | no | |
| `strike` | float64 | no | |
| `opt_type` | string | no | `CE` / `PE` |
| `ltp`, `bid`, `ask` | float64 | yes | |
| `bid_qty`, `ask_qty`, `volume`, `oi` | int64 | yes | |
| `iv`, `delta`, `gamma`, `theta`, `vega` | float64 | yes | **null in v1.1**; the engine computes them offline |
| `greeks_source` | string | yes | null in v1.1 |

### 5.4 `events`: everything that explains the data

| Column | Type | Null? | Notes |
|---|---|---|---|
| `ts_recv_ns` | int64 | no | |
| `event_type` | string | no | `RECORDER_START`, `RECORDER_STOP`, `WS_DISCONNECT`, `WS_RECONNECT`, `GAP`, `SUBSCRIBE`, `UNSUBSCRIBE`, `QUEUE_OVERFLOW`, `CLOCK_WARNING`, `ERROR` |
| `stream` | string | yes | affected stream (`ticks`/`depth` for SUBSCRIBE/UNSUBSCRIBE) |
| `token` | string | yes | `EXCH|TOKEN` for SUBSCRIBE/UNSUBSCRIBE |
| `from_ns`, `to_ns` | int64 | yes | gap / overflow start and end |
| `detail` | string | yes | free text or JSON (e.g. tsym, dropped count, error) |

When they are written:
- **GAP** `detail="recorder restart"`: from the last row saved before the restart to the restart time.
- **GAP** `detail="websocket down"`: from the disconnect to the reconnect, written together with `WS_RECONNECT`.
- **QUEUE_OVERFLOW**: frames dropped because the recorder fell behind, with count and time range.
- **CLOCK_WARNING**: the median of `ts_recv_ns − ts_exch_ns` is outside −1.5 s…+3 s (check chrony).
- **RECORDER_STOP** at session end, and the `UNSUBSCRIBE` rows that follow it, are stamped 1 ns before the session end (16:00) so they land in the last window.

The backtester uses these to tell "no ticks because nothing traded" apart from "no ticks because we were disconnected or not subscribed".

### 5.5 `instruments`: daily contract master (`instruments/snapshot.parquet`, at session start)

| Column | Type | Null? |
|---|---|---|
| `exchange`, `token`, `tsym` | string | no |
| `underlying` | string | yes |
| `instrument_type` | string | no — `INDEX` / `FUT` / `OPT` |
| `expiry` | date32 | yes |
| `strike` | float64 | yes |
| `opt_type` | string | yes |
| `lot_size` | int32 | yes (null for `INDEX`) |
| `tick_size` | float64 | yes (null for `INDEX`) |
| `freeze_qty` | int64 | yes (null: not in Shoonya's master) |

Contains the 3 indices, NIFTY/BANKNIFTY futures (2 months) and **every strike** of the recorded option expiries (not only the ±10 window), from that day's scrip master. Lot sizes change over time, so the backtester must use the values of *that* day.

### 5.6 `candles`: broker 1-minute candles (`candles/candles_1m.parquet`, after the close)

Shoonya's official 1-minute candles (TPSeries) for **every instrument recorded that day**: every token with a `SUBSCRIBE` event, read from the day's `events`, so instruments recorded before a restart are included. Downloaded after 16:00 at `RECORDER_CANDLES_CALLS_PER_SEC` (default 3) on a background thread. Every TPSeries field is kept.

| Column | Type | Null? | Shoonya field |
|---|---|---|---|
| `minute_ns` | int64 | no | `time` (candle start, IST → ns since epoch UTC) |
| `exchange`, `token`, `tsym` | string | no | |
| `open`, `high`, `low`, `close` | float64 | yes | `into`, `inth`, `intl`, `intc` |
| `vwap` | float64 | yes | `intvwap` |
| `volume` | int64 | yes | `intv` (traded in this minute) |
| `cum_volume` | int64 | yes | `v` (day volume at the minute's end) |
| `oi`, `oi_change` | int64 | yes | `oi`, `intoi` |
| `source` | string | no | `shoonya_tpseries_1m` |

The download status and the tick-vs-candle check go into `.candles_done.json` (local) and the manifest's `candles` key (§ 4).

**Retries:** one background candle thread does every download. It starts only after the journal recovery (so a crashed day's events are final), skips any day whose journal is still open, and runs at the close and then every `RECORDER_CANDLES_RETRY_SECS` (300). The marker is written only when the download is really finished:
- **complete:** every instrument fetched.
- **no_instruments:** nothing was subscribed that day.
- **partial / failed:** after `RECORDER_CANDLES_MAX_ATTEMPTS` (6) failed attempts, listing what's missing.

A failed instrument is retried alone, and the candles already saved are kept. If every call fails (e.g. an expired broker token), no empty file is written. A broker session that's down doesn't count as an attempt. Days are retried for up to `RECORDER_CANDLES_BACKFILL_DAYS` (5) days, across restarts.

**Late candles:** if a day was uploaded before its candles finished, the candles file and the updated manifest are uploaded on a later run. The manifest is the only file that is ever updated in Drive.

## 6. Parquet file metadata (key-value, required in every file)

| Key | Example |
|---|---|
| `schema_version` | `1.1` |
| `stream` | `ticks` |
| `trading_date` | `2026-10-12` (ISO, even though the folder is `12-10-2026`) |
| `window_start_ns` | `1791784800000000000` |
| `window_end_ns` | `1791788400000000000` |
| `window_minutes` | `60` |
| `row_count` | `612345` |
| `recorder_version` | `mpc-recorder 1.0.0` |
| `vm_hostname` | `pp-mumbai-01` |

The daily files (instruments snapshot, candles) have no window keys.

**Versioning:** a new nullable column is a minor bump (`1.0 → 1.1`), and readers must tolerate it. **1.1** appended the extra `ticks`/`depth` columns and `extra`, and added the `candles` stream. Renaming or removing a column, or changing a type, is a major bump (`2.0`), agreed by both teams beforehand.

## 7. Google Drive access (one-time setup, primepiptrade.com account)

1. Sign in to Google Cloud Console **as the primepiptrade.com account**. Create a project, e.g. **"PrimePip Data"**, and enable the **Google Drive API**.
2. OAuth consent screen:
   - If primepiptrade.com is a **Google Workspace** domain: choose User type **Internal**. Tokens don't expire after 7 days, and there's no unverified-app warning.
   - If it's a plain Google account: choose **External**, add the account as a user, and **publish the app to "In production"**. In "Testing" mode refresh tokens expire after 7 days and uploads would stop. An unverified-app warning at first login is expected.
3. Create **two OAuth clients of type "Desktop app"** in this project: one for the recorder, one for the engine.
4. Scope: **`https://www.googleapis.com/auth/drive.file`**. The apps only see files created by apps in this project.
   - A folder created by hand in the Drive web UI is **invisible** to the recorder. It creates `PrimePipData/` and every subfolder itself.
   - If the engine's client can't see the recorder's files, the engine falls back to `drive.readonly`. The recorder stays on `drive.file`.
5. On a machine with a browser, run:
   `python scripts/recorder_drive_login.py --client-secret client_secret.json --token drive_token.json`
   and **sign in as the primepiptrade.com account**. The script prints which account the token belongs to.
6. Copy `drive_token.json` to the VM at `~/.primepip/drive_token.json` (or set `RECORDER_DRIVE_TOKEN_FILE`), `chmod 600`. Set **`RECORDER_DRIVE_ACCOUNT=<the primepiptrade.com address>`** in the VM's `.env`. A token for any other account is then refused, so data can never go to the wrong Drive.
7. **Never commit** the token or the client secret (`.gitignore` covers `secrets/`, `client_secret*.json`, `drive_token*.json`).

Python packages: `google-api-python-client`, `google-auth`, `pyarrow` (in `requirements.txt`); `google-auth-oauthlib` only on the machine that runs the login.

---

# STEP 1 — Recorder (MyProductComplete) — implemented

Code: `service/marketRecorder/` (recorder, row builder, writer, planner, uploader, contract), wired in `app.py`. Status at `GET /api/internal/latency` (`recorder`, `recorder_upload`). Tests: `tests/test_market_recorder.py`.

## 8. Golden rule: the recorder must never hurt the product

| Requirement | How it's done |
|---|---|
| Never block the WS callback | The tap (`MarketRecorder.tap`, registered with `ShoonyaOptionFeed.on_raw_tick`) only does `time.time_ns()` and a `deque.append` of the raw frame. Outside the session it returns immediately. All parsing and writing happens on the `market-recorder` thread. |
| Never crash the product | Every step is wrapped; errors are logged and written as `ERROR` events. A watchdog task restarts the worker thread if it dies. |
| Bounded memory | Queue capped at `RECORDER_QUEUE_MAX` (200k frames). When full, frames are **dropped and counted**, never blocking, and a `QUEUE_OVERFLOW` event is written. Buffers go to disk every 30 s. |
| No extra broker load | Reuses the product's Shoonya session and WebSocket. **No second login.** Subscriptions go through the feed's ref-counting (`ensure_subscribed`/`release`, and the new `ensure_depth_subscribed`/`release_depth`), so the product's own subscriptions are never affected. Frames for tokens the recorder didn't ask for are ignored. |
| Kill switch | `RECORDER_ENABLED=false` (the default) disables it with no code change |
| Same data as the live screens | The recorder taps the **same WebSocket frames** that drive the app's live prices, option chain and positions, before any processing, so recorded and displayed data come from one source. Live data makes **no extra broker calls**. The only extra calls are the after-close candle download (one TPSeries request per recorded instrument, ~3/s, after 16:00) and the daily holiday list from NSE. |

## 9. What is recorded

| Instruments | Stream(s) | Notes |
|---|---|---|
| Nifty 50 (`NSE|26000`), Nifty Bank (`NSE|26009`), India VIX (`NSE|26017`) | `ticks` | tokens as used by the product today |
| NIFTY and BANKNIFTY futures, current + next month | `ticks` | 4 contracts, from `appconfig/FutureMaster` |
| NIFTY and BANKNIFTY front-month futures | `depth` | 2 contracts, depth feed |
| NIFTY options, **2 nearest expiries**, ATM ± 10 strikes, CE + PE | `ticks`, `chain` | from `appconfig/OptionMaster` |
| BANKNIFTY options, **2 nearest expiries**, ATM ± 10 strikes, CE + PE | `ticks`, `chain` | from `appconfig/OptionMaster` |

About **175 touchline + 2 depth** subscriptions, on top of the product's own. Shoonya's per-session limit is still open question 1. Lower `RECORDER_STRIKES_EACH_SIDE` if a limit is hit.

**Rolling ATM window:** ATM = the strike nearest to the index LTP (front-month future LTP until the index has ticked). Option chains are subscribed once the underlying has a price. Re-centring happens when ATM has moved **≥ 2 strikes** from the current centre. **New strikes are subscribed before old ones are released.** Every change is a `SUBSCRIBE`/`UNSUBSCRIBE` event. Expiries and strikes come from the scrip masters, which are refreshed daily, never hardcoded.

## 10. Writer design

```
WS thread:  tap(): (time.time_ns(), raw frame) → bounded deque
recorder thread:
   frame → forward-fill → row buffer per stream and window
   every 30 s:  buffer → data/market/.journal/DD-MM-YYYY/<stream>/<HHMM>-<last ns>-<seq>.arrows
                (Arrow IPC, written to .tmp, fsync, rename)
   window end + 2 s grace, and no queued frame stamped before the window end:
                all segments of the window → sort → DD-MM-YYYY/<stream>/part-HHMM.parquet.tmp
                → fsync → os.replace(.tmp → .parquet)    ← never a half-written .parquet
                → delete the window's segments
```

- **Crash recovery:** at startup, segments of windows that have already ended are turned into their Parquet files. The window in progress keeps its segments and is merged at window end with the data recorded after the restart, so there is **one file** per window. A crash loses at most the last ≤ 30 s, and a `GAP` event (`recorder restart`) covers the downtime.
- **No late rows:** a window's file is written only once every frame stamped inside it has been processed, so no frame arrives after its file. (`late_rows_dropped` in the status stays 0; it would only count after a backwards clock jump.)
- **Candles:** after the session ends, the broker's 1-minute candles are downloaded for every recorded instrument (§ 5.6).
- **Chain snapshots:** every 5 s from the in-memory latest state.
- **Instruments:** written at session start, once per day.
- **Session control:** 09:00–16:00 IST on trading days (the market closes at 15:30; the last half hour captures closing and post-close updates). Weekends and the dates in `appconfig/nse_holidays.json` are skipped; `special_sessions` there are recorded. Holidays are the union of the hand-maintained base list (`appconfig/nse_holidays.json`, tracked in git, never rewritten by the app) and a list **refreshed automatically from NSE's API** (`https://www.nseindia.com/api/holiday-master?type=trading`, F&O segment) into `data/market/.nse_holidays.json` (git-ignored) at startup and after each close, on a background thread. Only the years NSE returns are replaced; other years' dates are kept. If NSE can't be reached, the saved lists stay in use. At 16:00 the last window is closed and all recorder subscriptions are released. (A Muhurat session, marked `*` by NSE, has evening hours announced separately: add the date to `special_sessions` and set `RECORDER_SESSION_START`/`END` for that day.)

## 11. Uploader (end of day)

| Requirement | Detail |
|---|---|
| When | Daily at `RECORDER_UPLOAD_AT` (default **16:15 IST**), and once at startup to catch up any finished day not yet uploaded (VM down at 16:15, Drive unreachable, …). A day still holding journal segments is never uploaded. **Today** is uploaded only once its session is really closed, whatever `RECORDER_UPLOAD_AT` says: at least 5 minutes past the session end, and its last window's `events` file written (or 2 hours after the end, if the recorder wasn't running then). Today's upload then waits for its candles (`.candles_done.json`), checking every 5 minutes, for at most `RECORDER_UPLOAD_WAIT_FOR_CANDLES_MINS` (45) after `RECORDER_UPLOAD_AT`. Earlier days never wait, and candles that finish later are uploaded afterwards (§ 5.6). |
| Own thread | Runs on a dedicated `recorder-upload` thread; never delays the recorder, the product or broker calls |
| Order | All Parquet files of the day, then `_manifest.json` **last** |
| Resumable upload | `MediaFileUpload(..., resumable=True)` |
| Verify | Drive's `md5Checksum` must equal the local MD5; on a mismatch the file is re-uploaded once, otherwise the day fails and is retried next run |
| No duplicates | The name is looked up in the target folder first. Same md5 → skipped. Different md5 → the existing file's **content is replaced** (no second file) and an error is logged; this should only happen after a crash (or for the manifest, when late candles arrive). A file or folder create that times out is looked up before it is retried, so a create whose response was lost is never repeated. |
| Ledger | `data/market/.upload_ledger.jsonl`, append-only: path, Drive file id, md5, size, time; plus one line per finished day. A restart resumes from it. |
| Retry | Exponential backoff with jitter on 429/5xx/network errors; local files are kept until verified |
| Folder ids | Cached in `data/market/.drive_folders.json` |
| Empty day | A day with zero `ticks` rows is marked `skipped_empty` and not uploaded |
| Manual run | `python scripts/recorder_check_day.py DD-MM-YYYY [--upload]` validates a day and optionally uploads it |
| Drive audit | `python scripts/recorder_audit_drive.py [--root PrimePipData]` checks every day folder in Drive: a valid `DD-MM-YYYY` name, `_manifest.json` present, every manifest file present exactly once with the same md5, no stray or duplicate files, the instruments snapshot (and candles when downloaded) present. Missing hourly windows are listed as notes. Exit code 1 on any problem. |

## 12. Retention, storage, monitoring, acceptance

**Local disk (VM):** a day folder is deleted only when it is **verified in Drive and older than 7 days** (`RECORDER_LOCAL_KEEP_DAYS`). Free disk is in the status; alert below 15%.

**Drive budget (15 GB, shared with Gmail/Photos of the same account):**
- Estimate: 100–200 MB/day for all streams → 2.2–4.4 GB per month (22 trading days) → **15 GB lasts roughly 3–6 months**, less whatever Gmail/Photos already use. Measure the real daily size in week 1 (`rows_by_stream` and sizes are in every manifest and in the upload result).
- Budget **10 GB** by default (`RECORDER_DRIVE_BUDGET_GB`) for the **recorder's own files** under `PrimePipData/`, measured by listing them after each upload. The Google account's total usage (Gmail, Photos, other Drive files) is only reported, with a warning above 95% of the account limit; it never triggers a deletion.
- Over budget: the `ticks` and `depth` folders of the oldest days older than 60 days (`RECORDER_DRIVE_PRUNE_AFTER_DAYS`) are deleted first. `chain`, `events`, `instruments`, candles and the manifest are kept. Every deletion is logged and recorded in that day's **`_pruned.json`** in Drive, so the audit reports those streams as pruned, not missing.
- The engine keeps its own synced copy, so pruned days aren't lost if the engine synced them. If more history is needed, upgrade storage rather than change the format.

**Monitoring:** `GET /api/internal/latency` → `recorder` and `recorder_upload`:

| Metric (field) | Alert when |
|---|---|
| `last_file_closed` per stream | > 70 min ago during market hours |
| `queue_depth`, `dropped_frames` | any drop |
| `recorder_upload.backlog` | any finished day listed |
| `ws_connected`, WS events today | disconnected, or > 5 reconnects |
| `CLOCK_WARNING` events | any |
| `recorder_upload.last_run.drive.recorder_usage_gb` | > 90% of budget |
| `candles_last_result.check.missing_unexplained` | > 0 |
| `candles_last_result.status` | not `complete` |
| `disk.free_pct` | < 15% |
| `thread_alive`, `thread_restarts`, `last_error` | thread dead or restarting |

**Step 1 acceptance criteria:**
1. **1a — local only** (`RECORDER_ENABLED=true`, `RECORDER_UPLOAD_ENABLED=false`): one full live session recorded; `scripts/recorder_check_day.py` reports all files valid.
2. **1b — with Drive** (`RECORDER_UPLOAD_ENABLED=true`): the day appears under `PrimePipData/DD-MM-YYYY/` in the primepiptrade.com Drive with matching md5s, no duplicates, and `_manifest.json`.
3. **1c — 3 consecutive full sessions:** 7 files per stream per day plus the instruments snapshot and candles; every gap explained by an `events` row; `candles.check.missing_unexplained` = 0; daily size reported.
4. **Crash test:** kill the process mid-window and restart. One file for that window, ≤ 30 s lost, a `GAP` event recorded. (Covered by unit tests; to be repeated live.)
5. **Product-impact test:** tick-handling latency (`/api/internal/latency`, `recv_to_send_ms` p95/p99) unchanged with the recorder on vs off.

**Configuration (`.env`):**

| Variable | Default | Meaning |
|---|---|---|
| `RECORDER_ENABLED` | `false` | kill switch |
| `RECORDER_DATA_DIR` | `<project>/data/market` | local folder |
| `RECORDER_WINDOW_MINUTES` | `60` | file window |
| `RECORDER_SESSION_START` / `_END` | `09:00` / `16:00` | IST |
| `RECORDER_STRIKES_EACH_SIDE` | `10` | option window |
| `RECORDER_OPTION_EXPIRIES` | `2` | expiries per underlying |
| `RECORDER_RECENTER_AFTER_STRIKES` | `2` | hysteresis |
| `RECORDER_QUEUE_MAX` | `200000` | frames buffered before dropping |
| `RECORDER_UPLOAD_ENABLED` | `false` | Drive upload on/off |
| `RECORDER_UPLOAD_AT` | `16:15` | IST |
| `RECORDER_UPLOAD_WAIT_FOR_CANDLES_MINS` | `45` | longest wait for today's candles |
| `RECORDER_CANDLES_ENABLED` | `true` | broker 1-minute candles after the close |
| `RECORDER_CANDLES_CALLS_PER_SEC` | `3` | broker call rate for candles |
| `RECORDER_CANDLES_BACKFILL_DAYS` | `5` | how long a missed candle download is retried |
| `RECORDER_CANDLES_RETRY_SECS` | `300` | how often unfinished candle downloads are retried |
| `RECORDER_CANDLES_MAX_ATTEMPTS` | `6` | failed attempts before a day's candles are closed as partial/failed |
| `RECORDER_DRIVE_ACCOUNT` | — | the primepiptrade.com address; other accounts refused |
| `RECORDER_DRIVE_TOKEN_FILE` | `~/.primepip/drive_token.json` | OAuth token |
| `RECORDER_DRIVE_ROOT` | `PrimePipData` | root folder in Drive |
| `RECORDER_DRIVE_BUDGET_GB` | `10` | prune above this |
| `RECORDER_DRIVE_PRUNE_AFTER_DAYS` | `60` | only older days are pruned |
| `RECORDER_LOCAL_KEEP_DAYS` | `7` | local copies after upload |
| `RECORDER_HOLIDAYS_FILE` | `appconfig/nse_holidays.json` | holidays / special sessions |
| `RECORDER_REFRESH_HOLIDAYS` | `true` | refresh holidays from NSE's API |

---

# STEP 2 — Engine-side Reader (Strategic Engine repo)

No work for the backend team here, but everything relies on the contract.

| Component | What it does | Depends on |
|---|---|---|
| `marketdata/contract.py` | Schemas + `validate_file(path)` (same rules as `service/marketRecorder/contract.py`) | § 3, § 5, § 6 |
| `marketdata/drive_reader.py` | Read-only Drive client: list `PrimePipData/DD-MM-YYYY/` folders, download with md5 check, backoff | § 4, § 7 |
| `scripts/sync_data.py --from --to` | **Incremental** download of days that have `_manifest.json`, only files new or changed by md5 (from the manifest), parallel, validates each | manifest, immutability, unique names |
| `marketdata/store.py` | Local index and per-day status. A day is **complete** when its manifest exists and lists every expected window | manifest, `window_minutes` |
| `marketdata/replay_reader.py` | Merges all streams of many days into one time-ordered stream (by `ts_recv_ns`; `ts_exch_ns` is only 1-second resolution), deterministic | sorted rows |
| `scripts/data_report.py` | Inventory: days, files, rows, MB per stream per day, missing windows, gaps | all |

Folder names are `DD-MM-YYYY`: parse them, don't sort them as text.

---

## 13. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Shoonya's WebSocket subscription limit per session: do ~175 touchline + 2 depth fit alongside the product's own (option chain views ±20 strikes, indices, sectors, Nifty 50 watchlist)? Check in the first live session (subscription acks, errors in logs). | Backend |
| 2 | Does Shoonya's touchline carry `ltq`, and does the depth feed fill `bo`/`so` (order counts)? Check in the first recorded day: if a column is always null, say so here. | Backend |
| 3 | Does the engine's OAuth client (same project, `drive.file`) see the recorder's files, or does the engine need `drive.readonly`? | Engine + Backend, in 1b |
| 4 | Real daily size, and whether 15 GB (minus the account's Gmail/Photos) is enough or storage should be upgraded | Backend measures; product owner decides |
| 5 | Is primepiptrade.com a Google Workspace domain (Internal consent screen) or a plain Google account (External, In production)? | Product owner |
| 6 | Greeks stay null (the engine computes them offline). | Decided |

## 14. Sign-off

| Team | Name | Date | Agreed to contract v1.1 |
|---|---|---|---|
| Strategic Engine | | | ☐ |
| MyProductComplete backend | | | ☐ |
