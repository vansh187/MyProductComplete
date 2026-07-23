# Manual Test Scenarios — STOP-order (14), Modify-order (15), and Code-Review Fixes

Use this to manually verify this session's changes on a real deployment (staging or prod) before/after rollout. Each scenario lists: **setup → action → expected result**.

---

## 1. STOP / STOP-LIMIT orders don't execute immediately (ticket 14)

### 1.1 Plain STOP order stays dormant until triggered
- **Setup:** Pick a liquid symbol with a resting SELL LIMIT order at price 100 already in the book (or place one from a second test user).
- **Action:** User A places a SELL STOP order, `trigger_price = 90`, no limit price, quantity 10.
- **Expected:**
  - Order is created with status `PENDING_TRIGGER` (check `GET /orders/{id}`), **not** `PENDING`.
  - It does **not** fill immediately, even though a compatible BUY order may be resting in the book.
  - No trade appears in trade history for this order yet.

### 1.2 STOP order triggers when a real trade crosses the trigger price
- **Setup:** User A has a resting SELL STOP order, `trigger_price = 90` (from 1.1).
- **Action:** Have two other users trade the same symbol such that a real trade executes at price ≤ 90 (e.g., user B places BUY LIMIT @ 90, user C places SELL LIMIT @ 90, they match).
- **Expected:**
  - Immediately after that trade commits, User A's STOP order flips to `PENDING` (or fills, if a compatible counter-order exists) — check via `GET /orders/{id}` or trade history.
  - If a compatible BUY order exists in the book, User A's order fills at that time.

### 1.3 STOP-LIMIT order keeps its own limit price after triggering
- **Setup:** User places a SELL STOPLIMIT order: `trigger_price = 90`, `price = 89.5` (limit).
- **Action:** Trigger it (a real trade at ≤ 90 occurs, per 1.2).
- **Expected:** Once triggered, the order becomes a resting SELL LIMIT @ 89.5 (not marketable at whatever price triggered it) — verify via `GET /orders/{id}` that `price` is still 89.5.

### 1.4 Cancelling a not-yet-triggered STOP order
- **Setup:** User A has a resting `PENDING_TRIGGER` STOP order (from 1.1).
- **Action:** Call `POST /orders/{id}/cancel`.
- **Expected:**
  - Cancel succeeds (200, `success: true`).
  - Order status becomes `CANCELLED`.
  - It never triggers even if the trigger price is crossed afterward.

