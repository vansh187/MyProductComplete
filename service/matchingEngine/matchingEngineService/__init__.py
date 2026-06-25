from .matching_engine_service import MatchingEngineService

# Create a module-level object that provides access to the static methods
class _MatchingEngineServiceModule:
    @staticmethod
    def matchtradeOrderforUser(order, userId, status, cursor):
        return MatchingEngineService.matchtradeOrderforUser(order, userId, status, cursor)
    
    @staticmethod
    def price_match(incomingOrder, matchedOrder):
        return MatchingEngineService.price_match(incomingOrder, matchedOrder)
    
    @staticmethod
    def matchingOrder(incomingOrder, matchFoundOrderResponse):
        return MatchingEngineService.matchingOrder(incomingOrder, matchFoundOrderResponse)

matching_engine_service = _MatchingEngineServiceModule()
