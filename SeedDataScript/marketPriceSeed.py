import yfinance as yf
import os

from database.ConnectionFactory import ConnectionFactory
from dotenv import load_dotenv
load_dotenv()

# -----------------------------
# 10 SYMBOLS
# -----------------------------
SYMBOLS = [
    "TCS",
    "WIPRO",
    "INFY",
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "AXISBANK",
    "LT",
    "SBIN",
    "ITC",
    
    ]


# -----------------------------
# Fetch price safely
# -----------------------------
def get_price(symbol: str):

    try:
        stock = yf.Ticker(symbol + ".NS")
        history = stock.history(period="1d")

        if history is None or history.empty:
            return None

        return float(history["Close"].iloc[-1])

    except Exception as e:
        print(f"[ERROR] Failed for {symbol}: {e}")
        return None


# -----------------------------
# Seed DB
# -----------------------------
def seed_market_prices():

    conn = None
    cursor = None

    try:
        conn = ConnectionFactory.create_connection(
            os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE"),
            os.getenv("MYSQLPORT", 3306)
        )

        cursor = conn.cursor()

        print("Starting market price seed...\n")

        for symbol in SYMBOLS:

            price = get_price(symbol)

            if price is None:
                print(f"Skipping {symbol} (no price)")
                continue

            query = """
                INSERT INTO market_prices (symbol, current_price)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE
                    current_price = VALUES(current_price),
                    updated_at = CURRENT_TIMESTAMP
            """

            cursor.execute(query, (symbol, price))

            print(f"{symbol} -> {price}")

        conn.commit()

        print("\nSeed completed successfully!")

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[FATAL ERROR] {e}")

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    seed_market_prices()