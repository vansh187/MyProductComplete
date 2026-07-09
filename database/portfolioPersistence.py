from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
import psycopg2.extras
from dotenv import load_dotenv
from decimal import Decimal

load_dotenv()


class portfolioPersistence:

    @staticmethod
    def process_buyer(userId, symbol, quantity, price, cursor):
        if price is None or not isinstance(price, (int, float, Decimal)) or price <= 0:
            raise ValueError(f"Invalid price for buyer: {price}")
        try:
            cursor.execute(QueryLoader.get('portfolio.yaml', 'select_holdings'), (userId, symbol))
            holdings = cursor.fetchone()
            if not holdings:
                cursor.execute(
                    QueryLoader.get('portfolio.yaml', 'insert_holdings'),
                    (userId, symbol, quantity, price)
                )
            else:
                old_qty = holdings["quantity"]
                old_price = holdings["avg_price"]
                if old_qty < 0:
                    return {"userId": userId, "Message": "Trade did not happened for negative quantity"}
                new_qty = old_qty + quantity
                new_avg = ((old_qty * old_price) + (quantity * price)) / new_qty
                cursor.execute(
                    QueryLoader.get('portfolio.yaml', 'update_holdings'),
                    (new_qty, new_avg, userId, symbol)
                )
        except Exception as ex:
            raise Exception(f"Exception in process_buyer: {str(ex)}") from ex

    @staticmethod
    def process_seller(userId, symbol, quantity, price, cursor):
        if quantity <= 0:
            raise Exception("Quantity must be greater than zero")
        try:
            cursor.execute(QueryLoader.get('portfolio.yaml', 'select_holdings'), (userId, symbol))
            sellHolding = cursor.fetchone()
            if not sellHolding:
                raise Exception(f"No holdings found for user {userId} symbol {symbol}")
            old_qty = sellHolding["quantity"]
            if quantity > old_qty:
                raise Exception(f"Insufficient holdings: have {old_qty}, need {quantity}")
            new_qty = old_qty - quantity
            cursor.execute(
                QueryLoader.get('portfolio.yaml', 'update_order_quantity'),
                (new_qty, userId, symbol)
            )
        except Exception as e:
            raise Exception(f"Exception in process_seller: {str(e)}") from e

    @staticmethod
    def updateOrderStatusSingle(status, order_id, cursor, avg_fill_price=0, filled_qty=0,
                                 trigger_price=None, client_order_id=None):
        cursor.execute(
            QueryLoader.get('orders.yaml', 'update_order_status_single'),
            (status, avg_fill_price, filled_qty, trigger_price, client_order_id, order_id)
        )

    @staticmethod
    def updateorderStatus(cursor, userId, symbol, status, buy_order_id, sell_order_id):
        value = status if status is not None else "EXECUTED"
        cursor.execute(
            QueryLoader.get('orders.yaml', 'update_order_status'),
            (value, buy_order_id, sell_order_id)
        )

    @staticmethod
    def updateStatus(userId, symbol, status, buy_order_id, sell_order_id, cursor):
        portfolioPersistence.updateorderStatus(cursor, userId, symbol, status, buy_order_id, sell_order_id)

    @staticmethod
    def updateCounterpartyOrderBook(remaining_qty, status, counterparty_order_id, cursor):
        """Update the counterparty's order_book row by orders.id FK."""
        try:
            cursor.execute(
                QueryLoader.get('portfolio.yaml', 'update_counterparty_order_book'),
                (remaining_qty, status, counterparty_order_id)
            )
        except Exception as ex:
            raise Exception(f"Error updating counterparty order book: {str(ex)}") from ex

    @staticmethod
    def updateIncomingOrderBook(remaining_qty, status, order_book_id, cursor):
        """Update the incoming order's order_book row by order_book.id PK."""
        try:
            cursor.execute(
                QueryLoader.get('portfolio.yaml', 'update_incoming_order_book'),
                (remaining_qty, status, order_book_id)
            )
        except Exception as ex:
            raise Exception(f"Error updating incoming order book: {str(ex)}") from ex

    @staticmethod
    def createUserHolding(order, userId):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('portfolio.yaml', 'insert_holdings_with_time'),
                (userId, order.symbol, order.quantity, order.avg_price)
            )
            conn.commit()
        except Exception as ex:
            if conn:
                conn.rollback()
            raise Exception("Error in inserting holdings") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    @staticmethod
    def updateUserHoldings(order, userId, new_qty, round, new_avg):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('portfolio.yaml', 'update_user_holdings'),
                (new_qty, new_avg, userId, order.symbol)
            )
            conn.commit()
        except Exception as ex:
            if conn:
                conn.rollback()
            raise Exception("Error in holdings update") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    @staticmethod
    def getPortfolioServiceforLoggedInUser(userId):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(QueryLoader.get('portfolio.yaml', 'select_user_portfolio'), (userId,))
            return cursor.fetchall()
        except Exception as ex:
            raise Exception("Error in getting portfolio for logged in user") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    @staticmethod
    def getPortfolioOfLoggedInUserWithProfitLoss(userId):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(QueryLoader.get('portfolio.yaml', 'select_portfolio_with_pnl'), (userId,))
            return cursor.fetchall()
        except Exception as ex:
            raise Exception("Error in fetching data for Profit and Loss") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def createTradeinOrderBook(conn, cursor, order, userId, orderId):
        try:
            cursor.execute(
                QueryLoader.get('portfolio.yaml', 'insert_order_book'),
                (userId, order.symbol, order.side.value, 'LIMIT',
                 order.quantity, order.quantity, order.price, 'PENDING', orderId)
            )
            row = cursor.fetchone()
            if row is None:
                raise Exception("Failed to create order book entry - no ID returned")
            return row['id']
        except Exception as ex:
            raise Exception("Error in Inserting Data") from ex
