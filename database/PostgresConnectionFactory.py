import psycopg2
import psycopg2.extras
import os
from dotenv import load_dotenv

load_dotenv()

class PostgresConnectionFactory:
    @staticmethod
    def create_connection():
        conn = psycopg2.connect(
            host=os.getenv("PGHOST", "localhost"),
            port=int(os.getenv("PGPORT", 5432)),
            user=os.getenv("PGUSER"),
            password=os.getenv("PGPASSWORD"),
            dbname=os.getenv("PGDATABASE")
        )
        return conn
