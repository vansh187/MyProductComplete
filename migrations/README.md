# Migrations

Plain numbered SQL files, applied by hand against the Postgres instance (no Alembic/framework). Run them in order, once, then update the schema section of `systemDesign.md`.

- `0001_add_asset_type_to_holdings.sql`
- `0002_create_portfolio_equity_snapshots.sql`
- `0003_backfill_holdings_asset_type.sql` — safe to re-run; recomputes asset_type for any rows inserted before 0001
- `0004_create_positions_table.sql` — F&O position book; no-op if the table already exists
- `0005_add_product_type_to_positions_pk.sql` — adds product_type to the positions primary key; only needed on environments that already had `positions` before 0004
