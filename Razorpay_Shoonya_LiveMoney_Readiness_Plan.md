# Razorpay + Shoonya Live-Money Readiness Plan

**Companion to:** `OMS_Production_Readiness_Plan.md` (portal-side order-engine fixes). This document covers the **money layer** — going from Razorpay test mode + Shoonya API keys-in-hand, to real money entering the platform, sitting somewhere legally, and backing a real order placed at Shoonya, without the two sides of that flow ever disagreeing.

---

## 0. The core architectural decision (confirmed, phase 1)

**Decision made:** Phase 1 = launch on top of Shoonya using the **single company master account** that's already integrated (`marketengine/ShoonyaConnection.py:72-75` logs in with one `SHOONYA_USER_ID`/`SHOONYA_PASSWORD` from `.env` — not a per-user broker login). All customer orders route through this one Shoonya account, funded by the pooled Razorpay wallet. Full regulatory build-out (nodal account, any additional registrations the pooled-fund/order-routing model requires) is intentionally deferred to phase 2, once the company entity and legal registrations are further along.

**What this means technically — this is the "Model B" shape from the earlier draft of this doc:** the platform itself is the only Shoonya client; individual users never touch Shoonya directly. Our wallet ledger is the *only* record of who owns what — Shoonya has no concept of your users at all, it only sees one account's orders and positions. That has a specific consequence for OMS design:

