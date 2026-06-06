import os

import mysql.connector
from mysql.connector import Error


class ConnectionFactory:
    @staticmethod
    def create_connection(host_name, user_name, user_password, db_name):
        connection = None
        try:
            print("host:", host_name)
            print("user:", user_name)
            print("database:", db_name)
            connection = mysql.connector.connect(
                host=host_name,
                user=user_name,
                passwd=user_password,
                database=db_name
            )
            ##print("Connection to MySQL DB successful")
        except Error as e:
            print(f"The error '{e}' occurred")
        return connection