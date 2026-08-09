import psycopg2.extras

from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader


class MFSchemeRepository:
    """
    Owns all reads/writes against mf_schemes. Instance-based (constructor
    injection of the connection factory) rather than the static-method
    *Persistence convention used elsewhere in this repo, per this module's
    OOP requirement - a plain function default keeps the existing
    open/commit/close-per-call pattern intact.
    """

    def __init__(self, connection_factory=PostgresConnectionFactory.create_connection):
        self._connection_factory = connection_factory

    def upsert_schemes(self, schemes: list[dict]) -> None:
        """schemes: [{scheme_code, scheme_name}, ...] - identity only; category/fund_house
        etc. are filled in later by the backfill staleness check via mark_active_and_backfilled."""
        if not schemes:
            return
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor()
            query = QueryLoader.get('mutual_funds.yaml', 'upsert_schemes_bulk')
            # execute_values batches rows into a handful of multi-row INSERTs
            # instead of executemany's one-round-trip-per-row - matters here
            # since the daily sync upserts the full ~50k-75k scheme catalog.
            psycopg2.extras.execute_values(cursor, query, [(s["scheme_code"], s["scheme_name"]) for s in schemes])
            conn.commit()
        except Exception as ex:
            if conn is not None:
                conn.rollback()
            raise Exception(f"Error upserting mf_schemes: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def search_schemes(self, query: str | None, category: str | None = None,
                        fund_house: str | None = None, page: int = 1, page_size: int = 20) -> list[dict]:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            sql = QueryLoader.get('mutual_funds.yaml', 'search_schemes')
            cursor.execute(sql, {
                "query": query,
                "query_pattern": f"%{query}%" if query else None,
                "category": category,
                "fund_house": fund_house,
                "limit": page_size,
                "offset": max(page - 1, 0) * page_size,
            })
            return [dict(row) for row in cursor.fetchall()]
        except Exception as ex:
            raise Exception(f"Error searching mf_schemes: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def get_scheme(self, scheme_code: int) -> dict | None:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(QueryLoader.get('mutual_funds.yaml', 'get_scheme_by_code'), (scheme_code,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as ex:
            raise Exception(f"Error fetching scheme {scheme_code}: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def list_categories(self) -> list[str]:
        return self._list_distinct('list_categories', 'scheme_category')

    def list_fund_houses(self) -> list[str]:
        return self._list_distinct('list_fund_houses', 'fund_house')

    def _list_distinct(self, query_key: str, column: str) -> list[str]:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor()
            cursor.execute(QueryLoader.get('mutual_funds.yaml', query_key))
            return [row[0] for row in cursor.fetchall()]
        except Exception as ex:
            raise Exception(f"Error listing {column}: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def get_schemes_pending_backfill(self, batch_size: int) -> list[int]:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor()
            cursor.execute(QueryLoader.get('mutual_funds.yaml', 'get_schemes_pending_backfill'), (batch_size,))
            return [row[0] for row in cursor.fetchall()]
        except Exception as ex:
            raise Exception(f"Error fetching schemes pending backfill: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def mark_inactive(self, scheme_code: int) -> None:
        self._execute_and_commit('mark_inactive', (scheme_code,))

    def mark_active_and_backfilled(self, scheme_code: int, meta: dict) -> None:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor()
            cursor.execute(QueryLoader.get('mutual_funds.yaml', 'mark_active_and_backfilled'), {
                "scheme_code": scheme_code,
                "fund_house": meta.get("fund_house"),
                "scheme_type": meta.get("scheme_type"),
                "scheme_category": meta.get("scheme_category"),
                "isin_growth": meta.get("isin_growth"),
                "isin_div_reinvestment": meta.get("isin_div_reinvestment"),
                "scheme_name": meta.get("scheme_name"),
            })
            conn.commit()
        except Exception as ex:
            if conn is not None:
                conn.rollback()
            raise Exception(f"Error marking scheme {scheme_code} active/backfilled: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def get_all_backfilled_scheme_codes(self) -> list[int]:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor()
            cursor.execute(QueryLoader.get('mutual_funds.yaml', 'get_all_backfilled_scheme_codes'))
            return [row[0] for row in cursor.fetchall()]
        except Exception as ex:
            raise Exception(f"Error fetching backfilled scheme codes: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def _execute_and_commit(self, query_key: str, params: tuple) -> None:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor()
            cursor.execute(QueryLoader.get('mutual_funds.yaml', query_key), params)
            conn.commit()
        except Exception as ex:
            if conn is not None:
                conn.rollback()
            raise Exception(f"Error executing {query_key}: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
