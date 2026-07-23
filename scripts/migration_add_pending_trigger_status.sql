-- Ticket 14: STOP/STOP-LIMIT orders don't actually trigger
--
-- Adds a new enum value 'PENDING_TRIGGER' to both order-status enums, used to
-- mark a STOP/STOPLIMIT order as "resting, not yet matchable" until its
-- trigger_price is crossed by a real trade price. Additive only - existing
-- rows/values are untouched, and this is safe to run against a live DB with
-- no downtime (Postgres ALTER TYPE ... ADD VALUE does not take a table lock).
--
-- order_status_type backs orders.status (queries/orders.yaml).
-- order_status      backs order_book.status (queries/portfolio.yaml).
ALTER TYPE order_status_type ADD VALUE IF NOT EXISTS 'PENDING_TRIGGER';
ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'PENDING_TRIGGER';
