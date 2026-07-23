-- Live Shoonya F&O order placement: tracks the real broker order number
-- (norenordno) on the orders table, so the order-update feed
-- (service/orderUpdateService.py) can look up which internal order a real
-- fill/reject/cancel notification belongs to. order_book already has this
-- column from earlier work; orders does not yet.
--
-- Additive only - no existing data touched, no table lock beyond the brief
-- DDL lock every ALTER TABLE ADD COLUMN takes (this column has no default
-- to backfill, so it's a metadata-only change on Postgres 11+, near-instant
-- even on a large table).
ALTER TABLE orders ADD COLUMN IF NOT EXISTS broker_order_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_orders_broker_order_id ON orders (broker_order_id);
