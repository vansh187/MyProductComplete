from supabase import create_client, Client
import os
from dotenv import load_dotenv
load_dotenv()

class PostgresConnectionFactory:
        @staticmethod
        def test_connection():
                SUPABASE_URL = os.getenv("SUPABASE_URL")
                SUPABASE_KEY = os.getenv("POSTGRES_PUBLIC_KEY")
                try:
                        # Initialize the client
                        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
                        print("Connection successful!")
                        return supabase
                except Exception as e:
                        print("Connection failed!")
                        print("Error details:", e)
