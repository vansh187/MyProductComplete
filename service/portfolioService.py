from database.portfolioPersistence import portfolioPersistence
from productdto.portfolioDTO import PortfolioDTO
from productdto.holdingDTO import HoldingDTO
from utils.assetBuckets import ASSET_TYPE_BUCKETS

class portfolioService:

    def __init__(self):
        self.portfolioPersistence = portfolioPersistence()

    def process_buyer(self, userId, symbol, quantity, price, cursor):
        return self.portfolioPersistence.process_buyer(userId, symbol, quantity, price, cursor)

    def process_seller(self, userId, symbol, quantity, price, cursor):
        return self.portfolioPersistence.process_seller(userId, symbol, quantity, price, cursor)

    def updateCounterpartyOrderBook(self, remaining_qty, status, counterparty_order_id, cursor):
        return self.portfolioPersistence.updateCounterpartyOrderBook(remaining_qty, status, counterparty_order_id, cursor)

    def updateIncomingOrderBook(self, remaining_qty, status, order_book_id, cursor):
        return self.portfolioPersistence.updateIncomingOrderBook(remaining_qty, status, order_book_id, cursor)

    def createTradeinOrderBook(self, conn, cursor, order, userId, orderId):
        return self.portfolioPersistence.createTradeinOrderBook(conn, cursor, order, userId, orderId)

    def createUserHolding(self, order, userId):
        self.portfolioPersistence.createUserHolding(order, userId)

    def updateUserHoldings(self, order, userId, new_qty, round, new_avg):
        self.portfolioPersistence.updateUserHoldings(order, userId, new_qty, round, new_avg)

    def getPortfolioServiceforLoggedInUser(self, userId):
        return self.portfolioPersistence.getPortfolioServiceforLoggedInUser(userId)

    def getPortfolioOfLoggedInUserWithProfitLoss(self, userId, bucket=None):
        if bucket is not None and bucket not in ASSET_TYPE_BUCKETS:
            raise ValueError(f"Unknown bucket: {bucket}")
        asset_types = ASSET_TYPE_BUCKETS.get(bucket) if bucket else None
        userHoldings = self.portfolioPersistence.getPortfolioOfLoggedInUserWithProfitLoss(userId, asset_types)
        if userHoldings is not None:
            holdings = []
            total_pnl = 0
            for indexHolding in userHoldings:
                current_price = indexHolding.get('current_price') or indexHolding.get('avg_price')
                pnl = (current_price - indexHolding['avg_price']) * indexHolding['quantity']
                total_pnl += pnl
                holdings.append(
                    HoldingDTO(
                        symbol=indexHolding['symbol'],
                        quantity=indexHolding['quantity'],
                        avg_price=indexHolding['avg_price'],
                        current_price=current_price,
                        pnl=pnl,
                        asset_type=indexHolding['asset_type']
                    )
                )
            return PortfolioDTO(user_id=userId, total_pnl=total_pnl, holdings=holdings)
        return None
