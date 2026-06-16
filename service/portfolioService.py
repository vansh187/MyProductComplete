from database.portfolioPersistence import portfolioPersistence
from productdto.portfolioDTO import PortfolioDTO
from productdto.holdingDTO import HoldingDTO

class portfolioService:

    def __init__(self):
        pass
    
    def process_buyer(userId,symbol,quantity,price,cursor):
        return portfolioPersistence.process_buyer(userId,symbol,quantity,price,cursor)

   
    def process_seller(userId,symbol,quantity,price,cursor):
        return portfolioPersistence.process_seller(userId,symbol,quantity,price,cursor)

    @staticmethod
    def createUserHolding(order,userId):
        portfolioPersistence.createUserHolding(order,userId)

    @staticmethod
    def updateUserHoldings(order,userId,new_qty,round,new_avg):
        portfolioPersistence.updateUserHoldings(order,userId,new_qty,round,new_avg)

    @staticmethod
    def getPortfolioServiceforLoggedInUser(userId):
        return portfolioPersistence.getPortfolioServiceforLoggedInUser(userId)


    @staticmethod
    def getPortfolioOfLoggedInUserWithProfitLoss(userId):

        userHoldings = portfolioPersistence.getPortfolioOfLoggedInUserWithProfitLoss(userId)
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
                        pnl=pnl
                    )
                )
            
            return PortfolioDTO(user_id=userId,
            total_pnl=total_pnl,
            holdings=holdings)
        return None
    
    
    def createTradeinOrderBook(self,conn,cursor,order,userId,orderId):
        return portfolioPersistence.createTradeinOrderBook(conn,cursor,order,userId,orderId)
    
    
    
    
    def updateOrderBookQuantity(self,quantity,orderBookId,status,buy_order_id, sell_order_id,cursor):
        return portfolioPersistence.updateOrderBookQuantity(quantity,orderBookId,status,buy_order_id, sell_order_id,cursor)
        