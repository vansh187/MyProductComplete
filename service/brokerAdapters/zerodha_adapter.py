"""
Zerodha Kite API Adapter - Converts standardized OrderCreate to Zerodha format
No static methods. Clean exception handling. Production-grade code.
"""

import logging
from typing import Dict, Any
from api.models import OrderCreate, OrderType, OrderSide

logger = logging.getLogger(__name__)


class ZerodhaAdapter:
    """Converts standardized OrderCreate model to Zerodha Kite API format."""

    def __init__(self):
        """Initialize the Zerodha adapter."""
        self.logger = logger

    def convert(self, order: OrderCreate) -> Dict[str, Any]:
        """
        Convert OrderCreate to Zerodha Kite API format.

        Args:
            order: OrderCreate model instance

        Returns:
            Dict with Zerodha-formatted order parameters

        Raises:
            ValueError: If order validation fails
            TypeError: If order is None or invalid type
        """
        if order is None:
            self.logger.error("ZerodhaAdapter.convert() received None order")
            raise TypeError("Order cannot be None")

        if not isinstance(order, OrderCreate):
            self.logger.error(f"Invalid order type: {type(order)}")
            raise TypeError(f"Expected OrderCreate, got {type(order)}")

        try:
            # Build Zerodha order payload
            zerodha_order = {
                "tradingsymbol": order.symbol.upper().strip(),
                "exchange": order.exchange.value,
                "transaction_type": order.side.value,
                "quantity": order.quantity,
                "order_type": order.order_type.value,
                "product": order.product_type.value,
                "validity": order.validity.value,
                "variety": "regular"
            }

            # Add price only if specified and not MARKET order
            if order.price is not None and order.order_type != OrderType.MARKET:
                zerodha_order["price"] = float(order.price)

            # Add trigger price for STOP/STOPLIMIT orders
            if order.trigger_price is not None:
                zerodha_order["trigger_price"] = float(order.trigger_price)

            self.logger.info(f"Successfully converted order {order.symbol} to Zerodha format")
            return zerodha_order

        except (AttributeError, KeyError, ValueError, TypeError) as e:
            self.logger.error(f"Error converting order to Zerodha format: {str(e)}")
            raise ValueError(f"Order conversion failed: {str(e)}") from e
        except Exception as e:
            self.logger.error(f"Unexpected error in convert(): {str(e)}")
            raise

    def validate_order_for_zerodha(self, order: OrderCreate) -> bool:
        """
        Validate order compatibility with Zerodha requirements.

        Args:
            order: OrderCreate model instance

        Returns:
            True if order is valid for Zerodha

        Raises:
            ValueError: If order validation fails
        """
        if order is None:
            raise ValueError("Order cannot be None")

        # Zerodha specific validations
        if order.order_type == OrderType.MARKET and order.price is not None:
            self.logger.warning(f"MARKET order {order.symbol} has price specified, will be ignored")

        if order.order_type == OrderType.STOP and order.trigger_price is None:
            raise ValueError("STOP orders require trigger_price")

        if order.order_type == OrderType.STOPLIMIT:
            if order.price is None or order.trigger_price is None:
                raise ValueError("STOPLIMIT orders require both price and trigger_price")

        if order.quantity <= 0:
            raise ValueError("Quantity must be greater than 0")

        if order.symbol is None or not order.symbol.strip():
            raise ValueError("Symbol cannot be empty")

        return True
