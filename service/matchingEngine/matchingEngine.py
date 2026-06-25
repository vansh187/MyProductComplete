from service.matchingEngine.matchingEngineService.matching_engine_service import MatchingEngineService
##class is for matching the order from order book
class MatchingEngine:
    
    def __init__(self,order,userId,cursor):
          self.order=order
          self.userId=userId
          self.cursor=cursor
    
    def execute(self,order, userId,cursor):
        matchingService=MatchingEngineService(order,userId,cursor)
        if order.side == 'BUY':
            matchFound = matchingService.matchtradeOrderforUser(order, userId, 'SELL',cursor)
        elif order.side == 'SELL':
            matchFound = matchingService.matchtradeOrderforUser(order, userId, 'BUY',cursor)
        else:
            return {"userId": userId, "tradeExcecution": []}

        if matchFound is None:
            return None
        elif matchFound:
            return matchFound
