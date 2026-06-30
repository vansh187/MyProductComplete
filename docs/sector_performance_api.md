# Sector Performance API — Complete Reference

## Overview

The Sector Performance API fetches live NSE sectoral index data from Shoonya and streams
it to the frontend in real-time. It covers 8 sectors: IT, Banking, Energy, FMCG, Pharma,
Auto, Realty, and Metal.

---

## Architecture Flow

```
Frontend (EventSource / fetch)
        │
        ▼
  FastAPI Router  ──►  api/sectorPerformance.py
        │
        ▼
  SectorPerformanceFetcher  ──►  service/sectorPerformance/SectorPerformanceService.py
        │
        ├──► SectorIndexRegistry  ──►  appconfig/sector_indices.json
        │         (loads token config once at startup)
        │
        ▼
  shoonya.get_index_quote(exchange, token)
        │          (called for all 8 sectors in parallel via asyncio.gather)
        ▼
  ShoonyaConnection  ──►  marketengine/ShoonyaConnection.py
        │          (calls Shoonya REST API: GET /NorenWClientAPI/GetQuotes)
        ▼
  Raw Shoonya Response
        │          (fields: lp=ltp, c=prev_close, o=open, h=high, l=low, pc=change%)
        ▼
  Normalised quote dict  ──►  { ltp, open, high, low, prev_close, change, change_pct, as_of }
        │
        ▼
  JSON Response  ──►  { sector, change_pct, change, ltp }
```

---

## Config: appconfig/sector_indices.json

This file maps each sector display name to its Shoonya NSE token.
The registry loads this file **once at module import time** — no disk I/O per request.

```json
[
  { "sector": "IT",      "exchange": "NSE", "token": "26042" },
  { "sector": "Banking", "exchange": "NSE", "token": "26009" },
  { "sector": "Energy",  "exchange": "NSE", "token": "26033" },
  { "sector": "FMCG",    "exchange": "NSE", "token": "26035" },
  { "sector": "Pharma",  "exchange": "NSE", "token": "26013" },
  { "sector": "Auto",    "exchange": "NSE", "token": "26008" },
  { "sector": "Realty",  "exchange": "NSE", "token": "26054" },
  { "sector": "Metal",   "exchange": "NSE", "token": "26012" }
]
```

| Sector  | NSE Index Name       | Shoonya Token |
|---------|----------------------|---------------|
| IT      | Nifty IT (CNXIT)     | 26042         |
| Banking | Nifty Bank (CNXBAN)  | 26009         |
| Energy  | Nifty Energy         | 26033         |
| FMCG    | Nifty FMCG (CNXFMCG) | 26035        |
| Pharma  | Nifty Pharma         | 26013         |
| Auto    | Nifty Auto (CNXAUTO) | 26008         |
| Realty  | Nifty Realty         | 26054         |
| Metal   | Nifty Metal (CNXMET) | 26012         |

To add a new sector, add one line to this JSON — no code changes needed.

---

## Service Classes

### SectorIndexRegistry

Responsibility: Load and expose the sector config.

```python
registry = SectorIndexRegistry("appconfig/sector_indices.json")
registry.sectors()
# Returns the full list of sector dicts from the JSON file
```

### SectorPerformanceFetcher

Responsibility: Fetch live quotes for all sectors in parallel.

```python
fetcher = SectorPerformanceFetcher(registry)
sectors, errors = await fetcher.fetch_all(shoonya)
```

**Key logic — parallel fetching with asyncio.gather:**

All 8 `shoonya.get_index_quote()` calls are launched at the same time.
Total response time = slowest single sector call, not the sum of all 8.

```
Sequential (old way):  IT → Banking → Energy → ... → Metal  ≈ 800ms total
Parallel  (our way):   all 8 simultaneously                  ≈ 100ms total
```

**Lambda capture pattern (why `ex=` and `tk=` are used):**

```python
lambda ex=sector["exchange"], tk=sector["token"]: shoonya.get_index_quote(ex, tk)
```

Without `ex=` and `tk=`, all lambdas inside asyncio.gather would reference the
same `sector` variable (Python late binding), causing all calls to use the last
sector in the loop. The default-argument trick captures the value immediately.

---

