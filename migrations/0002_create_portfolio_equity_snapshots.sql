-- Historical NLV/P&L snapshots, one row per user per bucket per capture.
-- 'bucket' is one of 'ALL', 'STOCKS', 'FNO' (see utils.assetBuckets) and
-- powers the per-tab performance graphs on the dashboard.
CREATE TABLE portfolio_equity_snapshots (
    snapshot_id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(user_id),
    bucket VARCHAR(20) NOT NULL,
    net_value NUMERIC NOT NULL,
    total_unrealized_pnl NUMERIC NOT NULL,
    buying_power NUMERIC NOT NULL,
    captured_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_equity_snapshots_user_bucket_time
    ON portfolio_equity_snapshots (user_id, bucket, captured_at);
