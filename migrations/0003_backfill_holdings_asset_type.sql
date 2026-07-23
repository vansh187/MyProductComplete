-- Migration 0001 added asset_type with DEFAULT 'EQUITY', which every
-- pre-existing row (including real option/future holdings) silently
-- received. This re-derives the correct value from the symbol pattern,
-- mirroring database.portfolioPersistence.portfolioPersistence._infer_asset_type.
-- Safe to re-run: purely recomputes asset_type, touches no other column.
UPDATE holdings
SET asset_type = CASE
    WHEN RIGHT(symbol, 2) IN ('CE', 'PE')
         AND LENGTH(symbol) > 2
         AND SUBSTRING(symbol FROM LENGTH(symbol) - 2 FOR 1) ~ '[0-9]'
        THEN 'OPTION'
    WHEN RIGHT(symbol, 3) = 'FUT' THEN 'FUTURE'
    ELSE 'EQUITY'
END;
