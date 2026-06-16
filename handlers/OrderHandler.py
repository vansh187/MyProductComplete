class OrderHandler:

    def __init__(self, order_service):
        self.order_service = order_service

    def handle_trade_executed(self, payload):
        self.order_service.updateStatus(
            payload["user_id"],
            payload["symbol"],
            "EXECUTED",
            payload["order_id"],
            payload["buy_order_id"] if "buy_order_id" in payload else None,
            payload["sell_order_id"] if "sell_order_id" in payload else None,
        )