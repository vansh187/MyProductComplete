from service.matchingEngine.matchingEngineService.matching_engine_service import MatchingEngineService
##class is for matching the order from order book
class MatchingEngine:
    
    def __init__(self,order,userId,cursor):
          self.order=order
          self.userId=userId
          self.cursor=cursor
    
    def execute(self,order, userId,cursor):
        matchingService=MatchingEngineService(order,userId,cursor)
        ##this call will be replaced by real time NSE market
        if order.side == 'BUY':
            print("NEW MATCHING ENGINE IMPORT LOADED")
            matchFound = matchingService.matchtradeOrderforUser(order, userId, 'SELL',cursor)
        elif order.side == 'SELL':
             print("NEW MATCHING ENGINE IMPORT LOADED")
             matchFound = matchingService.matchtradeOrderforUser(order, userId, 'BUY',cursor)
        
                
        if matchFound is None:
                return None
        elif matchFound :
                return matchFound
