-- Margin Engine schema. Applied manually against Supabase Postgres (this repo
-- has no migration tooling - see FnO_Margin_Engine_Design.md section 10).
-- Safe to re-run: every statement is idempotent (IF NOT EXISTS / guarded ADD COLUMN).

CREATE TABLE IF NOT EXISTS order_margin_blocks (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id),
    user_id BIGINT NOT NULL,
    tsym VARCHAR(50) NOT NULL,
    exchange VARCHAR(10) NOT NULL,
    contract_type VARCHAR(10) NOT NULL CHECK (contract_type IN ('OPTION','FUTURES')),
    side VARCHAR(4) NOT NULL CHECK (side IN ('BUY','SELL')),
    qty INT NOT NULL CHECK (qty > 0),
    lot_size INT NOT NULL,
    blocked_amount NUMERIC(18,2) NOT NULL CHECK (blocked_amount >= 0),
    premium_component NUMERIC(18,2),
    notional_component NUMERIC(18,2) NOT NULL,
    reference_price NUMERIC(18,2) NOT NULL,
    reference_source VARCHAR(30) NOT NULL,
    reference_source_tier SMALLINT NOT NULL CHECK (reference_source_tier BETWEEN 1 AND 4),
    price_source_multiplier_used NUMERIC(6,4) NOT NULL DEFAULT 1.0,
    moneyness_multiplier_used NUMERIC(6,4),
    expiry_multiplier_used NUMERIC(6,4) NOT NULL DEFAULT 1.0,
    notional_pct_or_span_pct NUMERIC(6,4) NOT NULL,
    status VARCHAR(12) NOT NULL CHECK (status IN ('ACTIVE','RELEASED','PARTIAL')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ,
    release_reason VARCHAR(20) CHECK (release_reason IN ('CANCEL','FILL','EXPIRY','AMEND'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_order_margin_blocks_active
    ON order_margin_blocks(order_id) WHERE status = 'ACTIVE';

ALTER TABLE positions ADD COLUMN IF NOT EXISTS blocked_margin NUMERIC(18,2) NOT NULL DEFAULT 0;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS contract_type VARCHAR(10) NOT NULL DEFAULT 'OPTION' CHECK (contract_type IN ('OPTION','FUTURES'));

ALTER TABLE wallets ADD COLUMN IF NOT EXISTS blocked_margin NUMERIC(18,2) NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS margin_config (
    id BIGSERIAL PRIMARY KEY,
    contract_type VARCHAR(10) NOT NULL CHECK (contract_type IN ('OPTION','FUTURES')),
    underlying VARCHAR(30),
    notional_pct NUMERIC(6,4),
    span_pct NUMERIC(6,4),
    near_expiry_multiplier NUMERIC(6,4) NOT NULL DEFAULT 1.0,
    far_expiry_multiplier NUMERIC(6,4) NOT NULL DEFAULT 1.0,
    near_expiry_days SMALLINT NOT NULL DEFAULT 2,
    far_expiry_days SMALLINT NOT NULL DEFAULT 30,
    moneyness_itm_multiplier NUMERIC(6,4) NOT NULL DEFAULT 1.2,
    moneyness_atm_multiplier NUMERIC(6,4) NOT NULL DEFAULT 1.0,
    moneyness_otm_multiplier NUMERIC(6,4) NOT NULL DEFAULT 0.9,
    session_gap_multiplier NUMERIC(6,4) NOT NULL DEFAULT 1.1,
    price_source_tier1_multiplier NUMERIC(6,4) NOT NULL DEFAULT 1.0,
    price_source_tier2_multiplier NUMERIC(6,4) NOT NULL DEFAULT 1.0,
    price_source_tier3_multiplier NUMERIC(6,4) NOT NULL DEFAULT 1.1,
    price_source_tier4_multiplier NUMERIC(6,4) NOT NULL DEFAULT 1.2,
    tier3_verification_band_pct NUMERIC(6,4) NOT NULL DEFAULT 0.20,
    effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active BOOLEAN NOT NULL DEFAULT true
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_margin_config_active
    ON margin_config(contract_type, COALESCE(underlying, '')) WHERE is_active = true;

CREATE TABLE IF NOT EXISTS margin_block_audit (
    id BIGSERIAL PRIMARY KEY,
    order_margin_block_id BIGINT REFERENCES order_margin_blocks(id),
    user_id BIGINT NOT NULL,
    event_type VARCHAR(20) NOT NULL CHECK (event_type IN ('BLOCK','RELEASE','RECONCILE','FLAG')),
    amount_delta NUMERIC(18,2) NOT NULL,
    reason_code VARCHAR(30) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS peak_margin_snapshot (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    snapshot_date DATE NOT NULL,
    total_blocked_margin NUMERIC(18,2) NOT NULL,
    available_balance NUMERIC(18,2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_peak_margin_snapshot ON peak_margin_snapshot(user_id, snapshot_date);

CREATE TABLE IF NOT EXISTS margin_review_flags (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    flag_date DATE NOT NULL,
    flag_reason VARCHAR(30) NOT NULL CHECK (flag_reason IN ('MTM_BREACH','STALE_PRICE','MANUAL')),
    required_margin_recomputed NUMERIC(18,2) NOT NULL,
    available_balance_at_flag NUMERIC(18,2) NOT NULL,
    status VARCHAR(12) NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','REVIEWED','CLEARED')),
    reviewed_by BIGINT,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO margin_config (contract_type, underlying, notional_pct, span_pct)
SELECT 'OPTION', NULL, 0.15, NULL
WHERE NOT EXISTS (SELECT 1 FROM margin_config WHERE contract_type = 'OPTION' AND underlying IS NULL);

INSERT INTO margin_config (contract_type, underlying, notional_pct, span_pct)
SELECT 'FUTURES', NULL, NULL, 0.12
WHERE NOT EXISTS (SELECT 1 FROM margin_config WHERE contract_type = 'FUTURES' AND underlying IS NULL);
