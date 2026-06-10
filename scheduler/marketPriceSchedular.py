import os
import yfinance as yf
from apscheduler.schedulers.background import BackgroundScheduler

from database.ConnectionFactory import ConnectionFactory


class MarketPriceScheduler:

    _scheduler = None  # prevents duplicate schedulers

    # -----------------------------
    # Fetch price safely
    # -----------------------------
    @staticmethod
    def get_current_price(symbol: str):

        try:
            stock = yf.Ticker(symbol + ".NS")

            history = stock.history(period="1d")

            if (
                history is None
                or history.empty
                or "Close" not in history
            ):
                return None

            return float(history["Close"].iloc[-1])

        except Exception as e:
            print(f"[MarketPriceScheduler] Failed for {symbol}: {e}")
            return None

    # -----------------------------
    # Update DB safely
    # -----------------------------
    @staticmethod
    def update_market_prices():

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

            cursor.execute("""
                SELECT symbol
                FROM market_prices
            """)

            symbols = cursor.fetchall()
            if not symbols:
                print(" No symbols found in market_prices table")
                return
            update_query = """
                UPDATE market_prices
                SET current_price = %s
                WHERE symbol = %s
            """

            updated_count = 0

            for row in symbols:

                symbol = row[0] if isinstance(row, tuple) else row["symbol"]

                price = MarketPriceScheduler.get_current_price(symbol)

                if price is None:
                    continue

                cursor.execute(update_query, (price, symbol))
                updated_count += 1

            conn.commit()

            print(f"[MarketPriceScheduler] Updated {updated_count} symbols")

        except Exception as e:
            if conn:
                conn.rollback()
            print(f"[MarketPriceScheduler] DB update failed: {e}")

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    # -----------------------------
    # Start scheduler safely
    # -----------------------------
    @staticmethod
    def start():

        if MarketPriceScheduler._scheduler is not None:
            print("[MarketPriceScheduler] Already running")
            return

        scheduler = BackgroundScheduler()

        scheduler.add_job(
            MarketPriceScheduler.update_market_prices,
            trigger="interval",
            minutes=5,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60
        )

        scheduler.start()

        MarketPriceScheduler._scheduler = scheduler

        print("[MarketPriceScheduler] Started successfully")