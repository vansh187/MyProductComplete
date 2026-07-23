"""
API Models and Enums
Separated from orders.py to avoid circular imports
"""

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


# ============================================
# ENUM DEFINITIONS
# ============================================
class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOPLIMIT = "STOPLIMIT"


# Single source of truth for "does this order type start dormant
# (PENDING_TRIGGER) instead of immediately matchable (PENDING)?" - see
# service/stopOrderTriggerService.py. Previously this membership test was
# duplicated verbatim across database/orderPersistence.py,
# database/portfolioPersistence.py, and service/executionEngine.py; a future
# order type added to only some of those copies could desync orders.status
# from order_book.status, or leave a dormant order matchable at creation.
DORMANT_UNTIL_TRIGGERED_ORDER_TYPES = (OrderType.STOP.value, OrderType.STOPLIMIT.value)


def order_type_str(order_type) -> str:
    """Normalizes an OrderType enum member OR a plain string to its string
    value - the same `x.value if hasattr(x, "value") else str(x)` coercion
    duplicated across the order-creation/trigger call chain, centralized
    here so any future fix to the coercion applies everywhere at once."""
    return order_type.value if hasattr(order_type, "value") else str(order_type)


def is_dormant_order_type(order_type) -> bool:
    """True for STOP/STOPLIMIT - see DORMANT_UNTIL_TRIGGERED_ORDER_TYPES."""
    return order_type_str(order_type) in DORMANT_UNTIL_TRIGGERED_ORDER_TYPES


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ProductType(str, Enum):
    MIS = "MIS"
    CNC = "CNC"
    NRML = "NRML"


class ValidityType(str, Enum):
    DAY = "DAY"
    IOC = "IOC"
    TTL = "TTL"
    GTC = "GTC"


class ExchangeType(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"
    BFO = "BFO"
    NCDEX = "NCDEX"
    MCXSX = "MCXSX"


# Derivative exchanges routed to the F&O positions book (service/positionsService.py)
# instead of equity holdings - NSE/BSE are the only cash-equity exchanges.
DERIVATIVE_EXCHANGES = {ExchangeType.NFO, ExchangeType.NCDEX, ExchangeType.MCXSX}


# ============================================
# ORDER CREATE MODEL
# ============================================
class OrderCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=50, description="Stock symbol (up to 50 chars for derivatives like NIFTY07JUL2623800CE)")
    exchange: ExchangeType = Field(default=ExchangeType.NSE, description="Stock exchange")
    side: OrderSide = Field(..., description="BUY or SELL")
    quantity: int = Field(..., gt=0, description="Order quantity (must be > 0)")

    order_type: OrderType = Field(default=OrderType.MARKET, description="Order type")
    price: Optional[float] = Field(default=None, gt=0, description="Order price (required for LIMIT/STOPLIMIT)")
    trigger_price: Optional[float] = Field(default=None, gt=0, description="Trigger price (required for STOP/STOPLIMIT)")

    product_type: ProductType = Field(default=ProductType.MIS, description="Trading product type")
    validity: ValidityType = Field(default=ValidityType.DAY, description="Order validity")

    client_order_id: Optional[str] = Field(default=None, max_length=100, description="Client order ID for tracking")
    notes: Optional[str] = Field(default=None, max_length=500, description="Order notes")

    broker: Optional[str] = Field(default=None, max_length=20, description="Broker name (set by backend)")
    source: Optional[str] = Field(default=None, max_length=20, description="Order source (set by backend)")
    token: Optional[str] = Field(default=None, max_length=20, description="Instrument token (set by backend)")
    filled_qty: Optional[int] = Field(default=None, description="Filled quantity (set by backend)")
    lot_size: Optional[int] = Field(default=None, description="Lot size (set by backend)")
    avg_fill_price: Optional[float] = Field(default=None, description="Average fill price (set by backend)")
    updated_at: Optional[datetime] = Field(default=None, description="Last update timestamp (set by backend)")

    @field_validator('price')
    @classmethod
    def validate_price_for_limit_orders(cls, v, info):
        """Ensure LIMIT and STOPLIMIT orders have price"""
        order_type = info.data.get('order_type')
        if order_type in [OrderType.LIMIT, OrderType.STOPLIMIT] and v is None:
            raise ValueError(f'{order_type} orders require a price')
        return v

    @field_validator('trigger_price')
    @classmethod
    def validate_trigger_price_for_stop_orders(cls, v, info):
        """Ensure STOP and STOPLIMIT orders have trigger_price"""
        order_type = info.data.get('order_type')
        if order_type in [OrderType.STOP, OrderType.STOPLIMIT] and v is None:
            raise ValueError(f'{order_type} orders require a trigger_price')
        return v

    @field_validator('quantity')
    @classmethod
    def validate_quantity(cls, v):
        """Validate quantity is positive"""
        if v <= 0:
            raise ValueError('Quantity must be greater than 0')
        return v

    @field_validator('symbol')
    @classmethod
    def validate_symbol(cls, v):
        """Validate symbol is not empty"""
        if not v or not v.strip():
            raise ValueError('Symbol cannot be empty')
        return v.upper().strip()

    class Config:
        use_enum_values = False
        json_schema_extra = {
            "example": {
                "symbol": "RELIANCE",
                "exchange": "NSE",
                "side": "BUY",
                "quantity": 10,
                "order_type": "LIMIT",
                "price": 2450.50,
                "product_type": "MIS",
                "validity": "DAY"
            }
        }


# ============================================
# ORDER MODIFY MODEL
# ============================================
class OrderModify(BaseModel):
    """Ticket 15: amend a resting (PENDING/PENDING_TRIGGER) order's
    price/quantity/trigger_price in place, keeping the same order_id. Every
    field is optional - only fields actually sent are changed - but at least
    one must be provided, or there is nothing to modify."""
    price: Optional[float] = Field(default=None, gt=0, description="New order price")
    quantity: Optional[int] = Field(default=None, gt=0, description="New order quantity")
    trigger_price: Optional[float] = Field(default=None, gt=0, description="New trigger price (STOP/STOPLIMIT only)")
