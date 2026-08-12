-- Mutual Funds module: scheme catalog, locally-owned NAV history, computed
-- returns, and daily LLM-curated Explore/Collections picks. Apply manually
-- via psql against the Supabase/Postgres instance (this repo has no
-- migration runner - see scripts/migration_add_master_account_entitlements.sql
-- for the same pattern).

CREATE TABLE IF NOT EXISTS mf_schemes (
    scheme_code            BIGINT PRIMARY KEY,
    scheme_name             TEXT NOT NULL,
    isin_growth              TEXT,
    isin_div_reinvestment    TEXT,
    fund_house               TEXT,
    scheme_type              TEXT,
    scheme_category          TEXT,
    is_active                BOOLEAN,        -- NULL = not checked yet, TRUE = live, FALSE = stale/dormant
    is_backfilled             BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_mf_schemes_name_trgm
    ON mf_schemes USING GIN (scheme_name gin_trgm_ops) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_mf_schemes_category
    ON mf_schemes (scheme_category) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_mf_schemes_fund_house
    ON mf_schemes (fund_house) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_mf_schemes_pending_backfill
    ON mf_schemes (scheme_code) WHERE is_active IS NULL;

CREATE TABLE IF NOT EXISTS mf_nav_history (
    scheme_code   BIGINT NOT NULL REFERENCES mf_schemes(scheme_code),
    nav_date      DATE NOT NULL,
    nav           NUMERIC NOT NULL,
    PRIMARY KEY (scheme_code, nav_date)
);

-- No separate (scheme_code, nav_date DESC) index here on purpose - the
-- primary key above already covers every query against this table
-- (nav_history_get_series ORDER BY nav_date ASC; Postgres can scan a btree
-- index backwards just as efficiently for the reverse order). An earlier
-- version of this migration added one preemptively; it was never used by
-- any query and cost ~430MB on a table that's already the dominant share
-- of database size (8.5M rows across ~8.6k active schemes) - dropped after
-- discovering it via a Supabase storage overflow warning.

CREATE TABLE IF NOT EXISTS mf_scheme_returns (
    scheme_code       BIGINT PRIMARY KEY REFERENCES mf_schemes(scheme_code),
    return_1m         NUMERIC,
    return_6m         NUMERIC,
    return_1y         NUMERIC,
    return_3y         NUMERIC,   -- CAGR
    return_5y         NUMERIC,   -- CAGR
    day_change_pct    NUMERIC,
    latest_nav        NUMERIC,
    last_computed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mf_returns_3y ON mf_scheme_returns (return_3y DESC);

CREATE TABLE IF NOT EXISTS mf_curated_picks (
    collection_key   TEXT NOT NULL,     -- 'popular' or an MFCollectionsCatalog key
    scheme_code      BIGINT NOT NULL REFERENCES mf_schemes(scheme_code),
    rank             INT NOT NULL,
    blurb            TEXT,
    curated_by       TEXT NOT NULL,     -- 'gemini' | 'groq' | 'fallback_ranked'
    curated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (collection_key, scheme_code)
);

CREATE INDEX IF NOT EXISTS idx_mf_curated_picks_key_rank
    ON mf_curated_picks (collection_key, rank);
