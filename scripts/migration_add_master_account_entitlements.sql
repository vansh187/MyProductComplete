-- Ticket 12: per-user ledger as sole source of truth for pooled-position
-- entitlement (schema-only groundwork).
--
-- This table has no live writers/readers yet - there is nothing to link it
-- to until ticket 11 (live Shoonya order routing for the master account) is
-- built as its own project (see Part_B_Deferred_Tickets.md). It exists now
-- so that whenever a real Shoonya order is placed on the master account,
-- there is already a place to record which internal user(s) - and what
-- quantity each - that pooled position actually belongs to. broker_order_id
-- is nullable specifically because it cannot be populated until that live
-- routing exists.
CREATE TABLE IF NOT EXISTS master_account_order_entitlements (
    id BIGSERIAL PRIMARY KEY,
    broker_order_id VARCHAR(64),
    internal_order_id BIGINT NOT NULL REFERENCES orders(id),
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    symbol VARCHAR(50) NOT NULL,
    side VARCHAR(4) NOT NULL,
    quantity INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_master_entitlements_broker_order_id
    ON master_account_order_entitlements (broker_order_id);

CREATE INDEX IF NOT EXISTS idx_master_entitlements_internal_order_id
    ON master_account_order_entitlements (internal_order_id);
