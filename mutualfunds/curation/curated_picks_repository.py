import psycopg2.extras

from database.PostgresConnectionFactory import PostgresConnectionFactory
from productdto.mutualFundDto import CuratedPickDTO
from utils.query_loader import QueryLoader


class MFCuratedPicksRepository:
    """Owns all reads/writes against mf_curated_picks - the daily LLM-curation output."""

    def __init__(self, connection_factory=PostgresConnectionFactory.create_connection):
        self._connection_factory = connection_factory

    def upsert_picks(self, collection_key: str, picks: list[CuratedPickDTO]) -> None:
        """Replaces the entire pick list for a collection in one transaction."""
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor()
            cursor.execute(QueryLoader.get('mutual_funds.yaml', 'delete_curated_picks_for_collection'), (collection_key,))
            insert_query = QueryLoader.get('mutual_funds.yaml', 'insert_curated_pick')
            for pick in picks:
                cursor.execute(insert_query, (collection_key, pick.scheme_code, pick.rank, pick.blurb, pick.curated_by))
            conn.commit()
        except Exception as ex:
            if conn is not None:
                conn.rollback()
            raise Exception(f"Error upserting curated picks for {collection_key}: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def get_picks(self, collection_key: str) -> list[dict]:
        conn = None
        cursor = None
        try:
            conn = self._connection_factory()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(QueryLoader.get('mutual_funds.yaml', 'get_curated_picks'), (collection_key,))
            return [dict(row) for row in cursor.fetchall()]
        except Exception as ex:
            raise Exception(f"Error fetching curated picks for {collection_key}: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
