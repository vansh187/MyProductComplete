import psycopg2
import psycopg2.extras
import os
from dotenv import load_dotenv

load_dotenv()

class PostgresConnectionFactory:
    @staticmethod
    def create_connection():
        database_url = os.getenv("DATABASE_URL")
        if database_url:
            conn = psycopg2.connect(database_url, sslmode="require")
        else:
            conn = psycopg2.connect(
                host=os.getenv("PGHOST", "localhost"),
                port=int(os.getenv("PGPORT", 5432)),
                user=os.getenv("PGUSER"),
                password=os.getenv("PGPASSWORD"),
                dbname=os.getenv("PGDATABASE"),
                sslmode="require"
            )
        return conn
