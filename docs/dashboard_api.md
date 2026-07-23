# Dashboard API — Frontend Reference

## Overview

These endpoints power the personalized trading dashboard: header summary tiles,
per-asset-class (ALL / STOCKS / FNO) summary tiles, the portfolio performance
graph, and the underlying positions tables (Stocks tab, F&O tab).

All figures are computed from real data in Postgres (`holdings`, `positions`,
`portfolio_equity_snapshots`, wallet balance) — there are no mocked/static values.

---

## Authentication

Every endpoint below requires a valid JWT, same as the rest of the API:

```
Authorization: Bearer <access_token>
```

Obtained from the existing `/login` endpoint. Missing/expired tokens return:

```json
{ "detail": "Invalid or expired token" }
```
HTTP Status: `401 Unauthorized`

---

## Asset-class buckets

Three buckets are used across these endpoints: `ALL`, `STOCKS`, `FNO`.

| Bucket | Covers |
|--------|--------|
| `ALL` | Equity holdings + F&O positions combined |
| `STOCKS` | Equity holdings only (`holdings` table) |
| `FNO` | F&O positions only (`positions` table) |

Note: **F&O has no live contract price feed**, so `total_unrealized_pnl` for
the `FNO` bucket is always `0` — F&O P&L is realized-only (booked when a
position is reduced/closed). Equity/`STOCKS` unrealized P&L is mark-to-market
against `market_prices`.

---

## Endpoints

