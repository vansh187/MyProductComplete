import json
from pathlib import Path
from decimal import Decimal

from database.positionspersistence.PositionsPersistence import PositionsPersistence
from utils.fnoSymbolParser import parse_fno_symbol

_LOT_SIZE_CONFIG_PATH = Path(__file__).parent.parent / "appconfig" / "fno_lot_sizes.json"

# Loaded once at import time rather than per PositionsService() instantiation
# (this class is constructed per-request in executionEngine.py's trade loop).
with open(_LOT_SIZE_CONFIG_PATH, "r") as _fh:
    _raw_lot_sizes = json.load(_fh)
_LOT_SIZES = {k: v for k, v in _raw_lot_sizes.items() if not k.startswith("_")}


class PositionsService:

    def __init__(self):
        self.positionsPersistence = PositionsPersistence()

    def _lotSizeFor(self, underlying):
        if underlying is None:
            return None
        return _LOT_SIZES.get(underlying)

    def applyFill(self, userId, tsym, side, quantity, price, product_type, exchange, cursor):
        """
        Applies one F&O fill to the user's position row for (tsym,
        product_type), using the caller's shared cursor/transaction (matches
        portfolioPersistence.process_buyer's shared-cursor convention, since
        this must commit atomically with the rest of trade processing).

        realized_pnl is an incremental running total (added to on every fill
        that reduces exposure), not recomputed from lifetime buy/sell totals -
        that avoids blending a prior closed cycle's average price into a
        freshly reopened position (buyqty/sellqty/buyavgprc/sellavgprc are
        still accumulated lifetime-to-date, but purely as informational
        stats; they no longer feed the realized_pnl calculation).
        """
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValueError(f"Invalid quantity for F&O fill: {quantity}")
        if price is None or not isinstance(price, (int, float, Decimal)) or price <= 0:
            raise ValueError(f"Invalid price for F&O fill: {price}")
        if side not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side for F&O fill: {side}")

        price = Decimal(str(price))
        existing = self.positionsPersistence.getPositionForUpdate(userId, tsym, product_type, cursor)

        old_netqty = existing["netqty"] if existing is not None else 0
        old_netavgprc = existing["netavgprc"] if existing is not None else Decimal("0")
        old_buyqty = existing["buyqty"] if existing is not None else 0
        old_buyavgprc = existing["buyavgprc"] if existing is not None else Decimal("0")
        old_sellqty = existing["sellqty"] if existing is not None else 0
        old_sellavgprc = existing["sellavgprc"] if existing is not None else Decimal("0")
        realized_pnl = existing["realized_pnl"] if existing is not None else Decimal("0")

        signed_qty = quantity if side == "BUY" else -quantity

        if old_netqty == 0 or (old_netqty > 0) == (signed_qty > 0):
            # Starting flat (fresh position or reopened after a full close)
            # or adding to the existing exposure - no P&L to realize yet,
            # just blend the cost basis.
            new_netqty = old_netqty + signed_qty
            netavgprc = ((abs(old_netqty) * old_netavgprc) + (quantity * price)) / abs(new_netqty)
        else:
            # Opposite direction - closes existing exposure, fully or
            # partially, and may flip it to the other side.
            closing_qty = min(quantity, abs(old_netqty))
            if old_netqty > 0:
                realized_pnl += closing_qty * (price - old_netavgprc)
            else:
                realized_pnl += closing_qty * (old_netavgprc - price)

            new_netqty = old_netqty + signed_qty
            remaining_fill_qty = quantity - closing_qty
            if new_netqty == 0:
                netavgprc = Decimal("0")
            elif remaining_fill_qty > 0:
                netavgprc = price  # flipped direction - new fill sets the new cost basis
            else:
                netavgprc = old_netavgprc  # partial close, same direction - cost basis unchanged

        if side == "BUY":
            buyqty = old_buyqty + quantity
            buyavgprc = ((old_buyqty * old_buyavgprc) + (quantity * price)) / buyqty
            sellqty, sellavgprc = old_sellqty, old_sellavgprc
        else:
            sellqty = old_sellqty + quantity
            sellavgprc = ((old_sellqty * old_sellavgprc) + (quantity * price)) / sellqty
            buyqty, buyavgprc = old_buyqty, old_buyavgprc

        status = "OPEN" if new_netqty != 0 else "CLOSED"

        if existing is None:
            parsed = parse_fno_symbol(tsym)
            self.positionsPersistence.insertPosition(
                userId, tsym,
                None,   # broker - internally matched, not a live broker fill
                None,   # token - no scrip master lookup available
                exchange,
                parsed["underlying"], parsed["expiry"], parsed["strike"], parsed["option_type"],
                self._lotSizeFor(parsed["underlying"]),
                product_type,
                "INTERNAL_MATCH",
                new_netqty, netavgprc, buyqty, sellqty, buyavgprc, sellavgprc,
                realized_pnl, status, parsed["contract_type"],
                cursor
            )
        else:
            self.positionsPersistence.updatePosition(
                userId, tsym, product_type, new_netqty, netavgprc, buyqty, sellqty,
                buyavgprc, sellavgprc, realized_pnl, status, cursor
            )

    def getPositionsForUser(self, userId):
        return self.positionsPersistence.getPositionsForUser(userId)

    def getPositionsSummaryForUser(self, userId):
        return self.positionsPersistence.getPositionsSummaryForUser(userId)

    def getDistinctUsersWithPositions(self):
        return self.positionsPersistence.getDistinctUsersWithPositions()
