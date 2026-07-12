# Part B — Deferred Tickets (blocked on company registration / nodal account)

These items from `Razorpay_Shoonya_LiveMoney_Readiness_Plan.md` are **not being built now**. Keep this doc for when the company registration and Razorpay nodal/escrow account are in place.

---

## Ticket 8 — Withdrawal / payout flow

**Problem statement:** A `WITHDRAWAL` enum value exists in `RazorPayPersistence.py:154`, but no endpoint or service actually processes a withdrawal request end-to-end.

**Why it's blocked:** The real money-movement leg (RazorpayX Payouts API, or any bank payout API) requires a registered business entity and a business bank account with Razorpay. Without that, there is no live account to push money out of.

**What could be half-built without a nodal account (not done yet, noted for later):**
- Withdrawal request endpoint (user requests a withdrawal amount).
- Atomic wallet debit-with-lock for the requested amount (reuse the same `debitWalletIfSufficient` pattern used for orders).
- Ledger entry with a `PENDING_PAYOUT` status.
- Idempotency key so a retried request doesn't double-debit.

**What cannot be built until registration + nodal account exist:**
- The actual outbound transfer call (RazorpayX Payouts API or equivalent).
- Payout status webhook handling (processed / failed / reversed).
- Reconciliation between "we debited the wallet" and "money actually left the account."

**Do not** build the internal half in isolation and wire it to a stubbed/fake payout call — that would let a user's wallet be debited with no real money movement and no way to reverse it safely. Build ticket 8 as one unit once the payout API is available.

---

## Ticket 9 — Nodal-account ↔ wallet-ledger reconciliation job

**Problem statement:** Pooled customer money legally must sit in a nodal/escrow account, and the internal wallet ledger's total must be reconciled against that account's real balance on a schedule, so a shortfall or discrepancy is caught immediately rather than discovered during an audit.

**Why it's blocked:** There is no nodal account yet. There is nothing to reconcile against. Building a reconciliation job now would either reconcile against nothing (dead code) or reconcile against the wrong account (the master Shoonya trading account, which is not the nodal account and conflating the two would hide the exact risk this ticket exists to catch).

**Do this only after:** company registration + nodal/escrow account is opened and Razorpay is switched to live mode with pooled settlements flowing into that account.

---

## Ticket 10 — Settlement-timing policy decision

**Problem statement:** Decide and enforce a policy for how quickly customer funds move from "received into nodal account" to "usable for trading" and back — this affects both compliance exposure and user experience.

**Why it's blocked:** This is a policy decision, not an engineering task, and it's about real settlement cycles that don't exist yet (no live Razorpay account, no nodal account, no live NSE/SEBI settlement flow). There's nothing to decide against or enforce today — this isn't an engineering blocker so much as "not yet applicable."

**Revisit when:** the nodal account is live and real T+1/T+0 settlement behavior from Razorpay/NSE is known.

---

## Ticket 11 — Master-account float-management check (Shoonya)

**Problem statement:** Before the master Shoonya account is used to route real customer orders, the platform needs to check the account's available funds/margin before/while placing an order, so a pooled order isn't rejected by the broker (or worse, partially filled in a way that desyncs internal positions) because the master account ran out of float.

**Why this is bigger than originally scoped, and deferred:** A codebase survey (2026-07-13) found that **Shoonya order routing does not exist at all yet**. `service/brokerAdapters/shoonya_adapter.py` only converts an `OrderCreate` into a Shoonya-shaped payload — it is never called anywhere, and no code in the repo calls Shoonya's `place_order`, `get_limits`, or any funds/margin endpoint. Today, all trading is peer-to-peer internal matching; the master Shoonya account is not touched by a single live order. `marketengine/ShoonyaConnection.py` only wraps OAuth/login and market-data (quotes/candles).

Because of that, ticket 11 as originally described ("add a float check on top of order routing") isn't a small addition — it requires first building live order placement against Shoonya from scratch. That is a much larger, higher-risk piece of work (first time real orders touch a live broker: needs handling for order rejects, partial fills, broker downtime/timeouts, symbol/lot mapping correctness, and its own thorough corner-case test pass) and deserves to be scoped and reviewed as its own project, not bundled in as a side effect of a "float check" ticket.

**Revisit when:** ready to scope "build live Shoonya order routing for the master account" as its own ticket. Ticket 11 (float/funds check before placing) should be built as part of that same effort, not before it — a funds check is only meaningful once there's a real place-order call to check funds against.

---

## Ticket 13 — Two-way reconciliation: internal ledger vs Shoonya master account

**Problem statement:** Periodically compare the internal per-user position/ledger totals against the Shoonya master account's actual holdings, to catch any drift between "what our DB thinks is open" and "what the broker actually holds."

**Why it's blocked:** Same root cause as ticket 11 — there is nothing on the Shoonya side to reconcile against yet, since no order is ever placed there. Building a reconciliation job today would have nothing real to compare, only the internal ledger against itself.

**Note on existing job infrastructure (useful once unblocked):** `scheduler/marketPriceSchedular.py` already establishes the pattern for scheduled background jobs in this repo (APScheduler `BackgroundScheduler`, a class-level guard against duplicate schedulers, started at app startup). A reconciliation job should follow that same pattern rather than introducing Celery or a new scheduling framework.

**Revisit when:** ticket 11's live Shoonya order routing exists and the master account actually holds real positions to reconcile against.

---

## Trigger to revisit this document

Re-open tickets 8, 9, 10 as soon as:
1. The company is legally registered.
2. A nodal/escrow bank account is opened and linked to a live-mode Razorpay account.
3. Razorpay support confirms pooled settlement account details for reconciliation (ticket 9) and settlement cycle timing (ticket 10).

Re-open tickets 11 and 13 as soon as:
1. Live Shoonya order routing for the master account is scoped and built as its own project (this is the actual prerequisite, independent of company registration/nodal account — it can happen in parallel with 8/9/10's blockers clearing).

At that point, ticket 8's internal half (already built alongside tickets 12/14/15) just needs the real payout API wired to its existing debit/ledger scaffolding, and tickets 11/13 slot in on top of the new order-routing code.
