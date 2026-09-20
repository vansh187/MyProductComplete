# Order API — Frontend Integration Guide (Buy/Sell)

All endpoints below live under `api/orders.py`. Every endpoint requires
authentication.

## Authentication

Send a standard bearer token on every request:

```
Authorization: Bearer <jwt_token>
```

Requests without a valid token get `401 Unauthorized`.

---

## Two ways to place an order — pick the right one

| | `POST /orders` | `POST /createLiveOrder` |
|---|---|---|
| What it does | Simulated order, matched peer-to-peer inside our own system | **Real order placed on the live Shoonya broker account** |
| Symbols allowed | Equity + F&O | **F&O (options/futures) only** — rejects equity |
| When it's used | Default order flow today | Only when the backend's live-trading switch is turned on |
| Fills come from | Our internal matching engine | The real exchange (via Shoonya) |

**Frontend does not decide which one to call based on its own logic** — use
`POST /orders` for the normal buy/sell flow. `POST /createLiveOrder` is a
separate, explicitly-invoked endpoint for real-money F&O orders and will
itself reject the request (400) if live trading isn't currently enabled on
the backend, so it's safe to wire up ahead of time but don't call it as your
default "place order" action until told the live switch is on.

---

## 1. Place an order — `POST /orders`

Simulated order (equity or F&O), matched internally.

### Request body

