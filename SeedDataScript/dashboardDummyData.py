"""
Seeds a dedicated demo user with realistic equity holdings, F&O positions,
and ~14 days of equity-curve history, so the frontend team can exercise
every dashboard endpoint (getDashboardSummary, getAssetClassSummary,
getPortfolioEquityCurve, getPortfolioForLoggedInUser,
getPortfolioOfLoggedInUserWithProfitLoss, getFnoPositionsForLoggedInUser)
against real data instead of mocks.

Safe to re-run: it wipes only this demo user's own rows before reseeding.

Usage:
    python SeedDataScript/dashboardDummyData.py
"""
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import psycopg2.extras
from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.password_hasher import hash_password

DEMO_EMAIL = "dashboard.demo@example.com"
DEMO_PASSWORD = "Demo@12345"

HOLDINGS = [
    # symbol, quantity, avg_price, current_price
    ("RELIANCE", 10, Decimal("2450.00"), Decimal("2510.30")),
    ("TCS", 5, Decimal("3800.00"), Decimal("3854.50")),
    ("INFY", 20, Decimal("1450.00"), Decimal("1422.75")),
]

FNO_POSITIONS = [
    # tsym, underlying, expiry, strike, option_type, contract_type, lot_size,
    # product_type, netqty, netavgprc, buyqty, sellqty, buyavgprc, sellavgprc,
    # realized_pnl, status
    ("NIFTY28JUL2624500CE", "NIFTY", "2026-07-28", 24500, "CE", "OPTION", 75,
     "NRML", 75, Decimal("182.40"), 75, 0, Decimal("182.40"), Decimal("0"),
     Decimal("0"), "OPEN"),
    ("NIFTY28JUL2624800PE", "NIFTY", "2026-07-28", 24800, "PE", "OPTION", 75,
     "MIS", -75, Decimal("95.20"), 0, 75, Decimal("0"), Decimal("95.20"),
     Decimal("0"), "OPEN"),
    ("NIFTY21JUL2624200CE", "NIFTY", "2026-07-21", 24200, "CE", "OPTION", 75,
     "NRML", 0, Decimal("0"), 75, 75, Decimal("150.00"), Decimal("210.00"),
     Decimal("4500.00"), "CLOSED"),
]

STARTING_BUYING_POWER = Decimal("100000.00")


def get_connection():
    return PostgresConnectionFactory.create_connection()


def upsert_demo_user(cur):
    cur.execute("SELECT user_id FROM users WHERE email = %s", (DEMO_EMAIL,))
    row = cur.fetchone()
    if row:
        print(f"Reusing existing demo user_id={row['user_id']}")
        return row["user_id"]

    cur.execute(
        """INSERT INTO users (first_name, last_name, email, password, phone_number, created_at)
           VALUES (%s, %s, %s, %s, %s, NOW()) RETURNING user_id""",
        ("Dashboard", "Demo", DEMO_EMAIL, hash_password(DEMO_PASSWORD), "9999999999")
    )
    user_id = cur.fetchone()["user_id"]
    print(f"Created demo user_id={user_id}")
    return user_id


def reset_demo_data(cur, user_id):
    cur.execute("DELETE FROM portfolio_equity_snapshots WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM positions WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM holdings WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM wallets WHERE user_id = %s", (user_id,))


def seed_wallet(cur, user_id):
    cur.execute(
        "INSERT INTO wallets (user_id, balance, blocked_margin, updated_at) VALUES (%s, %s, 0, NOW())",
        (user_id, STARTING_BUYING_POWER)
    )


def seed_holdings_and_prices(cur, user_id):
    for symbol, qty, avg_price, current_price in HOLDINGS:
        cur.execute(
            """INSERT INTO holdings (user_id, symbol, quantity, avg_price, asset_type, updated_at)
               VALUES (%s, %s, %s, %s, 'EQUITY', NOW())""",
            (user_id, symbol, qty, avg_price)
        )
        cur.execute(
            """INSERT INTO market_prices (symbol, current_price, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (symbol) DO UPDATE SET current_price = EXCLUDED.current_price, updated_at = NOW()""",
            (symbol, current_price)
        )


