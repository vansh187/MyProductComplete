"""
Position Service - Builds/updates the positions table from executed fills.
"""

import logging
from decimal import Decimal
from typing import Dict, Any

from appconfig import OptionMaster
from database.positionPersistence import PositionPersistence

logger = logging.getLogger(__name__)

VALID_SIDES = ("BUY", "SELL")


class PositionService:
    """Service class for building/updating positions on executed and partially executed fills."""

    def __init__(self):
        self.position_persistence = PositionPersistence()
        self.logger = logger

    def apply_fill(self, user_id: int, side: str, order_snapshot: Dict[str, Any],
                    quantity: int, price, cursor) -> None:
        """
        Apply one trade fill (from a fully or partially executed order) to the
        user's position row for that instrument - creating the row on the first
        fill, updating it on every fill after.

        netavgprc/buyavgprc/sellavgprc are weighted averages recomputed from the
        position's own running totals (not the order's limit/trigger price).
        Reducing/closing an existing position banks the difference into
        realized_pnl; a fill that fully closes and then reverses the position
        (e.g. selling more than the current long) starts the new side fresh at
        this fill's price.

        Args:
            user_id: User ID for this side of the trade
            side: 'BUY' or 'SELL'
            order_snapshot: dict from OrderService.get_order_snapshot() - must
                contain at least 'symbol'
            quantity: Fill quantity (must be > 0)
            price: Fill execution price (must be > 0)
            cursor: Database cursor (shared with the calling transaction)

        Raises:
            ValueError: If parameters are invalid
        """
        if user_id is None or user_id <= 0:
            self.logger.error(f"apply_fill() received invalid user_id: {user_id}")
            raise ValueError("User ID must be a positive integer")

        if side not in VALID_SIDES:
            self.logger.error(f"apply_fill() received invalid side: {side}")
            raise ValueError(f"Side must be one of {VALID_SIDES}")

        if order_snapshot is None:
            self.logger.error("apply_fill() received None order_snapshot")
            raise ValueError("Order snapshot cannot be None")

        tsym = order_snapshot.get("symbol")
        if not tsym or not tsym.strip():
            self.logger.error("apply_fill() order_snapshot missing symbol")
            raise ValueError("Order snapshot must contain a symbol")

        if quantity is None or quantity <= 0:
            self.logger.error(f"apply_fill() received invalid quantity: {quantity}")
            raise ValueError("Quantity must be a positive integer")

        if price is None or price <= 0:
            self.logger.error(f"apply_fill() received invalid price: {price}")
            raise ValueError("Price must be a positive number")

        if cursor is None:
            self.logger.error("apply_fill() received None cursor")
            raise ValueError("Database cursor cannot be None")

        fill_qty = Decimal(quantity)
        fill_price = Decimal(str(price))

        existing = self.position_persistence.get_position(user_id, tsym, cursor)

        netqty = Decimal(existing["netqty"]) if existing and existing.get("netqty") is not None else Decimal(0)
        netavgprc = Decimal(existing["netavgprc"]) if existing and existing.get("netavgprc") is not None else Decimal(0)
        buyqty = Decimal(existing["buyqty"]) if existing and existing.get("buyqty") is not None else Decimal(0)
        sellqty = Decimal(existing["sellqty"]) if existing and existing.get("sellqty") is not None else Decimal(0)
        buyavgprc = Decimal(existing["buyavgprc"]) if existing and existing.get("buyavgprc") is not None else Decimal(0)
        sellavgprc = Decimal(existing["sellavgprc"]) if existing and existing.get("sellavgprc") is not None else Decimal(0)
        realized_pnl = Decimal(existing["realized_pnl"]) if existing and existing.get("realized_pnl") is not None else Decimal(0)

        if side == "BUY":
            buyavgprc = ((buyqty * buyavgprc) + (fill_qty * fill_price)) / (buyqty + fill_qty)
            buyqty += fill_qty

            if netqty >= 0:
                new_netqty = netqty + fill_qty
                netavgprc = ((netqty * netavgprc) + (fill_qty * fill_price)) / new_netqty
                netqty = new_netqty
            else:
                cover_qty = min(fill_qty, -netqty)
                realized_pnl += cover_qty * (netavgprc - fill_price)
                remaining = fill_qty - cover_qty
                netqty += fill_qty
                if netqty == 0:
                    netavgprc = Decimal(0)
                elif remaining > 0:
                    netavgprc = fill_price  # position flipped long, fresh entry price
                # else: still short at the same avg entry price - unchanged
        else:
            sellavgprc = ((sellqty * sellavgprc) + (fill_qty * fill_price)) / (sellqty + fill_qty)
            sellqty += fill_qty

            if netqty <= 0:
                new_short_qty = -netqty + fill_qty
                netavgprc = ((-netqty * netavgprc) + (fill_qty * fill_price)) / new_short_qty
                netqty -= fill_qty
            else:
                close_qty = min(fill_qty, netqty)
                realized_pnl += close_qty * (fill_price - netavgprc)
                remaining = fill_qty - close_qty
                netqty -= fill_qty
                if netqty == 0:
                    netavgprc = Decimal(0)
                elif remaining > 0:
                    netavgprc = fill_price  # position flipped short, fresh entry price
                # else: still long at the same avg entry price - unchanged

        contract = OptionMaster.find_by_tsym(tsym)

        position = {
            "user_id": user_id,
            "tsym": tsym,
            "broker": order_snapshot.get("broker"),
            "token": order_snapshot.get("token"),
            "exchange": order_snapshot.get("exchange"),
            "underlying": contract["underlying"] if contract else None,
            "expiry": contract["expiry"] if contract else None,
            "strike": contract["strike"] if contract else None,
            "option_type": contract["option_type"] if contract else None,
            "lot_size": order_snapshot.get("lot_size"),
            "product_type": order_snapshot.get("product_type"),
            "source": order_snapshot.get("source"),
            "netqty": int(netqty),
            "netavgprc": netavgprc,
            "buyqty": int(buyqty),
            "sellqty": int(sellqty),
            "buyavgprc": buyavgprc,
            "sellavgprc": sellavgprc,
            "realized_pnl": realized_pnl,
            "status": "OPEN" if netqty != 0 else "CLOSED",
        }

        self.position_persistence.upsert_position(position, cursor)
