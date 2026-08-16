# Mutual Funds API — Frontend Integration Guide

All endpoints below live under `api/mutualFunds.py`, mounted at
`/api/mutual-funds`. **No authentication required** — same as market-data
endpoints like Top Movers, these are public browse/read endpoints (not tied
to a user account).

All responses are JSON. All numeric return fields (`return_1m`, `return_3y`,
etc.) are **percentages** (e.g. `14.6` means +14.6%), rounded to 2 decimals,
and can be `null` if the fund doesn't have enough history for that period
(e.g. a 2-year-old fund has no `return_5y`).

---

## Page → endpoint map

| Screen | Endpoint |
|---|---|
| "Mutual Funds" landing page (Popular Funds + Collections tiles) | `GET /explore` |
| Tapping a Collection tile (e.g. "Large Cap") | `GET /collections/{key}` |
| "All Mutual Funds" / search bar | `GET /search` |
| Filter chips on the search/list page | `GET /categories` |
| Fund detail page header (name, category, fundamentals) | `GET /{scheme_code}` |
| Fund detail page chart + period returns (1M/6M/1Y/3Y/5Y/All tabs) | `GET /{scheme_code}/nav-chart` |

---

## 1. Explore / landing page — `GET /explore`

Returns Popular Funds + the Collections tile list in one call.

### Request

```
GET /api/mutual-funds/explore
```

### Response — `200`

```json
{
  "popular_funds": [
    {
      "scheme_code": 122639,
      "scheme_name": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth",
      "fund_house": "PPFAS Mutual Fund",
      "scheme_category": "Equity Scheme - Flexi Cap Fund",
      "scheme_type": "Open Ended Schemes",
      "latest_nav": 92.3269,
      "return_3y": 14.6
    },
    {
      "scheme_code": 118989,
      "scheme_name": "HDFC Mid Cap Fund - Growth Option - Direct Plan",
      "fund_house": "HDFC Mutual Fund",
      "scheme_category": "Equity Scheme - Mid Cap Fund",
      "scheme_type": "Open Ended Schemes",
      "latest_nav": 236.719,
      "return_3y": 20.5
    }
  ],
  "collections": [
    { "key": "high-return",     "title": "High return",    "icon_hint": "trending-up" },
    { "key": "best-sip-funds",  "title": "Best SIP funds",  "icon_hint": "wallet" },
    { "key": "gold-silver",     "title": "Gold & Silver",   "icon_hint": "ingot" },
    { "key": "large-cap",       "title": "Large Cap",       "icon_hint": "building" },
    { "key": "mid-cap",         "title": "Mid Cap",         "icon_hint": "building-2" },
    { "key": "small-cap",       "title": "Small Cap",       "icon_hint": "storefront" }
  ]
}
```

`collections[].key` is what you pass to endpoint #2 below when a tile is tapped.
`popular_funds` is capped to 4 entries (matches the reference "4 cards" layout).

`icon_hint` is a semantic name (`trending-up`, `wallet`, `ingot`, `building`,
`building-2`, `storefront`), not a URL — map it to whatever icon set the
frontend uses.

---

## 2. Collection fund list — `GET /collections/{key}`

The fund list for one tapped Collection tile.

### Request

```
GET /api/mutual-funds/collections/large-cap?page=1&page_size=20
```

| Param | Type | Required | Default | Notes |
|---|---|---|---|---|
| `key` (path) | string | yes | — | One of the keys from `/explore`'s `collections[]` |
| `page` | int | no | `1` | 1-indexed |
| `page_size` | int | no | `20` | |

### Response — `200`

Same shape as each entry in `popular_funds` above, as a JSON array:

```json
[
  {
    "scheme_code": 118269,
    "scheme_name": "CANARA ROBECO LARGE CAP FUND - DIRECT PLAN - GROWTH OPTION",
    "fund_house": "Canara Robeco Mutual Fund",
    "scheme_category": "Equity Scheme - Large Cap Fund",
    "scheme_type": "Open Ended Schemes",
    "latest_nav": 73.56,
    "return_3y": 12.26
  }
]
```

An unknown `key` returns an empty array `[]`, not an error.

---

## 3. Search — `GET /search`

Powers the "All Mutual Funds" list and the search bar.

### Request

```
GET /api/mutual-funds/search?q=gold&category=Other%20Scheme%20-%20Gold%20ETF&fund_house=Kotak%20Mahindra%20Mutual%20Fund&page=1&page_size=20
```

| Param | Type | Required | Notes |
|---|---|---|---|
| `q` | string | no | Fuzzy match against fund name (e.g. `"gold"`, `"parag parikh"`) |
| `category` | string | no | Exact match — get valid values from `/categories` |
| `fund_house` | string | no | Exact match — get valid values from `/categories` |
| `page` | int | no | Default `1` |
| `page_size` | int | no | Default `20` |

All params are optional — `GET /search` with no params returns the first page
of every active fund, alphabetically.

### Response — `200`

Same array shape as `/collections/{key}` above:

```json
[
  {
    "scheme_code": 106193,
    "scheme_name": "KOTAK GOLD ETF",
    "fund_house": "Kotak Mahindra Mutual Fund",
    "scheme_category": "Other Scheme - Gold ETF",
    "scheme_type": "Open Ended Schemes",
    "latest_nav": 123.9747,
    "return_3y": 34.75
  },
  {
    "scheme_code": 119788,
    "scheme_name": "SBI GOLD FUND- DIRECT PLAN - GROWTH",
    "fund_house": "SBI Mutual Fund",
    "scheme_category": "Other Scheme - FoF Domestic",
    "scheme_type": "Open Ended Schemes",
    "latest_nav": 45.6014,
    "return_3y": 34.83
  }
]
```