def seed_fno_positions(cur, user_id):
    for (tsym, underlying, expiry, strike, option_type, contract_type, lot_size,
         product_type, netqty, netavgprc, buyqty, sellqty, buyavgprc, sellavgprc,
         realized_pnl, status) in FNO_POSITIONS:
        cur.execute(
            """INSERT INTO positions (
                 user_id, tsym, broker, token, exchange, underlying, expiry, strike,
                 option_type, lot_size, product_type, source, netqty, netavgprc,
                 buyqty, sellqty, buyavgprc, sellavgprc, realized_pnl, status, contract_type
               ) VALUES (
                 %s, %s, NULL, NULL, 'NFO', %s, %s, %s,
                 %s, %s, %s, 'DEMO_SEED', %s, %s,
                 %s, %s, %s, %s, %s, %s, %s
               )""",
            (user_id, tsym, underlying, expiry, strike,
             option_type, lot_size, product_type, netqty, netavgprc,
             buyqty, sellqty, buyavgprc, sellavgprc, realized_pnl, status, contract_type)
        )


def seed_equity_curve(cur, user_id):
    """14 days of history per bucket, gently trending up, ending 'now' -
    enough for every range tab (1D/1W/1M) to show a real line."""
    equity_value = sum(qty * price for _, qty, _, price in HOLDINGS)  # current mark-to-market
    equity_unrealized = sum(qty * (price - avg) for _, qty, avg, price in HOLDINGS)
    fno_realized = sum(p[14] for p in FNO_POSITIONS)  # realized_pnl column

    days = 14
    for i in range(days + 1):
        captured_at = datetime.utcnow() - timedelta(days=(days - i))
        # Ramp buying power/equity/fno smoothly from ~92% up to 100% of today's values
        progress = Decimal(i) / Decimal(days)
        day_equity_value = equity_value * (Decimal("0.92") + Decimal("0.08") * progress)
        day_equity_unrealized = equity_unrealized * progress
        day_fno_realized = fno_realized * progress
        buying_power = STARTING_BUYING_POWER

        buckets = {
            "STOCKS": (buying_power + day_equity_value, day_equity_unrealized),
            "FNO": (buying_power + day_fno_realized, Decimal("0")),
            "ALL": (buying_power + day_equity_value + day_fno_realized, day_equity_unrealized),
        }
        for bucket, (net_value, unrealized) in buckets.items():
            cur.execute(
                """INSERT INTO portfolio_equity_snapshots
                     (user_id, bucket, net_value, total_unrealized_pnl, buying_power, captured_at)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (user_id, bucket, net_value, unrealized, buying_power, captured_at)
            )


def main():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        user_id = upsert_demo_user(cur)
        reset_demo_data(cur, user_id)
        seed_wallet(cur, user_id)
        seed_holdings_and_prices(cur, user_id)
        seed_fno_positions(cur, user_id)
        seed_equity_curve(cur, user_id)
        conn.commit()

        print("\nDashboard demo data seeded successfully.")
        print("=" * 50)
        print(f"  Login email:    {DEMO_EMAIL}")
        print(f"  Login password: {DEMO_PASSWORD}")
        print(f"  user_id:        {user_id}")
        print("=" * 50)
        print("POST /login with the above to get a Bearer token, then call:")
        print("  GET /getDashboardSummary")
        print("  GET /getAssetClassSummary?bucket=ALL|STOCKS|FNO")
        print("  GET /getPortfolioEquityCurve?bucket=ALL&range=1M")
        print("  GET /getPortfolioForLoggedInUser")
        print("  GET /getPortfolioOfLoggedInUserWithProfitLoss?bucket=STOCKS")
        print("  GET /getFnoPositionsForLoggedInUser")
    except Exception as ex:
        conn.rollback()
        print(f"[FATAL] Seed failed: {ex}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
