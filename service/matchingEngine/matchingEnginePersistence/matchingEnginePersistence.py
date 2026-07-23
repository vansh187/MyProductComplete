import logging
from utils.query_loader import QueryLoader
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class matchtradeOrderforUser:

    def __init__(self):
        pass

    def matchtradingOrderforUser(self, order, status, cursor, userId):
        try:
            # Excludes the incoming order's own resting orders on the
            # opposite side directly in the query (rather than filtering
            # self-trades out after matching) - a same-user resting order
            # must never be treated as a match candidate at all, otherwise
            # it silently consumes the incoming order's quantity in
            # matchingOrder() before the genuine counterparty order (further
            # down the price-priority list) is ever considered. Filtering at
            # the SQL level is also strictly cheaper: fewer rows fetched and
            # locked (FOR UPDATE NOWAIT) per match attempt.
            if status == 'BUY':
                cursor.execute(QueryLoader.get('matching_engine.yaml', 'select_buy_orders'), (order.symbol, userId))
            elif status == 'SELL':
                cursor.execute(QueryLoader.get('matching_engine.yaml', 'select_sell_orders'), (order.symbol, userId))
            return cursor.fetchall()
        except Exception as ex:
            raise Exception("Error in simulation of matching Order")
        finally:
            logger.debug("Matching engine query executed")
