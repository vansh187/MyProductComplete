-- F&O position book, one row per (user, symbol, product_type). Written by
-- service/positionsService.py::applyFill whenever an NFO/NCDEX/MCXSX order
-- matches. Uses CREATE TABLE IF NOT EXISTS since this table already exists
-- by hand in some environments (e.g. this project's original dev database,
-- predating this migration) - 0005 brings those environments up to the
-- same product_type-aware primary key defined here.
CREATE TABLE IF NOT EXISTS positions (
    user_id INT NOT NULL REFERENCES users(user_id),
    tsym VARCHAR(40) NOT NULL,
    broker VARCHAR(40),
    token VARCHAR(40),
    exchange VARCHAR(20),
    underlying VARCHAR(40),
    expiry DATE,
    strike NUMERIC,
    option_type VARCHAR(4),
    lot_size INT,
    product_type VARCHAR(20) NOT NULL,
    source VARCHAR(20),
    netqty INT NOT NULL DEFAULT 0,
    netavgprc NUMERIC NOT NULL DEFAULT 0,
    buyqty INT NOT NULL DEFAULT 0,
    sellqty INT NOT NULL DEFAULT 0,
    buyavgprc NUMERIC NOT NULL DEFAULT 0,
    sellavgprc NUMERIC NOT NULL DEFAULT 0,
    realized_pnl NUMERIC NOT NULL DEFAULT 0,
    status VARCHAR(10) NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    blocked_margin NUMERIC NOT NULL DEFAULT 0,
    contract_type VARCHAR(10) NOT NULL DEFAULT 'OPTION'
        CHECK (contract_type IN ('OPTION', 'FUTURES')),
    CONSTRAINT pk_positions PRIMARY KEY (user_id, tsym, product_type)
);

CREATE INDEX IF NOT EXISTS idx_positions_user ON positions (user_id);
