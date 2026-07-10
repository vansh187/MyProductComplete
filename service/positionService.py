"""
Position Service - Builds/updates open positions in Redis from executed
fills. Postgres is written on the fill that first opens a position (full or
partial) and again on the fill that closes it - every fill in between, and
every live-tick price update, touches only the Redis cache.
"""

import logging
import time
from decimal import Decimal
from typing import Dict, Any

from appconfig import OptionMaster
from database.positionCache import CACHE_SCHEMA_VERSION, PositionCache
from database.positionPersistence import PositionPersistence
from service.positionTickService import positionTickService

logger = logging.getLogger(__name__)

VALID_SIDES = ("BUY", "SELL")


class PositionService:
    """Service class for building/updating positions on executed and partially executed fills."""

    def __init__(self):
        self.position_persistence = PositionPersistence()
        self.position_cache = PositionCache()
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
        # Canonicalize before it's used as the cache hash field / DB conflict
        # key - the order API's own validator already does this (see
        # api/models.py OrderCreate.validate_symbol), but apply_fill() must
        # not depend on every caller upstream (seed scripts, admin tools,
        # future integrations) doing the same. Two fills for the same real
        # contract that differ only in case/whitespace would otherwise land
        # in two separate cache entries - a "duplicate position" a user has
        # no way to reconcile against their own trade.
        tsym = tsym.strip().upper()

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

        # Open positions live only in Redis between fills (see
        # database/positionCache.py) - Postgres's SELECT ... FOR UPDATE row
        # lock used to be what serialized concurrent fills on the same
        # (user_id, tsym); this distributed lock replaces that role now that
        # the read-modify-write happens against the cache instead.
        with self.position_cache.lock(user_id, tsym):
            cached_existing = self.position_cache.get_position(user_id, tsym)
            existing = cached_existing
            if existing is None:
                # Cache miss: either this is genuinely the first fill ever for
                # this (user_id, tsym), or it's reopening a position that was
                # previously closed (and evicted from cache on close) - either
                # way, fall back to Postgres so a reopen still inherits
                # nothing stale and this fill is the one that (re)creates the
                # DB row (see the netqty branch below).
                existing = self.position_persistence.get_position(user_id, tsym, cursor)

            # existing may come from the cache (plain float/int JSON) or the
            # DB fallback (Decimal) - str() first so both round-trip through
            # Decimal exactly, instead of Decimal(float) picking up binary
            # floating-point noise (e.g. Decimal(120.0) != Decimal("120.0")).
            def _dec(field):
                value = existing.get(field) if existing else None
                return Decimal(str(value)) if value is not None else Decimal(0)

            netqty = _dec("netqty")
            netavgprc = _dec("netavgprc")
            buyqty = _dec("buyqty")
            sellqty = _dec("sellqty")
            buyavgprc = _dec("buyavgprc")
            sellavgprc = _dec("sellavgprc")
            realized_pnl = _dec("realized_pnl")

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
            exchange = order_snapshot.get("exchange")
            token = order_snapshot.get("token")

            now_ts = int(time.time())
            created_at = existing.get("created_at") if existing and existing.get("created_at") else now_ts
            unrealized_pnl = (fill_price - netavgprc) * netqty if netqty != 0 else Decimal(0)

            position = {
                "schema_version": CACHE_SCHEMA_VERSION,
                "user_id": user_id,
                "tsym": tsym,
                "broker": order_snapshot.get("broker"),
                "token": token,
                "exchange": exchange,
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
                "unrealized_pnl": unrealized_pnl,
                "total_pnl": realized_pnl + unrealized_pnl,
                "lp": fill_price,
                "last_tick_ts": now_ts,
                "status": "OPEN" if netqty != 0 else "CLOSED",
                "created_at": created_at,
                "updated_at": now_ts,
            }

            if netqty == 0:
                # Position fully closed by this fill - selling-related
                # columns (sellqty/sellavgprc), netqty=0 and realized_pnl are
                # all already final in `position` above. Persisted to
                # Postgres and torn down from the cache, since a closed
                # position has nothing left to refresh on tick.
                self.position_persistence.upsert_position(position, cursor)
                self.position_cache.remove_position(user_id, tsym, exchange, token)
                positionTickService.release(exchange, token)
                self.logger.info(
                    f"Position closed and persisted: user_id={user_id}, tsym={tsym}, "
                    f"realized_pnl={realized_pnl}"
                )
            elif cached_existing is None:
                # First fill of this position's lifecycle (brand new, or
                # reopened after a prior close) - persisted to Postgres
                # immediately, in addition to the cache, so the positions
                # table reflects it as OPEN right away instead of only
                # appearing once it eventually closes.
                self.position_persistence.upsert_position(position, cursor)
                self.position_cache.save_open_position(position)
                positionTickService.ensure_subscribed(exchange, token)
                self.logger.info(
                    f"Position opened and persisted: user_id={user_id}, tsym={tsym}, "
                    f"netqty={position['netqty']}"
                )
            else:
                # Continuing fill on an already-open, already-cached position
                # - cache only, no DB call. Seed lp with this fill's price so
                # unrealized_pnl is sane immediately; the live tick feed
                # keeps both fresh from here on.
                self.position_cache.save_open_position(position)
                positionTickService.ensure_subscribed(exchange, token)
                self.logger.info(
                    f"Open position cached: user_id={user_id}, tsym={tsym}, "
                    f"netqty={position['netqty']}"
                )
