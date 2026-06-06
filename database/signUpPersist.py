from .ConnectionFactory import ConnectionFactory
from typing import Any
from dotenv import load_dotenv
import os

load_dotenv()
class signUpPersist:
    
    def signup_user(self, signup_request: Any):
        INSERT_USER_QUERY = "INSERT INTO users (first_name, last_name, email, password, phone_number, created_at) VALUES (%s, %s, %s, %s, %s, NOW())"
        connection=ConnectionFactory.create_connection(
            os.getenv("MYSQLHOST"),
            os.getenv("MYSQLUSER"),
            os.getenv("MYSQLPASSWORD"),
            os.getenv("MYSQLDATABASE")
        )
        cursor=connection.cursor()
        cursor.execute(INSERT_USER_QUERY, (signup_request.first_name, signup_request.last_name, signup_request.email, signup_request.password, signup_request.phone_number))
        connection.commit()
        cursor.close()
        connection.close()