## Endpoints

---

### 1. GET /api/market/sectors

One-shot snapshot of all 8 sectors.

**Request:**
```
GET /api/market/sectors
```
No query params, no body, no authentication header required.

---

#### Response — Happy Path (all 8 sectors available, market open)

All 8 Shoonya calls succeed and return valid prices.

**Logic:**
- `market_status` = "open" because current IST time is between 09:15 and 15:30 on a weekday
- Each sector entry has 3 calculated fields:
  - `change_pct` = `(ltp - prev_close) / prev_close * 100`, rounded to 2 decimal places
  - `change`     = `ltp - prev_close`, rounded to 2 decimal places
  - `ltp`        = last traded price of the index (raw from Shoonya field `lp`)
- Positive `change_pct` → sector is up today
- Negative `change_pct` → sector is down today
- `errors` is an empty list

```json
{
  "market_status": "open",
  "sectors": [
    { "sector": "IT",      "change_pct":  1.42, "change":  108.50, "ltp": 37821.30 },
    { "sector": "Banking", "change_pct": -0.21, "change":  -10.85, "ltp": 51234.70 },
    { "sector": "Energy",  "change_pct":  0.87, "change":   18.20, "ltp": 21045.60 },
    { "sector": "FMCG",    "change_pct":  0.34, "change":    6.90, "ltp": 20312.45 },
    { "sector": "Pharma",  "change_pct":  0.92, "change":   14.75, "ltp": 16187.80 },
    { "sector": "Auto",    "change_pct": -0.56, "change":  -28.30, "ltp": 50321.15 },
    { "sector": "Realty",  "change_pct":  1.18, "change":    9.45, "ltp":  8102.35 },
    { "sector": "Metal",   "change_pct": -1.03, "change":  -85.60, "ltp":  8219.40 }
  ],
  "errors": [],
  "last_updated": "2026-06-30T09:45:00.123456+00:00"
}
```

---

#### Response — Market Closed (after 15:30 or weekend)

Logic is identical — Shoonya still returns the last closing price when market is closed.
The only difference is `market_status` = "closed".
The frontend uses this to stop showing a live indicator or blinking dot.

