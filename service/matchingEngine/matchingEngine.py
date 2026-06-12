from service.matchingEngine.matchingEngineService import matchingEngineService
from service.matchingEngine.matchingEnginePersistence import matchingEnginePersistence


##class is for matching the order from order book
class MatchingEngine:
    @staticmethod
    def execute(order, userId,cursor):
        
        ##this call will be replaced by real time NSE market
        if order.side == 'BUY':
            matchFound = matchingEngineService.matchtradeOrderforUser(order, userId, 'SELL',cursor)
        elif order.side == 'SELL':
             matchFound = matchingEngineService.matchtradeOrderforUser(order, userId, 'BUY',cursor)
        
                
        if matchFound is None:
                return None
        elif matchFound :
                return matchFound