1. [GET /getDashboardSummary](#1-get-getdashboardsummary)
2. [GET /getAssetClassSummary](#2-get-getassetclasssummary)
3. [GET /getPortfolioEquityCurve](#3-get-getportfolioequitycurve)
4. [GET /getPortfolioForLoggedInUser](#4-get-getportfolioforloggedinuser)
5. [GET /getPortfolioOfLoggedInUserWithProfitLoss](#5-get-getportfolioofloggedinuserwithprofitloss)
6. [GET /getFnoPositionsForLoggedInUser](#6-get-getfnopositionsforloggedinuser)

---

### 1. GET /getDashboardSummary

Top-level counts used for the "Overview" landing tab: order/trade counts plus
equity-holdings totals. (For the scoped ALL/STOCKS/FNO summary tiles with
buying power and today's P&L, use `/getAssetClassSummary` instead — see #2.)

**Request:**
```
GET /getDashboardSummary
Authorization: Bearer <token>
```

**Response — 200 OK:**
```json
{
  "success": true,
  "userId": 5,
  "dashboard": {
    "orders": {
      "total_orders": 42,
      "pending_orders": 3,
      "executed_orders": 35,
      "partially_executed_orders": 2,
      "cancelled_orders": 1,
      "failed_orders": 1
    },
    "trades": {
      "total_trades": 37,
      "buy_trades": 20,
      "sell_trades": 17
    },
    "portfolio": {
      "total_invested": 125000.00,
      "total_holdings": 131500.00,
      "unrealized_pnl": 6500.00,
      "return_percentage": 5.20,
      "total_positions": 6,
      "last_updated": "2026-07-22T09:42:11.123456"
    },
    "last_updated": "2026-07-22T09:42:11.123456"
  }
}
```

**Response — error (any failure):**
```json
{ "success": false, "message": "An unexpected error occurred while fetching the dashboard summary" }
```

Notes:
- `dashboard.portfolio` reflects **equity holdings only** — it does not include F&O.
- `unrealized_pnl` / `total_holdings` fall back to `avg_price` for any symbol
  with no row yet in `market_prices` (rather than excluding it).

---

### 2. GET /getAssetClassSummary

Scoped summary tiles for a single tab: net value, today's P&L, unrealized P&L,
buying power. This is what powers the "Net Value / Today's P&L / Buying Power"
tile row per tab (Overview = `ALL`, Stocks tab = `STOCKS`, F&O tab = `FNO`).

**Request:**
```
GET /getAssetClassSummary?bucket=ALL
Authorization: Bearer <token>
```

| Query param | Values | Default |
|-------------|--------|---------|
| `bucket` | `ALL`, `STOCKS`, `FNO` | `ALL` |

**Response — 200 OK:**
```json
{
  "success": true,
  "userId": 5,
  "summary": {
    "bucket": "ALL",
    "net_value": 231500.00,
    "todays_pnl": 1250.00,
    "total_unrealized_pnl": 6500.00,
    "buying_power": 100000.00,
    "last_updated": "2026-07-22T09:45:00.123456"
  }
}
```

Example for `bucket=FNO` (note `total_unrealized_pnl` is always 0 — no live
F&O price feed; `net_value` reflects buying power + realized F&O P&L only):
```json
{
  "success": true,
  "userId": 5,
  "summary": {
    "bucket": "FNO",
    "net_value": 100000.00,
    "todays_pnl": 0.00,
    "total_unrealized_pnl": 0.00,
    "buying_power": 100000.00,
    "last_updated": "2026-07-21T15:29:47.000000"
  }
}
```

**Response — invalid bucket:**
```json
{ "success": false, "message": "Invalid bucket: FUTURES" }
```

**Response — error:**
```json
{ "success": false, "message": "An unexpected error occurred while fetching the asset class summary" }
```

`todays_pnl` is computed as `net_value - net_value_at_first_snapshot_today`,
so it reads `0` until the background capture job has taken at least one
snapshot today (captures every 60s while market is open, 300s while closed).

---

### 3. GET /getPortfolioEquityCurve

Time-series points for the portfolio performance graph, with time-range tabs.

**Request:**
```
GET /getPortfolioEquityCurve?bucket=ALL&range=1M
Authorization: Bearer <token>
```

| Query param | Values | Default |
|-------------|--------|---------|
| `bucket` | `ALL`, `STOCKS`, `FNO` | `ALL` |
| `range` | `1D`, `1W`, `1M`, `3M`, `1Y`, `All` | `1M` |

**Response — 200 OK:**
```json
{
  "success": true,
  "userId": 5,
  "bucket": "ALL",
  "range": "1M",
  "points": [
    {
      "net_value": 228750.00,
      "total_unrealized_pnl": 5200.00,
      "buying_power": 100000.00,
      "captured_at": "2026-06-22T09:20:00.000000"
    },
    {
      "net_value": 230100.00,
      "total_unrealized_pnl": 5900.00,
      "buying_power": 100000.00,
      "captured_at": "2026-06-23T09:20:00.000000"
    },
    {
      "net_value": 231500.00,
      "total_unrealized_pnl": 6500.00,
      "buying_power": 100000.00,
      "captured_at": "2026-07-22T09:45:00.123456"
    }
  ]
}
```

**Response — no data yet (new user / first day):**
```json
{
  "success": true,
  "userId": 5,
  "bucket": "ALL",
  "range": "1D",
  "points": []
}
```

**Response — invalid bucket/range:**
```json
{ "success": false, "message": "Invalid bucket: FUTURES" }
```
```json
{ "success": false, "message": "Invalid range: 5Y" }
```

Points are ordered oldest → newest; plot directly as a line chart (x =
`captured_at`, y = `net_value`).

---

### 4. GET /getPortfolioForLoggedInUser

Raw equity holdings list (no P&L calculation) — legacy/simple portfolio view.

**Request:**
```
GET /getPortfolioForLoggedInUser
Authorization: Bearer <token>
```

**Response — 200 OK (has holdings):**
```json
{
  "generated_at": "2026-07-22T09:46:02.000000",
  "success": true,
  "userId": 5,
  "total_positions": 2,
  "portfolio": [
    { "symbol": "RELIANCE", "quantity": 10, "avg_price": 2450.00, "asset_type": "EQUITY", "updated_at": "2026-07-15T11:20:00.000000" },
    { "symbol": "TCS", "quantity": 5, "avg_price": 3800.00, "asset_type": "EQUITY", "updated_at": "2026-07-18T10:05:00.000000" }
  ]
}
```

**Response — no holdings:**
```json
{ "userId": 5, "message": "No portfolio found for User" }
```

---

### 5. GET /getPortfolioOfLoggedInUserWithProfitLoss

Equity holdings with live P&L per symbol, optionally scoped to a bucket. This
is what feeds the **Stocks tab** positions table (equity only — see note below
on `FNO`).

**Request:**
```
GET /getPortfolioOfLoggedInUserWithProfitLoss
GET /getPortfolioOfLoggedInUserWithProfitLoss?bucket=STOCKS
Authorization: Bearer <token>
```

| Query param | Values | Default |
|-------------|--------|---------|
| `bucket` | `STOCKS` (or omit for all equity holdings) | *(none — all holdings)* |

**Response — 200 OK:**
```json
{
  "success": true,
  "user_id": 5,
  "total_pnl": 875.50,
  "portfolio": [
    {
      "symbol": "RELIANCE",
      "quantity": 10,
      "avg_price": 2450.00,
      "current_price": 2510.30,
      "pnl": 603.00,
      "asset_type": "EQUITY"
    },
    {
      "symbol": "TCS",
      "quantity": 5,
      "avg_price": 3800.00,
      "current_price": 3854.50,
      "pnl": 272.50,
      "asset_type": "EQUITY"
    }
  ]
}
```

**Response — `bucket=FNO` (rejected — use the dedicated F&O endpoints instead):**

F&O trades are tracked in a separate `positions` table (see #6), not in
`holdings`, so this bucket can never return F&O data here:
```json
{
  "success": false,
  "message": "F&O positions are tracked separately - use GET /getFnoPositionsForLoggedInUser or GET /getAssetClassSummary?bucket=FNO instead"
}
```

**Response — invalid bucket:**
```json
{ "success": false, "message": "Invalid bucket: CRYPTO" }
```

**Response — no holdings:**
```json
{ "success": false, "message": "No portfolio data found for user" }
```

---

### 6. GET /getFnoPositionsForLoggedInUser

F&O positions book (futures & options) — feeds the **F&O tab** positions
table. One row per `(symbol, product_type)` — e.g. an MIS and an NRML
position on the same option contract appear as two separate rows.

**Request:**
```
GET /getFnoPositionsForLoggedInUser
Authorization: Bearer <token>
```

**Response — 200 OK:**
```json
{
  "success": true,
  "userId": 5,
  "total_positions": 2,
  "positions": [
    {
      "symbol": "NIFTY07JUL2623800CE",
      "underlying": "NIFTY",
      "exchange": "NFO",
      "expiry": "2026-07-07",
      "strike": 23800.00,
      "option_type": "CE",
      "contract_type": "OPTION",
      "lot_size": 75,
      "product_type": "NRML",
      "netqty": 75,
      "netavgprc": 248.26,
      "buyqty": 75,
      "sellqty": 0,
      "buyavgprc": 248.26,
      "sellavgprc": 0.00,
      "realized_pnl": 0.00,
      "status": "OPEN"
    },
    {
      "symbol": "NIFTY14JUL2624500CE",
      "underlying": "NIFTY",
      "exchange": "NFO",
      "expiry": "2026-07-14",
      "strike": 24500.00,
      "option_type": "CE",
      "contract_type": "OPTION",
      "lot_size": 75,
      "product_type": "MIS",
      "netqty": -75,
      "netavgprc": 6.10,
      "buyqty": 0,
      "sellqty": 75,
      "buyavgprc": 0.00,
      "sellavgprc": 6.10,
      "realized_pnl": 0.00,
      "status": "OPEN"
    }
  ]
}
```

**Response — no positions:**
```json
{ "success": true, "userId": 5, "total_positions": 0, "positions": [] }
```

Field notes:
- `netqty` — positive = net long, negative = net short, `0` = fully closed (`status: "CLOSED"`).
- `netavgprc` — cost basis of the currently open exposure (`0` once closed).
- `buyqty` / `sellqty` / `buyavgprc` / `sellavgprc` — lifetime cumulative buy-side and sell-side stats (informational; not used to derive P&L).
- `realized_pnl` — cumulative booked P&L for this `(symbol, product_type)`, carried forward correctly across close → reopen cycles.
- There is **no `current_price` / mark-to-market field** — no live F&O contract price feed exists yet, so unrealized P&L for open F&O positions is not computed.

---

## Error shape summary

All endpoints return HTTP `200` with `"success": false` for handled errors
(validation, no-data), so check `success` rather than relying solely on HTTP
status. The only non-200 case across this set is a missing/expired token
(`401`, see Authentication above).

```json
{ "success": false, "message": "<human-readable reason>" }
```
