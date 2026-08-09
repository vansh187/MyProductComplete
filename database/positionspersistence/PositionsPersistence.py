from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


class PositionsPersistence:

    def __init__(self):
        pass

    def getPositionForUpdate(self, userId, tsym, product_type, cursor):
        """Locks and returns the existing position row (or None), participating
        in the caller's transaction - mirrors portfolioPersistence.process_buyer's
        shared-cursor convention. Scoped by product_type so MIS and NRML
        positions on the same symbol are tracked as separate books."""
        cursor.execute(
            QueryLoader.get('positions.yaml', 'select_position_for_update'),
            (userId, tsym, product_type)
        )
        return cursor.fetchone()

    def insertPosition(self, userId, tsym, broker, token, exchange, underlying, expiry,
                        strike, option_type, lot_size, product_type, source,
                        netqty, netavgprc, buyqty, sellqty, buyavgprc, sellavgprc,
                        realized_pnl, status, contract_type, cursor):
        cursor.execute(
            QueryLoader.get('positions.yaml', 'insert_position'),
            (userId, tsym, broker, token, exchange, underlying, expiry, strike, option_type,
             lot_size, product_type, source, netqty, netavgprc, buyqty, sellqty, buyavgprc,
             sellavgprc, realized_pnl, status, contract_type)
        )

    def updatePosition(self, userId, tsym, product_type, netqty, netavgprc, buyqty, sellqty,
                        buyavgprc, sellavgprc, realized_pnl, status, cursor):
        cursor.execute(
            QueryLoader.get('positions.yaml', 'update_position'),
            (netqty, netavgprc, buyqty, sellqty, buyavgprc, sellavgprc,
             realized_pnl, status, userId, tsym, product_type)
        )

    def getPositionsForUser(self, userId):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(QueryLoader.get('positions.yaml', 'select_positions_for_user'), (userId,))
            return cursor.fetchall()
        except Exception as ex:
            raise Exception("Error fetching positions for user") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def getPositionsSummaryForUser(self, userId):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(QueryLoader.get('positions.yaml', 'select_positions_summary_for_user'), (userId,))
            return cursor.fetchone()
        except Exception as ex:
            raise Exception("Error fetching positions summary for user") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def getDistinctUsersWithPositions(self):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(QueryLoader.get('positions.yaml', 'select_distinct_users_with_positions'))
            return cursor.fetchall()
        except Exception as ex:
            raise Exception("Error fetching distinct users with positions") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
