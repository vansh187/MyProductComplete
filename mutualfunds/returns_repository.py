import psycopg2.extras

from database.PostgresConnectionFactory import PostgresConnectionFactory
from productdto.mutualFundDto import ReturnsDTO
from utils.query_loader import QueryLoader

# Whitelisted so a caller-supplied sort_by can never be interpolated as
# arbitrary SQL - top_by_category().format()s this into an ORDER BY clause
# rather than passing it as a bound parameter (Postgres doesn't allow bound
# parameters for column/identifier names).
_ALLOWED_SORT_COLUMNS = {"return_1m", "return_6m", "return_1y", "return_3y", "return_5y", "day_change_pct"}


class MFReturnsRepository:
    """Owns all reads/writes against mf_scheme_returns - the durable, locally-computed performance table."""

    def __init__(self, connection_factory=PostgresConnectionFactory.create_connection):
        self._connection_factory = connection_factory

    def upsert_returns(self, scheme_code: int, returns: ReturnsDTO) -> None:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor()
            cursor.execute(QueryLoader.get('mutual_funds.yaml', 'upsert_returns'), {
                "scheme_code": scheme_code,
                "return_1m": returns.return_1m,
                "return_6m": returns.return_6m,
                "return_1y": returns.return_1y,
                "return_3y": returns.return_3y,
                "return_5y": returns.return_5y,
                "day_change_pct": returns.day_change_pct,
                "latest_nav": returns.latest_nav,
            })
            conn.commit()
        except Exception as ex:
            if conn is not None:
                conn.rollback()
            raise Exception(f"Error upserting returns for {scheme_code}: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def get_returns(self, scheme_code: int) -> ReturnsDTO | None:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(QueryLoader.get('mutual_funds.yaml', 'get_returns'), (scheme_code,))
            row = cursor.fetchone()
            if row is None:
                return None
            return ReturnsDTO(
                return_1m=row["return_1m"], return_6m=row["return_6m"], return_1y=row["return_1y"],
                return_3y=row["return_3y"], return_5y=row["return_5y"],
                day_change_pct=row["day_change_pct"], latest_nav=row["latest_nav"],
            )
        except Exception as ex:
            raise Exception(f"Error fetching returns for {scheme_code}: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def top_by_category(self, categories: list[str] | None = None, sort_by: str = "return_3y",
                         limit: int = 10, offset: int = 0) -> list[dict]:
        if sort_by not in _ALLOWED_SORT_COLUMNS:
            raise ValueError(f"sort_by must be one of {_ALLOWED_SORT_COLUMNS}, got {sort_by!r}")
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            query = QueryLoader.get('mutual_funds.yaml', 'top_by_category').format(sort_by=sort_by)
            cursor.execute(query, {"categories": categories, "limit": limit, "offset": offset})
            return [dict(row) for row in cursor.fetchall()]
        except Exception as ex:
            raise Exception(f"Error fetching top schemes by category: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
