from matchingEnginePersistence import matchingEnginePersistence
from productdto import matchingOrderDTO as MatchingOrderDTO
from datetime import datetime
from decimal import Decimal


class MatchingEngineService:

    def price_match(self, incomingOrder, matchedOrder):

        incoming_side = incomingOrder["side"].upper()
        matched_side = matchedOrder["side"].upper()

        incoming_price = Decimal(str(incomingOrder["price"]))
        matched_price = Decimal(str(matchedOrder["price"]))

        if incoming_side == "BUY" and matched_side == "SELL":
            return incoming_price >= matched_price

        elif incoming_side == "SELL" and matched_side == "BUY":
            return incoming_price <= matched_price

        return False


    def matchtradeOrderforUser(self, order, userId, status):

        matchFoundOrderResponse = matchingEnginePersistence.matchtradeOrderforUser(
            order, userId, status
        )

        if matchFoundOrderResponse is None:
            return {
                "userId": userId,
                "message": "No MatchFound Order still in order Book"
            }

        trades = self.matchingOrder(order, matchFoundOrderResponse)

        return {
            "userId": userId,
            "message": trades
        }


    def matchingOrder(self, incomingOrder, matchFoundOrderResponse):

        trades = []

        try:
            # FIX 1: safe access
            incoming_qty = incomingOrder.get("remaining_quantity", 0)

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

                execution_price = Decimal(str(response.get("price", 0)))

                # Decide BUY/SELL mapping
                if incomingOrder["side"].upper() == "BUY":
                    buy_order_id = incomingOrder["id"]
                    sell_order_id = response["id"]
                    buy_user_id = incomingOrder["user_id"]
                    sell_user_id = response["user_id"]
                else:
                    buy_order_id = response["id"]
                    sell_order_id = incomingOrder["id"]
                    buy_user_id = response["user_id"]
                    sell_user_id = incomingOrder["user_id"]

                trades.append(
                    MatchingOrderDTO(
                        symbol=incomingOrder["symbol"],
                        buy_order_id=buy_order_id,
                        sell_order_id=sell_order_id,
                        buy_user_id=buy_user_id,
                        sell_user_id=sell_user_id,
                        quantity=trade_qty,
                        execution_price=execution_price,
                        trade_value=execution_price * Decimal(trade_qty),
                        executed_at=datetime.now()
                    )
                )

                # FIX 3: IMPORTANT state updates (was missing)
                incoming_qty -= trade_qty
                response["remaining_quantity"] = opp_remaining_qty - trade_qty

            # update incoming order state
            incomingOrder["remaining_quantity"] = incoming_qty

            if incoming_qty == 0:
                incomingOrder["status"] = "EXECUTED"
            else:
                incomingOrder["status"] = "PARTIALLY_EXECUTED"

        except Exception as ex:
            # FIX 4: don't hide real error
            raise Exception(f"Exception in matching trade with incoming order: {str(ex)}")

        return trades