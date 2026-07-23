from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


class EquityCurvePersistence:

    def __init__(self):
        pass

    def insertSnapshot(self, userId, bucket, net_value, total_unrealized_pnl, buying_power):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('equitycurve.yaml', 'insert_snapshot'),
                (userId, bucket, net_value, total_unrealized_pnl, buying_power)
            )
            conn.commit()
        except Exception as ex:
            if conn:
                conn.rollback()
            raise Exception("Error inserting equity snapshot") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def getSnapshotsSince(self, userId, bucket, since):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                QueryLoader.get('equitycurve.yaml', 'select_snapshots_since'),
                (userId, bucket, since)
            )
            return cursor.fetchall()
        except Exception as ex:
            raise Exception("Error fetching equity snapshots") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def getFirstSnapshotSince(self, userId, bucket, since):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                QueryLoader.get('equitycurve.yaml', 'select_first_snapshot_since'),
                (userId, bucket, since)
            )
            return cursor.fetchone()
        except Exception as ex:
            raise Exception("Error fetching first equity snapshot") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def getLatestSnapshotTime(self, userId, bucket):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                QueryLoader.get('equitycurve.yaml', 'select_latest_snapshot'),
                (userId, bucket)
            )
            return cursor.fetchone()
        except Exception as ex:
            raise Exception("Error fetching latest equity snapshot time") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
