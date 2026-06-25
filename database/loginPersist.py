from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
import psycopg2.extras
from typing import Any
from dotenv import load_dotenv

load_dotenv()


class LoginPersistence:

    def login_user(self, login_request: Any):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                QueryLoader.get('login.yaml', 'get_user_by_email'),
                {"email": login_request.email}
            )
            user = cursor.fetchone()
            return user
        except Exception as ex:
            raise Exception(f"Database connection failed: {str(ex)}")
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
