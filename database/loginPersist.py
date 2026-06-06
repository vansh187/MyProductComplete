from .ConnectionFactory import ConnectionFactory
from typing import Any
from dotenv import load_dotenv
import os

load_dotenv()

def login_user(login_request: Any):
    conn = ConnectionFactory.create_connection(
            os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE")
    )
    if conn is None:
        raise Exception("Database connection failed")
    cursor = conn.cursor()
    cursor.execute( "SELECT * FROM users WHERE email = %(email)s",
    {"email": login_request.email})
    user = cursor.fetchone()
    cursor.close()
    conn.close()
    return user


class LoginPersistence:
    def login_user(self, login_request: Any):
        return login_user(login_request)