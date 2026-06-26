import psycopg2.extras
from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
from dotenv import load_dotenv

load_dotenv()


class GoogleAuthPersistence:

    def get_user_by_email(self, email: str):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                QueryLoader.get('login.yaml', 'get_user_by_email'),
                {"email": email}
            )
            return cursor.fetchone()
        except Exception as ex:
            raise Exception(f"Error fetching user by email: {str(ex)}")
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def create_google_user(self, first_name: str, last_name: str, email: str) -> int:
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                QueryLoader.get('signup.yaml', 'insert_google_user'),
                (first_name, last_name, email)
            )
            row = cursor.fetchone()
            conn.commit()
            return row['user_id']
        except Exception as ex:
            if conn:
                conn.rollback()
            raise Exception(f"Error creating Google user: {str(ex)}")
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