**Logic:**
- `market_status` = "closed" because IST time is past 15:30 or it is Saturday/Sunday
- `change_pct` reflects how much the sector moved during the last trading session
- `ltp` is the closing price of the last session
- The SSE stream interval switches to 60 seconds (prices won't change but connection stays alive)

```json
{
  "market_status": "closed",
  "sectors": [
    { "sector": "IT",      "change_pct":  1.42, "change":  108.50, "ltp": 37821.30 },
    { "sector": "Banking", "change_pct": -0.21, "change":  -10.85, "ltp": 51234.70 },
    { "sector": "Energy",  "change_pct":  0.87, "change":   18.20, "ltp": 21045.60 },
    { "sector": "FMCG",    "change_pct":  0.34, "change":    6.90, "ltp": 20312.45 },
    { "sector": "Pharma",  "change_pct":  0.92, "change":   14.75, "ltp": 16187.80 },
    { "sector": "Auto",    "change_pct": -0.56, "change":  -28.30, "ltp": 50321.15 },
    { "sector": "Realty",  "change_pct":  1.18, "change":    9.45, "ltp":  8102.35 },
    { "sector": "Metal",   "change_pct": -1.03, "change":  -85.60, "ltp":  8219.40 }
  ],
  "errors": [],
  "last_updated": "2026-06-30T15:35:00.000000+00:00"
}
```

---

#### Response — Partial Data (some sectors fail)

Some Shoonya calls return `None` or throw an exception.
Successful sectors appear in `sectors[]`.
Failed sectors appear in `errors[]` with a reason.
The response still returns HTTP 200 — partial data is better than a 500.

**Logic:**
- `asyncio.gather` runs all 8 in parallel; individual failures do not cancel the others
- Each failed sector is captured with its name and reason
- Possible `reason` values: `"no_data"` (Shoonya returned None), or an exception message string

```json
{
  "market_status": "open",
  "sectors": [
    { "sector": "IT",     "change_pct": 1.42, "change": 108.50, "ltp": 37821.30 },
    { "sector": "Energy", "change_pct": 0.87, "change":  18.20, "ltp": 21045.60 },
    { "sector": "FMCG",   "change_pct": 0.34, "change":   6.90, "ltp": 20312.45 },
    { "sector": "Pharma", "change_pct": 0.92, "change":  14.75, "ltp": 16187.80 },
    { "sector": "Realty", "change_pct": 1.18, "change":   9.45, "ltp":  8102.35 }
  ],
  "errors": [
    { "sector": "Banking", "reason": "no_data" },
    { "sector": "Auto",    "reason": "no_data" },
    { "sector": "Metal",   "reason": "Connection timeout" }
  ],
  "last_updated": "2026-06-30T10:12:00.000000+00:00"
}
```

---

#### Response — All Sectors Fail

Every Shoonya call returns `None`. `sectors` is an empty list, `errors` has all 8.
HTTP status is still 200 — the structure is valid, just empty.

**Logic:**
- Shoonya session may have expired mid-day (token rotates daily at 8:30 AM IST)
- Or market is in a pre-open state before 09:15 when some indices return no LTP

```json
{
  "market_status": "open",
  "sectors": [],
  "errors": [
    { "sector": "IT",      "reason": "no_data" },
    { "sector": "Banking", "reason": "no_data" },
    { "sector": "Energy",  "reason": "no_data" },
    { "sector": "FMCG",    "reason": "no_data" },
    { "sector": "Pharma",  "reason": "no_data" },
    { "sector": "Auto",    "reason": "no_data" },
    { "sector": "Realty",  "reason": "no_data" },
    { "sector": "Metal",   "reason": "no_data" }
  ],
  "last_updated": "2026-06-30T09:10:00.000000+00:00"
}
```

---

#### Response — Shoonya Not Connected (HTTP 503)

`app.state.shoonya` is None or `shoonya.is_connected` is False.
This happens if the server started without a valid Shoonya token and auto_login also failed.

**Logic:**
- `_get_shoonya()` checks `shoonya is None or not shoonya.is_connected`
- Raises `HTTPException(503)` before even reaching the fetcher
- Frontend should show "Market data unavailable" and retry after a delay

```json
{
  "detail": "Shoonya market data is not connected."
}
```

HTTP Status: `503 Service Unavailable`

---

### 2. GET /api/market/sectors/stream  (SSE)

Real-time stream. Pushes the same JSON structure as the one-shot endpoint,
repeatedly on a timer, for as long as the client is connected.

**Request:**
```
GET /api/market/sectors/stream
```

**Frontend connection:**
```javascript
const es = new EventSource('/api/market/sectors/stream');

es.onmessage = (event) => {
    const data = JSON.parse(event.data);
    // data.sectors → array of { sector, change_pct, change, ltp }
    // data.market_status → "open" or "closed"
    updateSectorCards(data.sectors);
};

es.onerror = () => {
    // Connection dropped — browser auto-reconnects after ~3 seconds
};
```

---

#### SSE Frame — Market Open (pushed every 10 seconds)

Each SSE message is a single line prefixed with `data: ` and terminated by `\n\n`.

**Logic:**
- Timer fires every 10 seconds while `_is_market_open()` returns True
- Each tick re-calls `_fetcher.fetch_all(shoonya)` — a fresh Shoonya API call every time
- The frontend replaces displayed values with each new push; no manual polling needed

```
data: {"market_status": "open", "sectors": [{"sector": "IT", "change_pct": 1.42, "change": 108.50, "ltp": 37821.30}, {"sector": "Banking", "change_pct": -0.21, "change": -10.85, "ltp": 51234.70}, {"sector": "Energy", "change_pct": 0.87, "change": 18.20, "ltp": 21045.60}, {"sector": "FMCG", "change_pct": 0.34, "change": 6.90, "ltp": 20312.45}, {"sector": "Pharma", "change_pct": 0.92, "change": 14.75, "ltp": 16187.80}, {"sector": "Auto", "change_pct": -0.56, "change": -28.30, "ltp": 50321.15}, {"sector": "Realty", "change_pct": 1.18, "change": 9.45, "ltp": 8102.35}, {"sector": "Metal", "change_pct": -1.03, "change": -85.60, "ltp": 8219.40}], "errors": [], "last_updated": "2026-06-30T09:45:00.123456+00:00"}

```

---

#### SSE Frame — Market Closed (pushed every 60 seconds)

**Logic:**
- When `_is_market_open()` returns False, the sleep interval switches to 60 seconds
- Data values do not change (market is closed), but the connection stays alive
- 60-second heartbeat prevents proxy/load balancer timeout from killing idle SSE connections

```
data: {"market_status": "closed", "sectors": [{"sector": "IT", "change_pct": 1.42, "change": 108.50, "ltp": 37821.30}, {"sector": "Banking", "change_pct": -0.21, "change": -10.85, "ltp": 51234.70}, {"sector": "Energy", "change_pct": 0.87, "change": 18.20, "ltp": 21045.60}, {"sector": "FMCG", "change_pct": 0.34, "change": 6.90, "ltp": 20312.45}, {"sector": "Pharma", "change_pct": 0.92, "change": 14.75, "ltp": 16187.80}, {"sector": "Auto", "change_pct": -0.56, "change": -28.30, "ltp": 50321.15}, {"sector": "Realty", "change_pct": 1.18, "change": 9.45, "ltp": 8102.35}, {"sector": "Metal", "change_pct": -1.03, "change": -85.60, "ltp": 8219.40}], "errors": [], "last_updated": "2026-06-30T15:40:00.000000+00:00"}

```

---

#### SSE — Client Disconnects

**Logic:**
- `await request.is_disconnected()` is checked at the top of every loop iteration
- When the browser tab is closed or `es.close()` is called, the check returns True
- The generator breaks and the server-side coroutine is cleaned up
- No zombie connections accumulate on the server

```
[Server log]
[SSE/sectors] Client disconnected. Loop exiting.
```

---

#### SSE — Shoonya Error Mid-Stream

If a fetch fails during streaming (e.g. a transient Shoonya timeout), the error is
logged and the loop continues. The next push 10 seconds later will try again.
The SSE connection itself is NOT closed on a single error.

```
[Server log]
[SSE/sectors] Error: Connection reset by peer

data: {"market_status": "open", "sectors": [...], "errors": [{"sector": "IT", "reason": "Connection reset by peer"}], "last_updated": "..."}
```

---

## Field Reference

| Field          | Type    | Description                                                      |
|----------------|---------|------------------------------------------------------------------|
| `market_status`| string  | `"open"` (09:15–15:30 IST weekdays) or `"closed"` otherwise     |
| `sectors`      | array   | List of sectors for which Shoonya returned valid data            |
| `sector`       | string  | Display name of the sector (IT, Banking, Energy, etc.)           |
| `change_pct`   | float   | % change from previous close. Positive = green, Negative = red  |
| `change`       | float   | Absolute point change from previous close                        |
| `ltp`          | float   | Last traded price of the NSE sectoral index                      |
| `errors`       | array   | Sectors that failed to fetch; always present, may be empty       |
| `last_updated` | string  | UTC ISO 8601 timestamp of when the response was assembled        |

---

## How change_pct Is Calculated

Shoonya provides:
- `lp`  → last traded price (ltp)
- `c`   → previous day's closing price (prev_close)
- `pc`  → percentage change (pre-calculated by Shoonya, used if available)

```
change     = ltp - prev_close
change_pct = (change / prev_close) × 100   ← used only if Shoonya's `pc` field is missing
```

If Shoonya's own `pc` field is present and valid, that value is used directly
(avoids floating point rounding differences vs Shoonya's own display).

---

## Streaming Interval Summary

| Condition              | Push interval | Reason                                      |
|------------------------|---------------|---------------------------------------------|
| Market open (weekday 09:15–15:30 IST) | 10 seconds | Prices change frequently    |
| Market closed / weekend | 60 seconds   | Prices static; keeps connection alive        |
| Client disconnects     | Stream ends   | `request.is_disconnected()` breaks the loop |

---

## HTTP Response Codes

| Status | When                                                  |
|--------|-------------------------------------------------------|
| 200    | All cases where Shoonya is reachable (even partial data) |
| 503    | Shoonya is not connected at all (`app.state.shoonya` is None or `is_connected` is False) |
