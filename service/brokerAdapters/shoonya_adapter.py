"""
Shoonya API Adapter - Converts standardized OrderCreate to Shoonya format
No static methods. Clean exception handling. Production-grade code.
"""

import logging
from typing import Dict, Any
from api.models import OrderCreate, OrderType, OrderSide

logger = logging.getLogger(__name__)


class ShonyaAdapter:
    """Converts standardized OrderCreate model to Shoonya API format."""

    def __init__(self):
        """Initialize the Shoonya adapter."""
        self.logger = logger

    def convert(self, order: OrderCreate) -> Dict[str, Any]:
        """
        Convert OrderCreate to Shoonya API format.

        Args:
            order: OrderCreate model instance

        Returns:
            Dict with Shoonya-formatted order parameters

        Raises:
            ValueError: If order validation fails
            TypeError: If order is None or invalid type
        """
        if order is None:
            self.logger.error("ShonyaAdapter.convert() received None order")
            raise TypeError("Order cannot be None")

        if not isinstance(order, OrderCreate):
            self.logger.error(f"Invalid order type: {type(order)}")
            raise TypeError(f"Expected OrderCreate, got {type(order)}")

        try:
            # Map order type
            prctyp = self._map_order_type(order.order_type)

            # Map side
            trantype = self._map_side(order.side)

            # Map symbol with exchange suffix
            tsym = self._map_symbol(order.symbol, order.exchange.value)

            # Build Shoonya order payload
            shoonya_order = {
                "exch": order.exchange.value,
                "tsym": tsym,
                "qty": order.quantity,
                "price": float(order.price) if order.price else 0.0,
                "prctyp": prctyp,
                "trantype": trantype,
                "prd": order.product_type.value,
                "retention": order.validity.value,
                "orderType": "Regular",
                "amo": "No"
            }

            # Add trigger price for STOP/STOPLIMIT orders
            if order.trigger_price is not None:
                shoonya_order["trgprc"] = float(order.trigger_price)

            self.logger.info(f"Successfully converted order {order.symbol} to Shoonya format")
            return shoonya_order

        except (AttributeError, KeyError, ValueError, TypeError) as e:
            self.logger.error(f"Error converting order to Shoonya format: {str(e)}")
            raise ValueError(f"Order conversion failed: {str(e)}") from e
        except Exception as e:
            self.logger.error(f"Unexpected error in convert(): {str(e)}")
            raise

    def _map_order_type(self, order_type: OrderType) -> str:
        """
        Map OrderType enum to Shoonya order type code.

        Args:
            order_type: OrderType enum value

        Returns:
            Shoonya order type code

        Raises:
            ValueError: If order_type is invalid
        """
        if order_type is None:
            raise ValueError("Order type cannot be None")

        type_mapping = {
            OrderType.MARKET: "MKT",
            OrderType.LIMIT: "L",
            OrderType.STOP: "SL",
            OrderType.STOPLIMIT: "SL-M"
        }

        result = type_mapping.get(order_type)
        if result is None:
            raise ValueError(f"Unknown order type: {order_type}")

        return result

    def _map_side(self, side: OrderSide) -> str:
        """
        Map OrderSide enum to Shoonya side code.

        Args:
            side: OrderSide enum value

        Returns:
            Shoonya side code (B or S)

        Raises:
            ValueError: If side is invalid
        """
        if side is None:
            raise ValueError("Side cannot be None")

        side_mapping = {
            OrderSide.BUY: "B",
            OrderSide.SELL: "S"
        }

        result = side_mapping.get(side)
        if result is None:
            raise ValueError(f"Unknown side: {side}")

        return result

    def _map_symbol(self, symbol: str, exchange: str) -> str:
        """
        Convert symbol and exchange to Shoonya format.

        Args:
            symbol: Stock symbol (e.g., "RELIANCE")
            exchange: Exchange code (e.g., "NSE")

        Returns:
            Shoonya symbol format (e.g., "RELIANCE-EQ")

        Raises:
            ValueError: If symbol or exchange is invalid
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError("Symbol must be a non-empty string")

        if not exchange or not isinstance(exchange, str):
            raise ValueError("Exchange must be a non-empty string")

        symbol = symbol.upper().strip()

        # Suffix mapping for different exchanges
        suffix_map = {
            "NSE": "-EQ",
            "BSE": "-BE",
            "NFO": "",
            "NCDEX": "",
            "MCXSX": ""
        }

        suffix = suffix_map.get(exchange, "")
        if suffix is None:
            raise ValueError(f"Unknown exchange: {exchange}")

        return f"{symbol}{suffix}"