**Purpose of calling this out explicitly:** because Shoonya only sees one net position per instrument (the company account's), the OMS itself must be the source of truth for **per-user P&L, per-user margin, and per-user entitlement to that shared position** — Shoonya's own risk/margin numbers describe the company account in aggregate, not any individual user. Every fix below is scoped to that reality: get phase-1 launch safe and correct under a single pooled account, and explicitly defer anything that only matters once users have their own broker-level accounts (that's phase 2 work, tracked but not blocking).

This is a business/compliance decision you've made, not something I'm validating from a legal standpoint — flagging only so the engineering scope below matches the real regulatory posture you're operating under today, and so phase 2 has a clear list of what to build once the legal structure catches up.

---

## 1. Fix what's already broken in the Razorpay code (do this before touching Shoonya)

### 1.1 Webhook secret is hardcoded, not read from environment
**Problem:** `service/razorpay/RazorPayMangerService.py:116` has:
```python
webhook_secret = "WEBHOOK_9897"#os.getenv("RAZORPAY_WEBHOOK_SECRET")""
```
The real `os.getenv` call is commented out — every webhook is verified against a hardcoded literal, and test-mode and live-mode webhook secrets are **always different values** issued by Razorpay per mode. This will silently break (or worse, stay "working" against a guessable constant) the moment you switch to live mode.
**Purpose of fix:** Read `RAZORPAY_WEBHOOK_SECRET` from environment (already imported via `dotenv`), fail startup loudly if unset, and keep separate `.env` values for test vs live so switching modes is a config change, not a code change.

### 1.2 Client-side payment verification never checks the signature the client actually sent
**Problem:** `verify_payment_signature()` (`RazorPayMangerService.py:65-107`, called from `POST /v1/VerifyFundPayements`) receives `razorpay_signature` from the frontend but never uses it. It **recomputes** the HMAC itself (`valid_signature`) and passes that self-computed value back into the Razorpay SDK's own verification call. This means the endpoint always reports "Payment Verified" regardless of what the client actually sent — the signature check is checking itself against itself. It happens to be low-risk *today* only because this endpoint's DB-write path is commented out (`RazorPayMangerService.py:91-103`) — the actual wallet credit only happens through the webhook path, which is correctly implemented (`verifyPaymentSignatureWebHook` → `invokeCallToDatabase`, with idempotency at `RazorPayMangerService.py:186-198`).
**Purpose of fix:** Either fix `verify_payment_signature` to pass the client-supplied `razorpay_signature` into the SDK check (not a self-computed one), or remove this endpoint entirely and rely solely on the webhook as the single source of truth for payment confirmation (recommended — client-side "verify" calls are inherently spoofable and should never be trusted to update money state; the webhook, signed by Razorpay's server, is the only trustworthy signal). Decide and document which one is authoritative so nobody re-enables the dead DB-write code in that function later.

### 1.3 Duplicate, mismatched webhook-verification method
**Problem:** `Razorypay.py:52-60` (`verifyPaymentSignatureWebhook`, lowercase "hook") calls `RazorPayManagerService.verify_webhook_signature()` with keyword arguments (`payload=`, `razorpay_signature=`, `userId=`) that don't match that method's actual signature (`raw_body`, `webhook_signature`) — this would raise a `TypeError` if ever called. It appears to be dead/unused, superseded by `verifyPaymentSignatureWebHook` (capital "Hook", `Razorypay.py:64-78`), which is the one actually wired to the webhook route.
**Purpose of fix:** Delete the dead duplicate before it confuses whoever wires live-mode changes into this file next. Two near-identically-named methods with different bugs is exactly how a wrong one gets called during a rushed live-mode fix.

### 1.4 No withdrawal / payout flow exists
**Problem:** A `WITHDRAWAL` transaction-type constant exists (`database/razorpaypersistence/RazorPayPersistence.py:154`) but no endpoint, service, or persistence method actually processes a withdrawal. Users can fund a wallet but have no way to get money back out.
**Purpose of fix:** Before real money is involved, a withdrawal path is not optional — regulators and users both expect it, and "money went in, only support tickets get it out" is not an acceptable live-money design. Needs: a payout-initiation endpoint, integration with Razorpay Payouts (or RazorpayX, which is the product that actually supports payouts *from* a nodal account to a beneficiary bank account), and the same idempotency discipline used for deposits.

---

## 2. Nodal account & wallet-ledger reconciliation (new — doesn't exist today)

### 2.1 No reconciliation between `wallet_ledger` and actual nodal account balance
**Problem:** The wallet ledger is purely a database table credited by webhook events. Nothing ever checks that the sum of `wallet_ledger` balances actually equals what's really sitting in the nodal bank account. A missed webhook retry, a Razorpay-side refund not reflected in our webhook handling, or a manual bank adjustment would silently desync the two — invisibly, since nothing compares them.
**Purpose of fix:** A scheduled job (daily, at minimum) that pulls Razorpay settlement/transaction reports (or the nodal bank's statement, depending on which entity — you or your banking partner — exposes it) and diffs the total against `SUM(wallet_ledger.balance)`. Any mismatch must alert a human, not self-correct silently. This is the financial equivalent of the OMS's "broker-state reconciliation" gap (item 7 in the companion OMS plan) — same failure category, different pool of money.

### 2.2 "Payment captured" is treated as final; settlement timing isn't modeled
**Problem:** The webhook credits the wallet immediately on `payment.captured` (`api/VerifyFundTransaction.py:61-70`). Razorpay settles captured payments into your account (and, in a nodal-account setup, the nodal account) on a T+1/T+2 cycle depending on your arrangement — the money isn't actually *in* the nodal account the instant `payment.captured` fires.
**Purpose of fix:** Decide explicitly whether a user's wallet balance is allowed to be used for live-order margin before actual settlement completes (this is a real credit-risk decision — most platforms accept the T+1 float risk for a smoother UX, but it must be a conscious choice, documented, with a cap on unsettled-exposure per user, not an accident of the code crediting immediately).

### 2.3 No float-management layer for the single master Shoonya account
**Problem:** Under the phase-1 single-master-account model, no individual user ever funds Shoonya directly — every live order is really the *company's* order, sized and margined against user wallet balances internally. Nothing today tracks whether the master account itself holds enough real funds/margin at Shoonya to cover the **aggregate** exposure of every pooled user position at once.
**Purpose of fix:** Build a float-management check: before the master account submits a live order, confirm the master account's actual Shoonya balance/margin (via Shoonya's own funds API) can support the *sum* of all open pooled exposure, not just this one order. If the pool ever needs more float than the master account currently holds, that's a top-up-from-nodal-account event, not something that should be discovered by a broker rejection.

---

## 3. Shoonya live order-placement gating (single master account, phase 1)

### 3.1 OMS must be the sole source of truth for per-user entitlement to the pooled position
**Problem:** Shoonya only ever sees one net position per instrument (the master account's). It has no concept of which of your users contributed how much to that position, at what price, or with what P&L. If the OMS's internal per-user ledger and the master account's actual Shoonya position ever disagree, there is no broker-side record to fall back on — the OMS's ledger *is* the only record.
**Purpose of fix:** Every live order placed at Shoonya must be paired, atomically, with the per-user ledger entry that justifies it (which user, how much of the master position is theirs, at what price). This is a stricter version of the companion OMS plan's reconciliation requirement — here reconciliation isn't "compare two sources of truth," it's "make sure the one external source of truth (Shoonya's aggregate position) never drifts from the arithmetic sum of what the internal ledger claims," since there's no way to independently recover a user's entitlement from Shoonya's side if the internal ledger is wrong.

### 3.2 Order must not reach Shoonya unless the master account's own margin covers it
**Problem:** Our internal margin engine (solid per the companion plan) currently authorizes orders against **wallet/internal margin ledger** balances. Once orders route to the master Shoonya account, our engine's "sufficient margin" answer must match Shoonya's real answer for the master account's aggregate book, or an order we approve will get rejected at the broker.
**Purpose of fix:** Before submitting a live order, call Shoonya's own margin/limits API as the final gate for the master account (source of truth = broker, not our engine's estimate) — see also item 2.3's float check, which this depends on. Internal margin engine remains the correct tool for *per-user* gating; Shoonya's own check is the correct tool for whether the *master account* can actually place the order right now.

### 3.3 Order/position state must be reconciled two ways (simpler than a per-user-broker model, but still mandatory)
**Problem:** The companion OMS plan already calls for OMS-state ↔ broker-order-state reconciliation generally. Under the single-master-account model this collapses to: our internal per-user ledger (sum of all users' claimed exposure) vs. the master account's actual Shoonya position and funds.
**Purpose of fix:** A reconciliation job that pulls the master account's live Shoonya position + funds and diffs it against `SUM` of internal per-user ledger claims. Any drift must halt new live-order acceptance platform-wide (not per-user, since it's one shared account) until resolved — this is a stronger blast radius than a per-user broker model would have, which is exactly why this check matters more here, not less.

---

## 4. Test-mode → live-mode migration checklist

Concrete, mechanical items to close before flipping any switch:

1. Separate `.env` values (not just key/secret, but **webhook secret too**, which differs per mode) for Razorpay test vs live — confirm today's hardcoded `WEBHOOK_9897` (item 1.1) is fully removed first.
2. Confirm the nodal account is actually provisioned and linked to your Razorpay live account (this is a business/KYC process with Razorpay's banking partner, not a code change — flagging so it's on the critical path, since it can take longer than the engineering work).
3. Same separation for Shoonya: confirm whether the API keys you have are sandbox/UAT or production, and whether Shoonya provides a UAT environment at all for pre-live testing (if not, plan for a controlled, small-amount live pilot instead).
4. Add environment-based config validation at startup: refuse to boot in a "live" environment if any test-mode indicator (test key prefix, hardcoded secret, etc.) is detected.
5. Run the reconciliation jobs (item 2.1, item 3.2) in test mode against Razorpay's test webhooks and Shoonya's available sandbox first, so the reconciliation logic itself is proven before it's guarding real money.

---

## Suggested build order

1. Fix the three concrete Razorpay bugs (1.1–1.3) — cheap, high-risk-reduction, no design decisions required.
2. Build withdrawal flow (1.4) and wallet↔nodal reconciliation job (2.1–2.2) — mandatory regardless of Shoonya timing, since this is Razorpay's own condition for going live, independent of the single-master-account decision.
3. Build the master-account float-management check (2.3) and the per-user-ledger ↔ Shoonya-position pairing (3.1).
4. Wire live order submission through Shoonya's own margin check (3.2) and the two-way reconciliation (3.3) — only after 1–3 are done and tested in test mode end-to-end.

---

## 5. Phase 1 (launch) vs Phase 2 (post-registration) — line-wise

**Phase 1 — build now, needed for a safe first live release on the single master account:**

- Fix Razorpay webhook secret hardcoding, broken client-side signature check, dead duplicate method (1.1–1.3)
- Build withdrawal/payout flow (1.4) — Razorpay live activation and basic user trust both require this regardless of architecture
- Build wallet-ledger ↔ nodal-account reconciliation job (2.1) and decide the settlement-timing policy explicitly (2.2) — this is Razorpay's own live-mode condition, not optional
- Build master-account float-management check: master Shoonya account must hold enough real margin/funds to cover the sum of all pooled user exposure before any order is submitted (2.3)
- Make the internal per-user ledger the atomic, paired source of truth for "who owns what share of the pooled Shoonya position" (3.1) — since Shoonya itself has no concept of your individual users
- Gate every live order through Shoonya's own margin/limits API as the final check for the master account (3.2)
- Build the two-way reconciliation job: internal ledger sum vs. master account's real Shoonya position/funds, with a platform-wide halt on drift (3.3)
- From the companion OMS plan: the P0 correctness fixes (wallet race condition, non-blocking wallet debit, MARKET-order crash) — these apply just as much to a pooled-account live order as to the existing paper-trading flow, arguably more, since a bug there now touches real money
- From the companion OMS plan: fix or explicitly disable STOP/STOP-LIMIT orders before going live, since they currently execute immediately rather than triggering — worse when the "immediate" execution is a real order at a real broker
- Test-mode → live-mode config checklist (section 4) — mechanical but blocking

**Phase 2 — defer until company entity/registrations are further along; don't build yet:**

- Per-user individual Shoonya accounts / per-user OAuth login to Shoonya (the "Model A" shape) — only relevant once users are meant to hold their own broker-level accounts instead of trading against the pooled master account
- Per-user funds-transfer-to-broker ledger and associated reconciliation (the version of 2.3/3.1 that assumes per-user accounts) — superseded by the simpler master-account float check above until then
- Formal nodal-account-to-individual-broker-account fund movement automation — not needed while the master account is the only Shoonya client
- GTT / bracket / cover / OCO order types (companion OMS plan item 6) — no evidence of user demand yet, and adds real complexity against a live broker; revisit post-launch
- Order modify/amend endpoint (companion OMS plan item 5) — genuinely useful, but cancel-and-replace against the pooled account is a workable stopgap for phase 1; promote to phase 1 only if early users complain
- Broader broker-adapter abstraction for multiple brokers (Zerodha adapter already exists unused) — stay Shoonya-only until phase 1 is proven stable

**Still an open question, worth deciding explicitly rather than defaulting:** whether phase 1 needs a hard per-user exposure cap (e.g., no single user can be allowed to represent more than X% of the master account's total pooled position), since one user's outsized position now has blast radius onto every other pooled user sharing the same broker account — a risk that doesn't exist once phase 2 moves to per-user accounts.
