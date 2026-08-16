from datetime import date

import psycopg2.extras

from database.PostgresConnectionFactory import PostgresConnectionFactory
from productdto.mutualFundDto import NavPointDTO
from utils.query_loader import QueryLoader


class MFNavHistoryRepository:
    """Owns all reads/writes against mf_nav_history - the durable, locally-owned NAV series."""

    def __init__(self, connection_factory=PostgresConnectionFactory.create_connection):
        self._connection_factory = connection_factory

    def bulk_insert(self, scheme_code: int, points: list[NavPointDTO]) -> None:
        """One-time backfill write: inserts the full (capped) history for a scheme."""
        if not points:
            return
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor()
            query = QueryLoader.get('mutual_funds.yaml', 'nav_history_bulk_insert')
            rows = [(scheme_code, point.nav_date, point.nav) for point in points]
            psycopg2.extras.execute_values(cursor, query, rows)
            conn.commit()
        except Exception as ex:
            if conn is not None:
                conn.rollback()
            raise Exception(f"Error bulk-inserting NAV history for {scheme_code}: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def append_daily(self, scheme_code: int, nav_date: date, nav: float) -> None:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('mutual_funds.yaml', 'nav_history_append_daily'),
                (scheme_code, nav_date, nav)
            )
            conn.commit()
        except Exception as ex:
            if conn is not None:
                conn.rollback()
            raise Exception(f"Error appending daily NAV for {scheme_code}: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def get_series(self, scheme_code: int, since: date | None = None) -> list[NavPointDTO]:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('mutual_funds.yaml', 'nav_history_get_series'),
                (scheme_code, since, since)
            )
            return [NavPointDTO(nav_date=row[0], nav=float(row[1])) for row in cursor.fetchall()]
        except Exception as ex:
            raise Exception(f"Error fetching NAV series for {scheme_code}: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
