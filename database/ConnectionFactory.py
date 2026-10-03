# LEGACY — MySQL connection factory. No longer used by the production application.
# All production persistence code uses database/PostgresConnectionFactory.py (PostgreSQL).
# This file is retained only because scheduler/marketPriceSchedular.py and
# SeedDataScript/marketPriceSeed.py still reference it. Do not use for new code.

import os

import mysql.connector
from mysql.connector import Error
import logging

logger = logging.getLogger(__name__)


class ConnectionFactory:
    @staticmethod
    def create_connection(host_name, user_name, user_password, db_name, port=3306):
        connection = None
        try:
            logger.debug(f"MySQL connect host={host_name} port={port} user={user_name} database={db_name}")
            connection = mysql.connector.connect(
                host=host_name,
                port=int(port),
                user=user_name,
                passwd=user_password,
                database=db_name
            )
            ##print("Connection to MySQL DB successful")
        except ValueError as e:
            logger.error(f"The error '{e}' occurred")
        except Error as e:
            logger.error(f"The error '{e}' occurred")
        return connection