from service.matchingEngine.matchingEngineService.matching_engine_service import MatchingEngineService
##class is for matching the order from order book
class MatchingEngine:
    @staticmethod
    def execute(order, userId,cursor):
        
        ##this call will be replaced by real time NSE market
        if order.side == 'BUY':
            print("NEW MATCHING ENGINE IMPORT LOADED")
            matchFound = MatchingEngineService.matchtradeOrderforUser(order, userId, 'SELL',cursor)
        elif order.side == 'SELL':
             print("NEW MATCHING ENGINE IMPORT LOADED")
             matchFound = MatchingEngineService.matchtradeOrderforUser(order, userId, 'BUY',cursor)
        
                
        if matchFound is None:
                return None
        elif matchFound :
                return matchFound
