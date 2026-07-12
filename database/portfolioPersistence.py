from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
from api.models import is_dormant_order_type, order_type_str
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
            order_type_value = order_type_str(order.order_type)
            # STOP/STOPLIMIT orders must not be immediately matchable - they
            # start dormant (PENDING_TRIGGER) until StopOrderTriggerService
            # sees a trade price that crosses trigger_price. MARKET/LIMIT
            # orders keep the existing PENDING (immediately matchable) behavior.
            initial_status = "PENDING_TRIGGER" if is_dormant_order_type(order.order_type) else "PENDING"
            exchange_value = order.exchange.value if hasattr(order.exchange, "value") else str(order.exchange)
            product_type_value = order.product_type.value if hasattr(order.product_type, "value") else str(order.product_type)
            validity_value = order.validity.value if hasattr(order.validity, "value") else str(order.validity)

            cursor.execute(
                QueryLoader.get('portfolio.yaml', 'insert_order_book'),
                (userId, order.symbol, order.side.value, order_type_value,
                 order.quantity, order.quantity, order.price, order.trigger_price,
                 exchange_value, product_type_value, validity_value, order.client_order_id,
                 initial_status, orderId)
            )
            row = cursor.fetchone()
            if row is None:
                raise Exception("Failed to create order book entry - no ID returned")
            return row['id']
        except Exception as ex:
            raise Exception("Error in Inserting Data") from ex

    @staticmethod
    def get_pending_trigger_orders_by_symbol(symbol, cursor):
        """Fetch resting STOP/STOPLIMIT order_book rows for a symbol, locked
        FOR UPDATE so two concurrent trade-price signals can't both try to
        trigger the same order. Caller owns the transaction/cursor."""
        cursor.execute(
            QueryLoader.get('portfolio.yaml', 'select_pending_trigger_orders_by_symbol'),
            (symbol,)
        )
        return cursor.fetchall()

    @staticmethod
    def mark_order_book_triggered(order_book_id, new_price, cursor):
        """Flips a triggered order_book row from PENDING_TRIGGER to PENDING so
        it becomes visible to the matching engine's select_buy_orders/
        select_sell_orders queries. new_price is only applied for plain STOP
        orders (no existing limit price) - passing None leaves price
        unchanged, which is correct for STOPLIMIT orders that already carry
        their own limit price."""
        cursor.execute(
            QueryLoader.get('portfolio.yaml', 'mark_order_book_triggered'),
            (new_price, order_book_id)
        )

    @staticmethod
    def mark_orders_triggered(order_id, new_price, cursor):
        """Mirrors mark_order_book_triggered on the orders table, so GET
        /orders reflects PENDING (resting/matchable) instead of the stale
        PENDING_TRIGGER (dormant) status once triggered."""
        cursor.execute(
            QueryLoader.get('portfolio.yaml', 'mark_orders_triggered'),
            (new_price, order_id)
        )

    @staticmethod
    def cancel_order_book_by_order_id(order_id, cursor):
        """Cancels the order_book row for a cancelled order. Previously
        missing entirely: cancel_order_by_id only updated the `orders` table,
        leaving the order_book row (what select_buy_orders/select_sell_orders
        in matching_engine.yaml actually query) still PENDING/PARTIALLY_EXECUTED
        - a "cancelled" resting order could still be silently matched against
        an incoming order. Also required for STOP-order correctness: without
        this, cancelling a not-yet-triggered STOP order would leave its
        order_book row at PENDING_TRIGGER, where a later trade could still
        trigger and match an order the user believes they cancelled."""
        cursor.execute(
            QueryLoader.get('portfolio.yaml', 'cancel_order_book_by_order_id'),
            (order_id,)
        )

    @staticmethod
    def modify_order_book_by_order_id(order_id, price, quantity, remaining_quantity, trigger_price, cursor):
        """Mirrors modify_order (orders.yaml) on the order_book row - see
        OrderPersistence.modify_order_by_id's docstring. remaining_quantity
        is passed as the same new quantity value (never independently), since
        modify is only allowed on orders with zero fills so far
        (status IN ('PENDING', 'PENDING_TRIGGER'))."""
        cursor.execute(
            QueryLoader.get('portfolio.yaml', 'modify_order_book_by_order_id'),
            (price, quantity, remaining_quantity, trigger_price, order_id)
        )
