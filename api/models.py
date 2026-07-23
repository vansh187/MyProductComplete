"""
API Models and Enums
Separated from orders.py to avoid circular imports
"""

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
