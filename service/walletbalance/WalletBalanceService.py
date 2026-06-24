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
