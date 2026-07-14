-- Idempotency guard for real Shoonya order-update Fill events. Shoonya's
-- own order-update feed can redeliver a Fill message (e.g. after a
-- reconnect); each Fill carries a unique `flid` (Fill ID) specifically for
-- deduplication. This table records every flid we've already settled -
-- OrderUpdateService inserts a row before settling a fill, and a duplicate
-- insert (same flid) fails on the PRIMARY KEY, which is used to skip
-- re-settling it instead of double-crediting a position/wallet.
CREATE TABLE IF NOT EXISTS processed_broker_fills (
    flid VARCHAR(64) PRIMARY KEY,
    order_id INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_processed_broker_fills_order_id ON processed_broker_fills (order_id);
