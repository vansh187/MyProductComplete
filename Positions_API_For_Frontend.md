# Positions API — for Frontend Integration

## How it works (confirmed in the backend)

Every time an F&O order executes — fully or partially — the position for that instrument is created or updated **immediately, in the same transaction as the trade fill** (`service/positionService.py: PositionService.apply_fill`). There is no delay and no separate step: the moment a trade settles, the position row is written to the live cache this endpoint reads from. So the frontend flow is simply:

1. User places an order.
2. If/when it fills (even partially), call the positions endpoint again (or just re-poll on an interval) — the new/updated position will already be there.

---

## Endpoint

### `GET /getPositionsForLoggedInUser`

**Auth:** Bearer token (same as every other authenticated endpoint) — send the JWT in the `Authorization` header.

**Method/Path:** `GET /getPositionsForLoggedInUser`

**Returns:** All of the authenticated user's currently **OPEN** positions. Closed positions (netqty back to 0) are not returned here — they're history, not live positions.

#### Response shape

```json
{
  "success": true,
  "user_id": 42,
  "total_positions": 2,
  "positions": [
    {
      "tsym": "NIFTY14JUL2623950CE",
      "broker": "Shoonya",
      "token": "12345",
      "exchange": "NFO",
      "underlying": "NIFTY",
      "expiry": "2026-07-14",
      "strike": 23950.0,
      "option_type": "CE",
      "lot_size": 75,
      "product_type": "MIS",
      "netqty": 75,
      "netavgprc": 101.15,
      "buyqty": 75,
      "sellqty": 0,
      "buyavgprc": 101.15,
      "sellavgprc": 0,
      "realized_pnl": 0,
      "unrealized_pnl": 187.5,
      "total_pnl": 187.5,
      "lp": 103.65,
      "last_tick_ts": 1752400000,
      "status": "OPEN",
      "created_at": 1752398000,
      "updated_at": 1752400000
    }
  ]
}
```

#### Field reference

| Field | Type | Meaning |
|---|---|---|
| `tsym` | string | Tradingsymbol (uppercased) — the contract identifier |
| `exchange` | string | `NFO` or `BFO` for F&O; equity/other exchanges won't appear here (this endpoint is F&O positions only — equity holdings are a separate endpoint, see below) |
| `underlying` | string / null | e.g. `NIFTY`, `BANKNIFTY`. Null if the symbol wasn't resolvable (e.g. a futures contract not in the option master) |
| `expiry` | string / null | ISO date `YYYY-MM-DD` |
| `strike` | number / null | Strike price (options only) |
| `option_type` | string / null | `CE` or `PE` (options only); null for futures |
| `lot_size` | number | Contract lot size |
| `product_type` | string | `MIS` / `CNC` / `NRML` |
| `netqty` | int | Net open quantity — **positive = long, negative = short, never 0 in this list** (0 means closed, not returned) |
| `netavgprc` | number | Weighted average entry price of the current net position |
| `buyqty` / `sellqty` | int | Cumulative buy/sell quantity for this instrument so far |
| `buyavgprc` / `sellavgprc` | number | Weighted average buy/sell price |
| `realized_pnl` | number | Banked P&L from quantity already closed out |
| `unrealized_pnl` | number | `(lp − netavgprc) × netqty` — mark-to-market on the open quantity |
| `total_pnl` | number | `realized_pnl + unrealized_pnl` |
| `lp` | number | Last traded price this position has seen (seeded from the fill price, refreshed live if/when a live broker tick arrives) |
| `last_tick_ts` | number | Unix timestamp of the last price update |
| `status` | string | Always `"OPEN"` in this list |
| `created_at` / `updated_at` | number | Unix timestamps |

**F&O detection for the frontend:** every row from this endpoint IS an F&O position by definition (the endpoint only ever returns F&O positions). No extra filtering needed on the frontend side.

---

## How to show live P&L updates

**For tomorrow, simplest and fastest:** poll `GET /getPositionsForLoggedInUser` on an interval (e.g. every 2–5 seconds) while the Positions screen is open. This endpoint is cheap (reads straight from Redis) and always reflects the latest fill/tick.

A real push-based streaming endpoint (so P&L updates without polling) is planned as a fast-follow, mirroring the existing SSE pattern already used for the option chain screen — flag it if you want that scoped next.

---

## Related endpoint (not F&O — equity holdings, for reference)

If the frontend needs equity/cash holdings too (separate from F&O positions), that's a different endpoint with a different shape:
- `GET /getPortfolioForLoggedInUser`
- `GET /getPortfolioOfLoggedInUserWithProfitLoss`

These return `symbol, quantity, avg_price, current_price, pnl` — no strike/expiry/option_type, since they're plain equity holdings, not F&O positions.
