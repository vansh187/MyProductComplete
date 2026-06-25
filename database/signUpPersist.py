from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader
from typing import Any
from dotenv import load_dotenv

load_dotenv()

class signUpPersist:

    def signup_user(self, signup_request: Any):
        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            cursor.execute(
                QueryLoader.get('signup.yaml', 'insert_user'),
                (signup_request.first_name, signup_request.last_name,
                 signup_request.email, signup_request.password,
                 signup_request.phone_number)
            )
            conn.commit()
        except Exception as ex:
            if conn:
                conn.rollback()
            raise Exception(f"Error in signup: {str(ex)}")
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
