from database.walletbalancepersistence.WalletBalancePersistence import WalletBalancePersistence

class WalletBalanceService:

    def __init__(self):
        pass

    def getWalletBalance(self, userId):
        try:
            walletBalancePersistence = WalletBalancePersistence()
            return walletBalancePersistence.getWalletBalance(userId)
        except Exception as ex:
            raise Exception(f"WalletBalanceService error: {str(ex)}")

    def creditWallet(self, cursor, userId, amount):
        try:
            walletBalancePersistence = WalletBalancePersistence()
            return walletBalancePersistence.creditWallet(cursor, userId, amount)
        except Exception as ex:
            raise Exception(f"WalletBalanceService error: {str(ex)}") from ex

    def debitWalletIfSufficient(self, userId, amount) -> bool:
        try:
            walletBalancePersistence = WalletBalancePersistence()
            return walletBalancePersistence.debitWalletIfSufficient(userId, amount)
        except Exception as ex:
            raise Exception(f"WalletBalanceService error: {str(ex)}") from ex

    def creditWalletStandalone(self, userId, amount) -> None:
        try:
            walletBalancePersistence = WalletBalancePersistence()
            walletBalancePersistence.creditWalletStandalone(userId, amount)
        except Exception as ex:
            raise Exception(f"WalletBalanceService error: {str(ex)}") from ex
