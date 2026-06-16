class TradeHandler:

    def __init__(self, trade_service):
        self.trade_service = trade_service

    def handle_trade_executed(self, payload):
        self.trade_service.save_trade(
            trade_id=payload["trade_id"],
            user_id=payload["user_id"],
            symbol=payload["symbol"],
            qty=payload["qty"],
            price=payload["price"]
        )