### 1.5 Cancelled orders can no longer be silently matched (regression this ticket also fixed)
- **Setup:** User A places a resting LIMIT order (any normal order), then cancels it.
- **Action:** Have another user place a compatible opposite order that would have matched the cancelled order's price/quantity.
- **Expected:** The cancelled order does **not** get filled. The new incoming order either matches something else or stays `PENDING` unmatched. (Before this fix, a cancelled order's `order_book` row was never marked cancelled, so it could still be matched.)

### 1.6 Normal MARKET/LIMIT orders are unaffected
- **Setup:** Two users, compatible BUY/SELL LIMIT orders at the same price.
- **Action:** Place both orders as before.
- **Expected:** They match and execute immediately exactly as before this session's changes — no new delay, no new required field, same response shape.

---

## 2. Order modify/amend endpoint (ticket 15)

### 2.1 Increase a resting BUY order's price — wallet debited for the delta
- **Setup:** User has ₹50,000 wallet balance. Places a BUY LIMIT order, qty 10 @ price 100 (debits ₹1,000 from wallet at creation).
- **Action:** Call `PUT /orders/{id}` with `{"price": 150}`.
- **Expected:**
  - Response 200, order's price becomes 150.
  - Wallet is debited an additional ₹500 (new required 1,500 − old required 1,000).
  - Total wallet debit for this order is now ₹1,500.

### 2.2 Decrease a resting BUY order's price — wallet refunded the delta
- **Setup:** Same order as 2.1 (now at price 150, qty 10).
- **Action:** `PUT /orders/{id}` with `{"price": 100}`.
- **Expected:** Wallet is credited back ₹500; order's price is now 100.

### 2.3 Insufficient balance for an increase is rejected cleanly
- **Setup:** User's wallet balance is only ₹100 more than what's already committed to this order.
- **Action:** `PUT /orders/{id}` with a price increase requiring more than ₹100 additional.
- **Expected:** 400 response, "Insufficient balance..." message. Order is **unchanged** (price/qty still the old values). Wallet balance is unchanged (no partial debit left behind).

### 2.4 Modifying quantity recomputes the required balance correctly
- **Setup:** BUY order, qty 10 @ price 100.
- **Action:** `PUT /orders/{id}` with `{"quantity": 15}`.
- **Expected:** Wallet debited an additional ₹500 (5 extra units × 100).

### 2.5 SELL orders never touch the wallet
- **Setup:** User places a SELL LIMIT order (equity, no cash debit at creation).
- **Action:** `PUT /orders/{id}` changing price and/or quantity.
- **Expected:** Order updates successfully; wallet balance is untouched throughout.

### 2.6 Margin-required (F&O) orders are rejected, not half-modified
- **Setup:** User has a resting OPTION SELL order (margin-blocked) or a FUTURES order.
- **Action:** `PUT /orders/{id}` with any field change.
- **Expected:** 400 response explaining modification isn't supported for margin-required orders yet; nothing changes (order, wallet, or margin block).

### 2.7 Only PENDING / PENDING_TRIGGER orders can be modified
- **Setup:** An order that has already fully executed.
- **Action:** `PUT /orders/{id}` on it.
- **Expected:** 400 "Only pending (unfilled) orders can be modified." Nothing changes.

### 2.8 Modifying trigger_price on a STOP order
- **Setup:** Resting STOP order, `trigger_price = 90`.
- **Action:** `PUT /orders/{id}` with `{"trigger_price": 92}`.
- **Expected:** Order's trigger_price becomes 92; it now triggers off the new price level, not the old one.

### 2.9 Concurrent modify requests don't double-charge (race protection)
- **Setup:** A resting BUY order, qty 10 @ price 100.
- **Action:** Fire two `PUT /orders/{id}` requests back-to-back/concurrently, e.g. one changing price to 150, another changing price to 200.
- **Expected:** Exactly one of them succeeds (200) and the other gets a 409 "Order could not be modified..." with its wallet delta correctly reversed (not double-debited, not left with a stray partial debit). Final order price matches whichever request actually won.

### 2.10 Concurrent trigger_price-only modifies also protected (code-review fix)
- **Setup:** Resting STOP order, `trigger_price = 90`.
- **Action:** Fire two concurrent `PUT /orders/{id}` requests, each changing only `trigger_price` (e.g., one to 91, one to 92) — no price/quantity change in either.
- **Expected:** Exactly one wins; the other gets 409. (Before this code-review fix, both could silently succeed, with only the second one's value actually persisted and no error to either caller.)

### 2.11 Cancel refund reflects the actual amount cancelled, not a stale read (code-review fix)
- **Setup:** BUY order qty 10 @ price 100 (₹1,000 debited).
- **Action:** Modify the order's price to 200 (now ₹2,000 committed), then immediately cancel it.
- **Expected:** Wallet is refunded the full ₹2,000 (the current/actual committed amount), not the original ₹1,000.

---

## 3. General regression checks (run after any deploy)

- [ ] Place a normal MARKET BUY order — executes immediately, wallet debited correctly.
- [ ] Place a normal LIMIT SELL order — rests in book, matches when a compatible BUY arrives.
- [ ] Cancel a normal pending order — wallet refunded (if BUY), order no longer matchable.
- [ ] Place an OPTION SELL order — margin blocked correctly, unaffected by this session's changes.
- [ ] Place a FUTURES order (BUY and SELL) — margin blocked correctly, no cash wallet debit.
- [ ] Full `pytest tests/` run — all tests green (355 as of this session).
- [ ] `python -c "import app"` — no import errors, app boots cleanly.

---

## Notes for whoever runs this

- Tickets 14/15 both touch the `orders` and `order_book` tables — after any test above, spot-check that both tables' `status`/`price`/`quantity` values agree with each other (they should always move together; a mismatch is itself a bug worth reporting).
- Two Postgres schema migrations were applied earlier this session (`PENDING_TRIGGER` enum value on both status enums, and a new `master_account_order_entitlements` table for future use) — no action needed, already live.
- Ticket 12 (`master_account_order_entitlements`) has no live callers yet — nothing to test there until live Shoonya order routing is built (see `Part_B_Deferred_Tickets.md`).
