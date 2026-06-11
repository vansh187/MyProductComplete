from matchingEngineService import matchingEngineService
from matchingEnginePersistence import matchingEnginePersistence


##class is for matching the order from order book
class MatchingEngine:
    def execute(order,userId):
        
        ##this call will be replaced by real time NSE market
        if order.type is 'BUY':
            matchFound=matchingEngineService.matchtradeOrderforUser(order,userId,'SELL')
        elif order.type:
             matchFound=matchingEngineService.mamatchtradeOrderforUser(order,userId,'BUY')
        
                
        if matchFound is None:
                return None
        elif matchFound is True:
                return True
        elif matchFound is False:
                return False  