from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
import psycopg2.extras
from dotenv import load_dotenv
from decimal import Decimal

load_dotenv()


class portfolioPersistence:

    def process_buyer(userId, symbol, quantity, price, cursor):
        try:
            if price <= 0:
                return {"userId": userId, "Message": "Trade did not happened for negative price"}

            cursor.execute(QueryLoader.get('portfolio.yaml', 'select_holdings'), (userId, symbol))
            holdings = cursor.fetchone()
            if not holdings:
                cursor.execute(
                    QueryLoader.get('portfolio.yaml', 'insert_holdings'),
                    (userId, symbol, quantity, price)
                )
                print("HOLDINGS INSERTED")
            else:
                old_qty = holdings["quantity"]
                old_price = holdings["avg_price"]
                if old_qty <= 0:
                    return {"userId": userId, "Message": "Trade did not happened for negative quantity"}
                new_qty = old_qty + quantity
                new_avg = ((old_qty * old_price) + (quantity * price)) / new_qty
                cursor.execute(
                    QueryLoader.get('portfolio.yaml', 'update_holdings'),
                    (new_qty, new_avg, userId, symbol)
                )
        except Exception as ex:
            raise Exception("Exception in inserting holdings")
        finally:
            print("Execute finally block process buy")

    def process_seller(userId, symbol, quantity, price, cursor):
        if quantity <= 0:
            raise Exception("Quantity must be greater than zero")
        try:
            cursor.execute(QueryLoader.get('portfolio.yaml', 'select_holdings'), (userId, symbol))
            sellHolding = cursor.fetchone()
            if not sellHolding:
                cursor.execute(
                    QueryLoader.get('portfolio.yaml', 'insert_holdings_with_time'),
                    (userId, symbol, quantity, price)
                )
                print("HOLDINGS INSERTED")
            else:
                old_qty = sellHolding["quantity"]
                if quantity > old_qty:
                    return {"userId": userId, "Message": "quantity you have is less than trade quantity"}
                new_qty = old_qty - quantity
                cursor.execute(
                    QueryLoader.get('portfolio.yaml', 'update_order_quantity'),
                    (new_qty, userId, symbol)
                )
        except Exception as e:
            raise Exception(f"Exception in process_seller: {str(e)}")
        finally:
            print("Executing finally for process sell")

    def updateorderStatus(cursor, userId, symbol, status, buy_order_id, sell_order_id):
        value = status if status is not None else "EXECUTED"
        cursor.execute(
            QueryLoader.get('orders.yaml', 'update_order_status'),
            (value, buy_order_id, sell_order_id)
        )
        print("order status update success")

    def updateStatus(userId, symbol, status, buy_order_id, sell_order_id, cursor):
        portfolioPersistence.updateorderStatus(cursor, userId, symbol, status, buy_order_id, sell_order_id)

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

    def getPortfolioServiceforLoggedInUser(userId):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(QueryLoader.get('portfolio.yaml', 'select_user_portfolio'), (userId,))
            return cursor.fetchall()
        except Exception as ex:
            raise Exception("Error in getting portfolio for logged in user")
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

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
                (userId, order.symbol, order.side, 'LIMIT',
                 order.quantity, order.quantity, order.price, 'PENDING', orderId)
            )
            row = cursor.fetchone()
            return row['id']
        except Exception as ex:
            raise Exception("Error in Inserting Data")
        finally:
            print("finally block of order book")

    def updateOrderBookQuantity(quantity, orderBookId, status, buy_order_id, sell_order_id, cursor):
        try:
            cursor.execute(
                QueryLoader.get('portfolio.yaml', 'update_order_book_quantity'),
                (quantity, status, buy_order_id, sell_order_id)
            )
            print("trade executed")
        except Exception as ex:
            raise Exception("Error in updating orderBook")
        finally:
            print("final block of update Order book")
