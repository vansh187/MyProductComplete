class PortfolioHandler:

    def __init__(self, portfolio_service):
        self.portfolio_service = portfolio_service

    def handle_trade_executed(self, payload):
        self.portfolio_service.update_position(
            user_id=payload["user_id"],
            symbol=payload["symbol"],
            qty=payload["qty"],
            price=payload["price"]
        )