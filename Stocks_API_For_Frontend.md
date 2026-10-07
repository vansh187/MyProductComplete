# Explore Stocks API — Frontend Integration Guide

All endpoints below live under `api/stocks.py` and `api/search.py`. These are
**public, no-auth** market-data reads — same access convention as
`/api/mutual-funds/*`. No `Authorization` header is required.

Every endpoint returns errors in this app's standard shape:

```json
{ "detail": "..." }
```

| Status | Meaning |
|---|---|
| `404` | Symbol/exchange pair not recognized |
| `503` | Live market data temporarily unavailable (broker disconnected, timeout, or module not initialized) |

---

## 1. Explore page — `GET /api/stocks/explore`

Powers the Explore Stocks landing page (trending, gainers/losers, most
active, collection tiles). Backed by a 5s server-side cache, so it's safe to
poll every 5s from the frontend without hammering the broker.

### Response — `200`

```json
{
  "market_status": "OPEN",
  "trending": [
    {
      "symbol": "RELIANCE",
      "exchange": "NSE",
      "name": "Reliance Industries",
      "ltp": 2456.75,
      "change": 12.30,
      "change_pct": 0.50,
      "volume": 4523100,
      "sector": "Energy"
    }
  ],
  "top_gainers": [ { "...": "same shape as trending" } ],
  "top_losers": [ { "...": "same shape as trending" } ],
  "most_active": [ { "...": "same shape as trending" } ],
  "collections": [
    { "key": "nifty50", "title": "Nifty 50", "icon_hint": "trending-up" },
    { "key": "banking", "title": "Banking", "icon_hint": "landmark" }
  ]
}
```

| Field | Notes |
|---|---|
| `market_status` | `PRE_OPEN`, `OPEN`, or `CLOSED` (IST market hours, Mon–Fri) |
| `trending` / `most_active` | Same list today (sorted by volume) — kept as separate fields for the frontend's two different UI slots |
| `top_gainers` / `top_losers` | Only positive/negative movers respectively; can be shorter than 10 items or empty if nothing qualifies |
| `sector` | May be `null` if the symbol has no sector tag |
| `collections.icon_hint` | A hint string (e.g. `"landmark"`, `"cpu"`) — map to whatever icon set the frontend uses, not a literal image URL |

If the broker session is disconnected, list fields (`trending`, etc.) come
back as empty arrays rather than erroring — render as an empty state, not a
failure.

---

## 2. Filter options — `GET /api/stocks/facets`

Populates the exchange/sector filter dropdowns on the search/browse UI.

### Response — `200`

```json
{
  "exchanges": ["BSE", "NSE"],
  "sectors": ["Auto", "Banking", "Energy", "FMCG", "IT", "Pharma"]
}
```

---

## 3. Search stocks — `GET /api/stocks/search`

### Query params

| Param | Type | Required | Notes |
|---|---|---|---|
| `q` | string | no | Symbol/name substring match |
| `exchange` | string | no | e.g. `NSE`, `BSE` |
| `sector` | string | no | Case-insensitive exact match against a facet value |
| `page` | int | no (default `1`) | Must be `>= 1` |
| `page_size` | int | no (default `20`) | `1`–`200` |

Example: `GET /api/stocks/search?q=REL&exchange=NSE&page=1&page_size=20`

### Response — `200`

```json
[
  {
    "symbol": "RELIANCE",
    "exchange": "NSE",
    "name": "Reliance Industries",
    "ltp": 2456.75,
    "change": 12.30,
    "change_pct": 0.50,
    "volume": 4523100,
    "sector": "Energy"
  }
]
```

Returns `[]` (not a 404) when nothing matches.

---

## 4. Combined navbar search — `GET /api/search`

Fans out to stock search **and** mutual-fund search in one call, for the
top-nav search box.

### Query params

| Param | Type | Required | Notes |
|---|---|---|---|
| `q` | string | no | Empty/blank `q` returns an empty result set, not an error |
| `limit` | int | no (default `6`) | `1`–`25`, applies to each side independently |

### Response — `200`

