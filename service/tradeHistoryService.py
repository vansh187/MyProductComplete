from database.tradeHistoryPersistence import TradeHistoryPersistence as tradeHist
from productdto.matchingOrderDTO import MatchingOrderDTO


class TradeHistoryService:

    def insertTradeOrders(  buy_order_id,
                            sell_order_id,
                            buy_user_id,
                            sell_user_id,
                            symbol,
                            quantity,
                            execution_price,
                            trade_value,
                            cursor):
        
        
       return tradeHist.insertTradeHistoryOrders(buy_order_id,
                            sell_order_id,
                            buy_user_id,
                            sell_user_id,
                            symbol,
                            quantity,
                            execution_price,
                            trade_value,
                            cursor)
        

    def getTradeOrdersById(userId):
        return tradeHist.getTradeOrdersById(userId)    