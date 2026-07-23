"""
OrderUpdateService - the low-latency path from a real Shoonya fill/reject/
cancel notification back into our positions/wallet/order status.

Order updates arrive over the SAME WebSocket already used for price ticks
(confirmed from Shoonya's own README - "Feed and Order updates are received
from the same websocket") via marketengine/ShoonyaOptionFeed.py's
on_order_update() registration - no separate connection, no polling.

This is the SOLE source of truth for F&O orders placed via POST
/createLiveOrder (service/liveOrderRoutingService.py): those orders do not
participate in internal peer-matching at all, so nothing else ever updates
their status or the resulting positions. Reuses the exact settlement path
(TradeSettlementService.settle_fill -> PositionService.apply_fill) and the
exact cancel path (OrderService.cancel_order_by_id, with its wallet refund +
margin release + order_book cancellation) that internal/simulated orders
already use - no new position or refund math is introduced here.
"""

import logging
import re
from decimal import Decimal
from typing import Any, Dict, Optional

import psycopg2
import psycopg2.errors

from database.PostgresConnectionFactory import PostgresConnectionFactory
from database.orderPersistence import OrderPersistence
from service.orderService import OrderService
from service.tradeSettlementService import TradeSettlementService
from service.tradeHistoryService import TradeHistoryService
from utils.query_loader import QueryLoader

logger = logging.getLogger(__name__)

# Matches the exact remarks tag LiveOrderRoutingService.place_live_order
# stamps on every order it submits (f"primepip_{order_id}") - used as a
# fallback lookup when an update's broker_order_id can't yet be found (see
# _resolve_order_by_remarks_fallback below).
_REMARKS_ORDER_ID_PATTERN = re.compile(r"^primepip_(\d+)$")

# report_type values a Fill/Rejected/Canceled event can carry, per Shoonya's
# README "Order Update subscription Updates" table - other values (New,
# Replaced, ...) need no action here, the order is already PENDING from
# placement time.
_REPORT_TYPE_FILL = "Fill"
_REPORT_TYPE_REJECTED = "Rejected"
_REPORT_TYPE_CANCELED = "Canceled"

# Statuses a live order can still be reconciled from - mirrors exactly what
# OrderService.cancel_order_by_id's own query already allows (status IN
# ('PENDING', 'PENDING_TRIGGER')), so this never attempts to "cancel" an
# order the DB would refuse to touch anyway.
_RECONCILABLE_STATUSES = ("PENDING", "PENDING_TRIGGER")


