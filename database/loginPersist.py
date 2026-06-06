from .ConnectionFactory import ConnectionFactory
from typing import Any


def login_user(login_request: Any):
    conn = ConnectionFactory.create_connection("localhost", "root", "root", "PortfolioDb")
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