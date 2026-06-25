from .matchingEnginePersistence import matchtradeOrderforUser as MatchtradeOrderForUser

# Create a module-level object that provides access to the static methods
class _MatchingEnginePersistenceModule:
    @staticmethod
    def matchtradeOrderforUser(order, userId, status, cursor):
        return MatchtradeOrderForUser.matchtradeOrderforUser(order, userId, status, cursor)

matchingEnginePersistence = _MatchingEnginePersistenceModule()
