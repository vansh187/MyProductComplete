"""
ShoonyaOrderService - the ONLY place that translates our internal order
representation into Shoonya's real NorenApi place_order/modify_order/
cancel_order field names and codes.

Confirmed directly from Shoonya's own ShoonyaApi-py repo (README.md +
api_helper.py + tests/test_place_order.py), NOT guessed - the previous dead
adapter (service/brokerAdapters/shoonya_adapter.py) used field names
(`prctyp`, `trantype`, `prd`) and codes (`"L"`, `"SL-M"`) that do not match
the real API at all (`price_type`, `buy_or_sell`, `product_type`;
`'LMT'`/`'SL-MKT'`) and would have been rejected by the broker on every call.

place_order's real signature (side/product/exchange/tradingsymbol/qty/
price_type/price/trigger_price/retention/remarks), and its response shape
(`{"stat": "Ok"/"Not_Ok", "norenordno": "...", "emsg": "..."}`), come
straight from that repo - no invented field names here.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# BUY/SELL -> Shoonya's buy_or_sell code.
_SIDE_MAP = {"BUY": "B", "SELL": "S"}

# Our ProductType (MIS/CNC/NRML) -> Shoonya's product_type code.
_PRODUCT_TYPE_MAP = {"CNC": "C", "NRML": "M", "MIS": "I"}

# Our OrderType (MARKET/LIMIT/STOP/STOPLIMIT) -> Shoonya's price_type code.
# STOP/STOPLIMIT map to Shoonya's own native stop-loss order types - the
# REAL exchange handles the trigger for these, not our internal
# StopOrderTriggerService (which continues to serve equity/simulated orders
# only - see service/stopOrderTriggerService.py).
_PRICE_TYPE_MAP = {"MARKET": "MKT", "LIMIT": "LMT", "STOP": "SL-MKT", "STOPLIMIT": "SL-LMT"}


class ShoonyaOrderMappingError(Exception):
    """Raised when an order field can't be mapped to a Shoonya code - a
    programming error (an enum value this map doesn't know about), not a
    broker rejection. Must never reach the broker with a guessed/default
    code for money-moving fields like side or quantity direction."""


class ShoonyaOrderService:
    """Thin wrapper around the authenticated NorenApi-derived client
    (app.state.shoonya._api) - no business logic, no DB access, purely the
    correctness-critical field/code translation layer. One instance per
    request is fine - it holds no state beyond the client reference."""

    def __init__(self, shoonya_api):
        if shoonya_api is None:
            raise ValueError("shoonya_api cannot be None - caller must confirm a live session first")
        self._api = shoonya_api
        self.logger = logger

    @staticmethod
    def _map(mapping: dict, value: str, field_name: str) -> str:
        code = mapping.get(value)
        if code is None:
            raise ShoonyaOrderMappingError(f"No Shoonya mapping for {field_name}={value!r}")
        return code

    def place_order(self, *, side: str, product_type: str, exchange: str, tradingsymbol: str,
                     quantity: int, order_type: str, price: Optional[float] = None,
                     trigger_price: Optional[float] = None, remarks: Optional[str] = None) -> Dict[str, Any]:
        """
        Places a real order on Shoonya. Returns the raw response dict -
        callers must check `response.get("stat") == "Ok"` and read
        `response.get("norenordno")` on success, `response.get("emsg")` on
        failure. Never raises for a broker-level rejection (that's a normal
        `stat: Not_Ok` response, not an exception) - only raises for a
        mapping error (our own bug) or if the underlying HTTP call itself
        blows up (network error, auth expired, etc.), which the caller
        (LiveOrderRoutingService) is responsible for catching.
        """
        buy_or_sell = self._map(_SIDE_MAP, side, "side")
        product_code = self._map(_PRODUCT_TYPE_MAP, product_type, "product_type")
        price_type = self._map(_PRICE_TYPE_MAP, order_type, "order_type")

        # MARKET orders must send price=0 (a real limit price on a market
        # order is meaningless to the broker and rejected by some brokers as
        # malformed); STOP (SL-MKT) similarly has no limit price of its own.
        effective_price = float(price) if price is not None and price_type in ("LMT", "SL-LMT") else 0.0
        effective_trigger = float(trigger_price) if trigger_price is not None else None

        self.logger.info(
            f"Placing Shoonya order: side={buy_or_sell}, product={product_code}, "
            f"exchange={exchange}, tsym={tradingsymbol}, qty={quantity}, "
            f"price_type={price_type}, price={effective_price}, trigger={effective_trigger}"
        )

        return self._api.place_order(
            buy_or_sell=buy_or_sell,
            product_type=product_code,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            quantity=quantity,
            discloseqty=0,
            price_type=price_type,
            price=effective_price,
            trigger_price=effective_trigger,
            retention="DAY",
            remarks=remarks or "primepip",
        )

    def modify_order(self, *, orderno: str, exchange: str, tradingsymbol: str,
                      newquantity: Optional[int] = None, newprice_type: Optional[str] = None,
                      newprice: Optional[float] = None, newtrigger_price: Optional[float] = None) -> Dict[str, Any]:
        """Modifies a resting real order. newprice_type, if given, must
        already be a Shoonya code (e.g. from _PRICE_TYPE_MAP) - callers pass
        our OrderType strings through _map themselves if needed; kept
        separate from place_order's mapping since modify only changes a
        subset of fields."""
        kwargs = {"exchange": exchange, "tradingsymbol": tradingsymbol, "orderno": orderno}
        if newquantity is not None:
            kwargs["newquantity"] = newquantity
        if newprice_type is not None:
            kwargs["newprice_type"] = newprice_type
        if newprice is not None:
            kwargs["newprice"] = float(newprice)
        if newtrigger_price is not None:
            kwargs["newtrigger_price"] = float(newtrigger_price)
        return self._api.modify_order(**kwargs)

    def cancel_order(self, orderno: str) -> Dict[str, Any]:
        return self._api.cancel_order(orderno=orderno)

    def get_order_book(self) -> Any:
        return self._api.get_order_book()

    def single_order_history(self, orderno: str) -> Any:
        return self._api.single_order_history(orderno=orderno)

    def get_positions(self) -> Any:
        return self._api.get_positions()

    def get_limits(self) -> Any:
        return self._api.get_limits()
