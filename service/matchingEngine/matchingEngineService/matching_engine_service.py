from service.matchingEngine.matchingEnginePersistence.matchingEnginePersistence import matchtradeOrderforUser as MatchingPersistence
from productdto.matchingOrderDTO import MatchingOrderDTO
from datetime import datetime
from decimal import Decimal


class MatchingEngineService:

    def __init__(self,order,userId,cursor):
        self.order=order
        self.userId=userId
        self.cursor=cursor
    
    
    
    def price_match( self,incomingOrder, matchedOrder):

        incoming_side = incomingOrder.side.upper()
        matched_side = matchedOrder["side"].upper()

        incoming_price = Decimal(str(incomingOrder.price))
        matched_price = Decimal(str(matchedOrder["price"]))

        if incoming_side == "BUY" and matched_side == "SELL":
            return incoming_price >= matched_price

        elif incoming_side == "SELL" and matched_side == "BUY":
            return incoming_price <= matched_price

        return False


   
    def matchtradeOrderforUser( self,order, userId, status,cursor):

        matchingPersistence=MatchingPersistence()
        matchFoundOrderResponse = matchingPersistence.matchtradingOrderforUser( order,status,cursor)

        if matchFoundOrderResponse is None:
            return {
                "userId": userId,
                "message": "No MatchFound Order still in order Book"
            }

        trades = self.matchingOrder(order, matchFoundOrderResponse,userId)

        return {
            "userId": userId,
            "tradeExcecution": trades
        }



    def matchingOrder( self,incomingOrder, matchFoundOrderResponse,userId):

        trades = []

        try:
            # FIX 1: safe access
            if len(matchFoundOrderResponse)>0 :
                incoming_qty = incomingOrder.quantity

                for response in matchFoundOrderResponse:

                    if incoming_qty == 0:
                        break

                    if not self.price_match(incomingOrder, response):
                        continue

                    # FIX 2: safe access for DB rows
                    opp_remaining_qty = response.get("remaining_quantity", 0)
                    if opp_remaining_qty <= 0:
                        continue

                    trade_qty = min(incoming_qty, opp_remaining_qty)
                    remainingQty=opp_remaining_qty-trade_qty
                    #opp one is which is in order book and incoming one is order from person
                    execution_price = Decimal(str(response.get("price", 0)))

                    # Decide BUY/SELL mapping
                    if incomingOrder.side.upper() == "BUY":
                        buy_order_id = incomingOrder.id
                        sell_order_id = response["order_id"]
                        buy_user_id = userId
                        sell_user_id = response["user_id"]
                    else:
                        buy_order_id = response["order_id"]
                        sell_order_id = incomingOrder.id
                        buy_user_id = response["user_id"]
                        sell_user_id = userId

                    trades.append(
                        MatchingOrderDTO(
                            symbol=incomingOrder.symbol,
                            buy_order_id=buy_order_id,
                            sell_order_id=sell_order_id,
                            buy_user_id=buy_user_id,
                            sell_user_id=sell_user_id,
                            quantity=trade_qty,
                            execution_price=execution_price,
                            trade_value=execution_price * Decimal(trade_qty),
                            executed_at=datetime.now(),
                            remaining_qty=remainingQty
                        )
                    )

                    # FIX 3: IMPORTANT state updates (was missing)
                    incoming_qty -= trade_qty
                    response["remaining_quantity"] = opp_remaining_qty - trade_qty

                # update incoming order state
                incomingOrder.remainingQty = incoming_qty

                if incoming_qty == 0:
                    incomingOrder.status = "EXECUTED"
                else:
                    incomingOrder.status = "PARTIALLY_EXECUTED"

        except Exception as ex:
                # FIX 4: don't hide real error
            raise Exception(f"Exception in matching trade with incoming order: {str(ex)}")

        return trades