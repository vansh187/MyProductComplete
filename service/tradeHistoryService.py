from database.tradeHistoryPersistence import TradeHistoryPersistence as tradeHist
from productdto.matchingOrderDTO import MatchingOrderDTO


class TradeHistoryService:

    def insertTradeOrders(self, buy_order_id,
                            sell_order_id,
                            buy_user_id,
                            sell_user_id,
                            symbol,
                            quantity,
                            execution_price,
                            trade_value,
                            cursor,
                            user_id,
                            side):

       return tradeHist.insertTradeHistoryOrders(buy_order_id,
                            sell_order_id,
                            buy_user_id,
                            sell_user_id,
                            symbol,
                            quantity,
                            execution_price,
                            trade_value,
                            cursor,
                            user_id,
                            side)
        

    def getTradeOrdersById(self, userId):
        return tradeHist.getTradeOrdersById(userId)

    def getFillStats(self, order_id, cursor):
        return tradeHist.getFillStatsByOrderId(order_id, cursor)