```json
{
  "stocks": [
    {
      "symbol": "RELIANCE",
      "exchange": "NSE",
      "name": "Reliance Industries",
      "ltp": 2456.75,
      "change": 12.30,
      "change_pct": 0.50,
      "volume": 4523100,
      "sector": "Energy"
    }
  ],
  "mutual_funds": [
    {
      "scheme_code": 119551,
      "scheme_name": "Axis Bluechip Fund - Direct Growth",
      "fund_house": "Axis Mutual Fund",
      "latest_nav": 58.42
    }
  ]
}
```

If one side's backing service fails or is unavailable, that side simply
comes back as `[]` — the other side's results are unaffected.

---

## 5. Stock quote — `GET /api/stocks/{exchange}/{symbol}/quote`

Example: `GET /api/stocks/NSE/RELIANCE/quote`

### Response — `200`

```json
{
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "name": "Reliance Industries",
  "ltp": 2456.75,
  "change": 12.30,
  "change_pct": 0.50,
  "open": 2445.00,
  "high": 2461.20,
  "low": 2440.10,
  "close": 2444.45,
  "volume": 4523100,
  "avg_price": 2452.30,
  "upper_circuit": 2689.00,
  "lower_circuit": 2199.90,
  "week_52_high": 0.0,
  "week_52_low": 0.0,
  "market_cap": 0.0,
  "pe_ratio": 0.0,
  "depth": {
    "bids": [
      { "price": 2456.70, "qty": 150, "orders": 3 }
    ],
    "asks": [
      { "price": 2456.80, "qty": 200, "orders": 5 }
    ]
  },
  "is_market_open": true,
  "last_updated": "2026-09-20T14:32:05.123456+05:30"
}
```

| Field | Notes |
|---|---|
| `close` | **Previous** day's close (used as the change baseline), not today's close |
| `week_52_high`, `week_52_low`, `market_cap`, `pe_ratio` | Always `0.0` for now — not sourced from Shoonya's feed. Fixed field names so no response-shape change is needed once a fundamentals source is added later. Treat `0.0` as "not available", not a real value |
| `depth.bids` / `depth.asks` | **Always exactly 5 entries each**, zero-padded (`price: 0.0, qty: 0, orders: 0`) if the broker returned fewer levels — safe to render as a fixed 5-row depth ladder without length checks |
| `last_updated` | ISO 8601, IST (`+05:30`) |

### Errors

| Status | Meaning |
|---|---|
| `404` | `{exchange}/{symbol}` not found in the symbol master |
| `503` | Symbol is valid but no live data available (broker disconnected/timeout) |

---

## 6. Stock chart — `GET /api/stocks/{exchange}/{symbol}/chart`

Example: `GET /api/stocks/NSE/RELIANCE/chart?period=1m`

### Query params

| Param | Type | Required | Notes |
|---|---|---|---|
| `period` | string | no (default `1d`) | One of `1d`, `1w`, `1m`, `6m`, `1y`, `5y`. An unrecognized value silently falls back to `1d` rather than erroring |

### Response — `200`

```json
{
  "symbol": "RELIANCE",
  "period": "1m",
  "candles": [
    {
      "timestamp": 1758345600,
      "open": 2440.00,
      "high": 2461.20,
      "low": 2438.50,
      "close": 2456.75,
      "volume": 892300
    }
  ]
}
```

| Field | Notes |
|---|---|
| `timestamp` | Unix seconds (UTC), start of the candle's bucket |
| `candles` | Ordered oldest → newest. Can be `[]` if the broker has no data for that symbol/period — **this is a normal, renderable empty-chart state, not an error** |
| Candle granularity | `1d`/`1w`/`1m` are true minute-level candles from the broker. `6m`/`1y`/`5y` are aggregated into daily/weekly buckets server-side (broker only provides minute-granularity history); `5y` is capped to ~2 years of actual lookback even though the period label says 5y |

### Errors

| Status | Meaning |
|---|---|
| `404` | `{exchange}/{symbol}` not found in the symbol master |

(A broker outage or empty history does **not** 503 here — it returns `200` with `candles: []`, unlike the quote endpoint.)