`latest_nav`/`return_3y` can be `null` for a fund that hasn't finished its
initial data sync yet — handle that in the UI (e.g. show "—").

---

## 4. Filter facets — `GET /categories`

Populates the category/fund-house filter chips on the search page.

### Request

```
GET /api/mutual-funds/categories
```

### Response — `200`

```json
{
  "categories": [
    "Equity Scheme - ELSS",
    "Equity Scheme - Flexi Cap Fund",
    "Equity Scheme - Large Cap Fund",
    "Equity Scheme - Mid Cap Fund",
    "Equity Scheme - Small Cap Fund",
    "Other Scheme - FoF Domestic",
    "Other Scheme - Gold ETF"
  ],
  "fund_houses": [
    "Canara Robeco Mutual Fund",
    "Franklin Templeton Mutual Fund",
    "HDFC Mutual Fund",
    "Kotak Mahindra Mutual Fund",
    "PPFAS Mutual Fund",
    "SBI Mutual Fund"
  ]
}
```

Both lists are alphabetical and only include categories/fund houses that have
at least one active fund right now (grows as more funds finish syncing).

---

## 5. Fund detail (header) — `GET /{scheme_code}`

Identity + fundamentals for the fund detail page header (name, category,
tags). **Does not include NAV or returns** — call endpoint #6 for those.

### Request

```
GET /api/mutual-funds/106193
```

### Response — `200`

```json
{
  "scheme_code": 106193,
  "scheme_name": "KOTAK GOLD ETF",
  "fund_house": "Kotak Mahindra Mutual Fund",
  "scheme_category": "Other Scheme - Gold ETF",
  "scheme_type": "Open Ended Schemes",
  "isin_growth": "INF174KA1HJ8",
  "isin_div_reinvestment": null,
  "min_sip_amount": null,
  "fund_size_aum": null,
  "expense_ratio": null,
  "rating": null,
  "holdings": "unavailable"
}
```

> **Frontend note:** `min_sip_amount`, `fund_size_aum`, `expense_ratio`,
> `rating`, and `holdings` are **always `null`/`"unavailable"` in v1** — our
> data source (mfapi.in) doesn't provide them. Design the detail page so
> these sections gracefully collapse/hide rather than showing "null" or a
> broken layout. This is a known gap, not a bug, and may be filled in by a
> future data source.

### Error — `404`

```json
{ "detail": "Scheme not found." }
```

---

## 6. Fund detail chart + returns — `GET /{scheme_code}/nav-chart`

Powers the chart and the period-return figure on the fund detail page
(e.g. "+13.97% · 6M return", the `1M | 6M | 1Y | 3Y | 5Y | All` tabs).

**This endpoint fetches live data on most requests** (not just from our
database) — expect it to be slower than the other endpoints (typically
200ms–1.5s vs <50ms for search/explore), especially the first time a given
fund is viewed. Show a loading state on tab switches too, not just first load.

### Request

```
GET /api/mutual-funds/106193/nav-chart?period=1y
```

| Param | Type | Required | Default | Allowed values |
|---|---|---|---|---|
| `period` | string | no | `6m` | `1m`, `6m`, `1y`, `3y`, `5y`, `all` |

### Response — `200`

```json
{
  "scheme_code": 106193,
  "period": "1y",
  "points": [
    { "nav_date": "2025-08-07", "nav": 84.1418 },
    { "nav_date": "2025-08-08", "nav": 84.5869 },
    { "nav_date": "2025-08-11", "nav": 83.7542 }
  ],
  "returns": {
    "return_1m": 2.43,
    "return_6m": 5.7,
    "return_1y": 12.62,
    "return_3y": 20.5,
    "return_5y": 20.71,
    "day_change_pct": 0.35,
    "latest_nav": 236.719
  },
  "is_live": true
}
```

- `points` is the chart series **for the requested `period` only** (e.g.
  `period=1m` returns ~1 month of daily points, not the full history) —
  ordered oldest → newest.
- `returns` always contains **all** trailing periods (1M/6M/1Y/3Y/5Y +
  1-day change), regardless of which `period` was requested for the chart —
  so you can show "+13.97% · 6M" as the headline figure while the chart tabs
  switch independently. A period the fund is too young for is `null`.
- **`is_live`**: `true` means this response came from a live fetch just now
  (accurate as of today). `false` means mfapi.in was unreachable and this is
  our last-known-good data from the database instead — **if `false`, show a
  subtle "data may not be current" indicator** rather than presenting it as
  live.

### Error — `404`

Currently this endpoint does not 404 for an unknown `scheme_code` the way
`GET /{scheme_code}` does — an unknown code with no live or stored data will
return an empty `points` array and all-`null` `returns` with `is_live: false`.
Treat an empty `points` array as "no data available for this fund" in the UI.

---

## Quick reference — all endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/mutual-funds/explore` | Landing page: Popular Funds + Collections tiles |
| `GET` | `/api/mutual-funds/collections/{key}` | Fund list for one Collection tile |
| `GET` | `/api/mutual-funds/search` | Search / "All Mutual Funds" list |
| `GET` | `/api/mutual-funds/categories` | Filter chip values |
| `GET` | `/api/mutual-funds/{scheme_code}` | Fund detail header |
| `GET` | `/api/mutual-funds/{scheme_code}/nav-chart` | Fund detail chart + returns (live) |
