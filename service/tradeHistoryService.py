from database.tradeHistoryPersistence import TradeHistoryPersistence as tradeHist



class TradeHistoryService:

    def insertTradeOrders(orderId,userId, symbol,side ,quantity,price):
       return tradeHist.insertTradeHistoryOrders(orderId,userId,symbol, side,quantity,price)
        
        