from utils.query_loader import QueryLoader
from dotenv import load_dotenv

load_dotenv()


class matchtradeOrderforUser:

    def __init__(self):
        pass

    def matchtradingOrderforUser(self, order, status, cursor):
        try:
            if status == 'BUY':
                cursor.execute(QueryLoader.get('matching_engine.yaml', 'select_buy_orders'), (order.symbol,))
            elif status == 'SELL':
                cursor.execute(QueryLoader.get('matching_engine.yaml', 'select_sell_orders'), (order.symbol,))
            return cursor.fetchall()
        except Exception as ex:
            raise Exception("Error in simulation of matching Order")
        finally:
            print("in final block of create match order for user")