def _safe_int(val) -> Optional[int]:
    try:
        return int(float(val)) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _enum_str(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


class OrderUpdateService:

    def __init__(self):
        self.order_persistence = OrderPersistence()
        self.order_service = OrderService()
        self.trade_settlement_service = TradeSettlementService()
        self.trade_history_service = TradeHistoryService()
        self.logger = logger

    async def handle_order_update(self, raw: Dict[str, Any]) -> None:
        """
        Registered as ShoonyaOptionFeed.on_order_update's handler. Async so
        the feed's dispatch (marketengine/ShoonyaOptionFeed.py._on_order_update)
        schedules this as a detached task on the asyncio loop instead of
        blocking NorenApi's own WS thread - mirrors the tick-handling pattern
        already used throughout this codebase.

        Never raises - every branch is defensive and logs loudly rather than
        letting a malformed/unexpected update crash silently (this runs on
        every single order-status change for every live order; one bad
        message must never take down processing of the next one).
        """
        try:
            broker_order_id = raw.get("norenordno")
            if not broker_order_id:
                return

            report_type = (raw.get("reporttype") or "").strip()
            if not report_type:
                # Ack-only messages (e.g. the initial subscription 'ok') -
                # nothing to reconcile.
                return

            order_row = self.order_persistence.get_order_by_broker_order_id(broker_order_id)
            if order_row is None:
                # Rare but real race: place_live_order's own
                # set_broker_order_id write hasn't committed yet when this
                # update arrives (fast fills on liquid options can beat our
                # own HTTP round trip back). Without this fallback the fill
                # would be silently dropped forever - the broker's own
                # `remarks` tag (stamped at placement as f"primepip_{order_id}")
                # lets us find the order directly by its internal id instead.
                order_row = self._resolve_order_by_remarks_fallback(raw)

            if order_row is None:
                self.logger.warning(
                    f"[OrderUpdate] No internal order found for broker_order_id={broker_order_id} "
                    f"(reporttype={report_type}) - update ignored"
                )
                return

            if report_type == _REPORT_TYPE_FILL:
                self._handle_fill(order_row, raw)
            elif report_type == _REPORT_TYPE_REJECTED:
                self._handle_rejected(order_row, raw)
            elif report_type == _REPORT_TYPE_CANCELED:
                self._handle_cancelled(order_row, raw)
        except Exception as ex:
            self.logger.error(f"[OrderUpdate] Failed to process order update {raw}: {str(ex)}", exc_info=True)

    def _resolve_order_by_remarks_fallback(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        remarks = (raw.get("remarks") or "").strip()
        match = _REMARKS_ORDER_ID_PATTERN.match(remarks)
        if not match:
            return None

        order_id = int(match.group(1))
        try:
            order_row = self.order_persistence.get_order_by_id_only(order_id)
        except Exception as ex:
            self.logger.error(f"[OrderUpdate] Remarks fallback lookup failed for order_id={order_id}: {ex}")
            return None

        if order_row is None:
            return None

        broker_order_id = raw.get("norenordno")
        if not order_row.get("broker_order_id") and broker_order_id:
            # Self-heal: back-fill the write that hadn't committed yet, so
            # every later update for this order finds it via the normal
            # broker_order_id lookup instead of hitting this fallback again.
            try:
                self.order_persistence.set_broker_order_id(order_id, broker_order_id)
                order_row["broker_order_id"] = broker_order_id
            except Exception as ex:
                self.logger.error(
                    f"[OrderUpdate] Failed to back-fill broker_order_id={broker_order_id} for "
                    f"order_id={order_id} during remarks fallback: {ex}"
                )

        self.logger.warning(
            f"[OrderUpdate] Resolved order_id={order_id} via remarks fallback (broker_order_id="
            f"{broker_order_id} not yet committed when this update arrived)"
        )
        return order_row

    def _handle_fill(self, order_row: Dict[str, Any], raw: Dict[str, Any]) -> None:
        fill_qty = _safe_int(raw.get("flqty"))
        fill_price = _safe_float(raw.get("flprc"))
        if not fill_qty or fill_qty <= 0 or not fill_price or fill_price <= 0:
            self.logger.warning(f"[OrderUpdate] Fill event missing/invalid flqty/flprc: {raw}")
            return

        user_id = order_row["user_id"]
        order_id = order_row["id"]
        side_value = _enum_str(order_row["side"])
        exchange_value = _enum_str(order_row["exchange"])

        order_snapshot = {
            "symbol": order_row["symbol"],
            "exchange": exchange_value,
            "token": order_row.get("token"),
            "broker": "Shoonya",
            "source": "LIVE",
            "lot_size": order_row.get("lot_size"),
            "product_type": _enum_str(order_row["product_type"]),
        }

        flid = raw.get("flid")

        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()

            if flid:
                # Shoonya's order-update feed can redeliver a Fill message
                # (e.g. resubscription after a WS reconnect) - `flid` is the
                # broker's own unique id for this specific fill, provided
                # exactly so duplicates can be detected. Claiming it here, in
                # the SAME transaction as the settlement below, means a
                # duplicate delivery hits the table's PRIMARY KEY and is
                # skipped instead of double-crediting the position/wallet.
                try:
                    insert_fill_query = QueryLoader.get('orders.yaml', 'insert_processed_broker_fill')
                    cursor.execute(insert_fill_query, (flid, order_id))
                except psycopg2.errors.UniqueViolation:
                    conn.rollback()
                    self.logger.info(
                        f"[OrderUpdate] Duplicate fill ignored: order_id={order_id}, flid={flid} "
                        f"(already settled)"
                    )
                    return
            else:
                # Per Shoonya's own docs flid is always present on a Fill
                # event - defensive only. Proceeding without dedup here is no
                # worse than this code's behavior before this fix.
                self.logger.warning(f"[OrderUpdate] Fill event has no flid, cannot dedup: {raw}")

            # The real counterparty is the exchange itself, not another
            # internal user - only our own side of buy_order_id/sell_order_id
            # and buy_user_id/sell_user_id is populated (both are nullable;
            # get_fill_stats_by_order_id's `buy_order_id = %s OR
            # sell_order_id = %s` already tolerates a NULL counterparty side).
            trade_value = Decimal(str(fill_qty)) * Decimal(str(fill_price))
            if side_value == "BUY":
                buy_order_id, sell_order_id = order_id, None
                buy_user_id, sell_user_id = user_id, None
            else:
                buy_order_id, sell_order_id = None, order_id
                buy_user_id, sell_user_id = None, user_id

            self.trade_history_service.insertTradeOrders(
                buy_order_id, sell_order_id, buy_user_id, sell_user_id,
                order_row["symbol"], fill_qty, fill_price, trade_value,
                cursor, user_id, side_value,
            )

            # Reuses the EXACT settlement path an internal peer-matched fill
            # uses (service/tradeSettlementService.py -> holdings or
            # positions, including margin reconciliation inside
            # PositionService.apply_fill) - no new position/margin math here,
            # just a real fill price/qty instead of an internal match's.
            self.trade_settlement_service.settle_fill(
                user_id, side_value, order_snapshot, fill_qty, fill_price, cursor
            )

            avg_fill_price, filled_qty = self.trade_history_service.getFillStats(order_id, cursor)
            new_status = "EXECUTED" if filled_qty >= order_row["quantity"] else "PARTIALLY_EXECUTED"
            self.order_service.update_order_status_single(new_status, order_id, cursor)

            conn.commit()

            self.logger.info(
                f"[OrderUpdate] Real fill settled: order_id={order_id}, user={user_id}, "
                f"qty={fill_qty}, price={fill_price}, status={new_status}"
            )
        except Exception as ex:
            if conn:
                conn.rollback()
            self.logger.error(
                f"[OrderUpdate] Failed to settle real fill for order_id={order_row.get('id')}, "
                f"broker_order_id={raw.get('norenordno')}: {str(ex)} - manual reconciliation needed "
                f"(the real fill happened at the broker regardless of this failure)",
                exc_info=True,
            )
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def _handle_rejected(self, order_row: Dict[str, Any], raw: Dict[str, Any]) -> None:
        order_id = order_row["id"]
        user_id = order_row["user_id"]
        status_value = _enum_str(order_row["status"])

        if status_value not in _RECONCILABLE_STATUSES:
            # Already reconciled (e.g. a synchronous Not_Ok at placement time
            # already cancelled it) - avoid double-refunding.
            self.logger.info(
                f"[OrderUpdate] Ignoring Rejected update for order {order_id} already in status {status_value}"
            )
            return

        reason = raw.get("rejreason", "unknown")
        self.logger.warning(f"[OrderUpdate] Broker rejected order {order_id} post-placement: {reason}")
        try:
            # Reuses the exact same cancel path (wallet refund + margin
            # release + order_book cancellation) a user-initiated cancel
            # uses - a broker-side reject after acceptance is functionally
            # identical to a cancellation from our system's point of view.
            self.order_service.cancel_order_by_id(user_id, order_id)
        except Exception as ex:
            self.logger.error(
                f"[OrderUpdate] Failed to reconcile rejected order {order_id}: {str(ex)}", exc_info=True
            )

    def _handle_cancelled(self, order_row: Dict[str, Any], raw: Dict[str, Any]) -> None:
        order_id = order_row["id"]
        user_id = order_row["user_id"]
        status_value = _enum_str(order_row["status"])

        if status_value not in _RECONCILABLE_STATUSES:
            # A cancel notification for an order that's already
            # PARTIALLY_EXECUTED (cancelling the unfilled remainder) isn't
            # handled here - refunding only the unfilled portion needs
            # tracking this codebase doesn't have yet for live orders
            # (cancel_order_by_id's own query only ever matches
            # PENDING/PENDING_TRIGGER, mirroring the same limitation
            # internal/simulated orders already have). Logged loudly for
            # manual reconciliation rather than guessed at.
            self.logger.warning(
                f"[OrderUpdate] Canceled update for order {order_id} in status {status_value} - "
                f"not auto-reconciled (only PENDING/PENDING_TRIGGER orders are handled automatically), "
                f"manual reconciliation needed if a refund is owed"
            )
            return

        self.logger.info(f"[OrderUpdate] Broker confirms order {order_id} cancelled")
        try:
            self.order_service.cancel_order_by_id(user_id, order_id)
        except Exception as ex:
            self.logger.error(
                f"[OrderUpdate] Failed to reconcile cancelled order {order_id}: {str(ex)}", exc_info=True
            )