```json
{
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "side": "BUY",
  "quantity": 10,
  "order_type": "LIMIT",
  "price": 2450.50,
  "trigger_price": null,
  "product_type": "MIS",
  "validity": "DAY",
  "client_order_id": "optional-your-own-id"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `symbol` | string | yes | Up to 50 chars (F&O symbols like `NIFTY14JUL2623950CE` are long) |
| `exchange` | enum | no (default `NSE`) | `NSE`, `BSE`, `NFO`, `BFO`, `NCDEX`, `MCXSX` |
| `side` | enum | yes | `BUY` or `SELL` |
| `quantity` | int | yes | Must be `> 0`. For F&O, must be a multiple of the contract's lot size |
| `order_type` | enum | no (default `MARKET`) | `MARKET`, `LIMIT`, `STOP`, `STOPLIMIT` |
| `price` | float | required for `LIMIT`/`STOPLIMIT` | Must be `> 0` |
| `trigger_price` | float | required for `STOP`/`STOPLIMIT` | Must be `> 0` |
| `product_type` | enum | no (default `MIS`) | `MIS`, `CNC`, `NRML` |
| `validity` | enum | no (default `DAY`) | `DAY`, `IOC`, `TTL`, `GTC` |
| `client_order_id` | string | no | Your own tracking id, echoed back on the order |

`STOP`/`STOPLIMIT` orders start in a dormant state (`PENDING_TRIGGER`) and
only become active once the market price crosses `trigger_price` — they are
not immediately matchable.

### Success response — `200`

```json
{
  "success": true,
  "order_id": 101,
  "execution": { "...": "internal matching result" }
}
```

### Error responses

| Status | Meaning |
|---|---|
| `400` | Bad request body / validation failure (missing price for LIMIT, invalid enum value, etc.) |
| `400` | Insufficient wallet balance (BUY) or insufficient margin (F&O SELL/FUTURES) |
| `401` | Not authenticated |
| `500` | Order creation failed |

---

## 2. Place a real F&O order — `POST /createLiveOrder`

Same request body shape as `POST /orders` (see above), but:
- **F&O only.** Equity symbols get `400 "createLiveOrder only supports F&O (OPTION/FUTURES) symbols"`.
- **Quantity must be an exact multiple of the contract's lot size** — checked before anything touches the broker.
- Places a real order on the Shoonya master account. No internal matching — the real exchange is the sole source of truth for the fill.

### Success response — `200`

```json
{
  "success": true,
  "order_id": 101,
  "broker_order_id": "20052000000017",
  "status": "PENDING"
}
```

### Special response — `202` (status uncertain, NOT a failure)

```json
{ "detail": "Order was submitted but broker confirmation timed out - check order status before retrying" }
```

This means the broker call timed out or returned something unexpected — the
order **may have actually gone through**. **Do not let the user retry/resubmit
immediately on a 202** — show "order status uncertain, please check your
order book" and let them refresh/poll `GET /orders/{id}` instead. Re-submitting
blindly on a 202 risks a duplicate real order.

### Error responses

| Status | Meaning |
|---|---|
| `400` | Live trading disabled, not an F&O symbol, bad lot size, broker rejected the order (see `detail` for the broker's reason) |
| `503` | No live Shoonya session / session disconnected |
| `202` | Status uncertain — see above, treat as "unknown", not "failed" |
| `401` | Not authenticated |

---

## 3. Get all orders — `GET /orders`

```json
{
  "success": true,
  "user_id": 42,
  "orders": [
    {
      "id": 101,
      "symbol": "RELIANCE",
      "side": "BUY",
      "quantity": 10,
      "price": 2450.50,
      "status": "PENDING",
      "exchange": "NSE",
      "order_type": "LIMIT",
      "product_type": "MIS",
      "validity": "DAY",
      "trigger_price": null,
      "client_order_id": null,
      "broker_order_id": null,
      "created_at": "2026-07-14T10:00:00",
      "updated_at": "2026-07-14T10:00:00"
    }
  ]
}
```

`broker_order_id` is only populated for real orders placed via
`/createLiveOrder` (the real Shoonya order number) — use its presence to show
a "LIVE" badge in the UI if useful; it's `null` for every simulated order.

### Order `status` values

| Status | Meaning |
|---|---|
| `PENDING` | Resting, unfilled, awaiting a match / broker fill |
| `PENDING_TRIGGER` | STOP/STOPLIMIT order, not yet triggered |
| `PARTIALLY_EXECUTED` | Some quantity filled, rest still resting |
| `EXECUTED` | Fully filled |
| `CANCELLED` | Cancelled (by user or system) |
| `REJECTED` | Rejected (validation, margin, or broker reject) |

---

## 4. Get a single order — `GET /orders/{order_id}`

```json
{ "success": true, "message": "Order retrieved successfully", "order": { "...": "same shape as above" } }
```

`404` if not found (or belongs to another user).

---

## 5. Cancel an order — `POST /orders/{order_id}/cancel`

```json
{ "success": true, "message": "Order cancelled successfully", "order_id": 101 }
```

Only works for orders still `PENDING`/`PENDING_TRIGGER` (zero fills so far).
Automatically refunds any wallet debit / releases any margin block.

**Important:** if the order has a `broker_order_id` set (i.e. it was placed
via `/createLiveOrder` and is a real order at the broker), this endpoint
returns `400` — cancelling a real broker order isn't supported through this
endpoint yet. Don't offer a cancel button for orders where
`broker_order_id` is non-null.

| Status | Meaning |
|---|---|
| `400` | No pending order to cancel, OR it's a live broker order (see above) |
| `404`/`401` | as usual |

---

## 6. Modify an order — `PUT /orders/{order_id}`

```json
{ "price": 2460.0 }
```

Any of `price`, `quantity`, `trigger_price` — at least one required, only the
fields you send are changed. Same restrictions as cancel:

- Only `PENDING`/`PENDING_TRIGGER` orders.
- **Not supported for margin-required orders** (OPTION SELL, FUTURES any side) — `400`, cancel and re-place instead.
- **Not supported for live broker orders** (`broker_order_id` set) — `400`, same reasoning as cancel. Don't show an edit option for these.
- If you increase a BUY order's value, insufficient balance returns `400`.
- If the order gets matched/cancelled right as you modify it, you get `409 Conflict` — treat this as "refresh and check the order's current state," not a retryable error.

```json
{ "success": true, "message": "Order modified successfully", "order_id": 101 }
```

---

## Quick reference — status codes across all 6 endpoints

| Status | Meaning |
|---|---|
| `200` | Success |
| `202` | (createLiveOrder only) Submitted, confirmation uncertain |
| `400` | Validation / business-rule failure — read `detail` |
| `401` | Missing/invalid auth token |
| `404` | Order not found |
| `409` | (modify only) Lost a race — order changed state concurrently |
| `500` | Unexpected server error |

Every error response has the shape `{ "detail": "human-readable message" }` —
safe to show `detail` directly to the user for `400`/`409`/`202`.
