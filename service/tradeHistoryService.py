from database.tradeHistoryPersistence import TradeHistoryPersistence as tradeHist



class TradeHistoryService:

    def insertTradeOrders(orderId,userId, symbol,side ,quantity,price,cursor):
       return tradeHist.insertTradeHistoryOrders(orderId,userId,symbol, side,quantity,price,cursor)
        

    def getTradeOrdersById(userId):
        return tradeHist.getTradeOrdersById(userId)    