-- Brings environments that created `positions` before 0004 (product_type
-- was nullable and not part of the key) up to the same shape: MIS and NRML
-- positions on the same symbol must net separately, not blend into one row.
-- Safe to run only after confirming no existing row has a NULL product_type.
ALTER TABLE positions ALTER COLUMN product_type SET NOT NULL;

ALTER TABLE positions DROP CONSTRAINT IF EXISTS pk_positions;
ALTER TABLE positions ADD CONSTRAINT pk_positions PRIMARY KEY (user_id, tsym, product_type);
