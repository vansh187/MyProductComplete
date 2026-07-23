-- Adds asset classification to holdings so the dashboard can split
-- Stocks vs F&O (Futures + Options). Value is derived from the symbol
-- pattern at insert time (see portfolioPersistence._infer_asset_type):
-- 'EQUITY', 'FUTURE', or 'OPTION'.
ALTER TABLE holdings ADD COLUMN asset_type VARCHAR(20) NOT NULL DEFAULT 'EQUITY';
