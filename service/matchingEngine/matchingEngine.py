from service.matchingEngine.matchingEngineService.matching_engine_service import MatchingEngineService
from api.models import OrderSide

class MatchingEngine:

    def __init__(self,order,userId,cursor):
          self.order=order
          self.userId=userId
          self.cursor=cursor

    def execute(self, order, userId, cursor, order_id):
        matchingService = MatchingEngineService(order, userId, cursor)
        if order.side == OrderSide.BUY:
            matchFound = matchingService.matchtradeOrderforUser(order, userId, 'SELL', cursor, order_id)
        elif order.side == OrderSide.SELL:
            matchFound = matchingService.matchtradeOrderforUser(order, userId, 'BUY', cursor, order_id)
        else:
            return {"userId": userId, "tradeExcecution": []}

        if matchFound is None:
            return None
        elif matchFound:
            return matchFound
