"""
EodMarginSweepService - the daily end-of-day sweep referenced throughout
FnO_Margin_Engine_Design.md (section 6): releases margin for options that
expired today (auto-assignment settlement), and flags (never
auto-liquidates) any account whose current mark-to-market required margin
would exceed its available balance if recomputed today.

This service is a plain callable class, not wired to a scheduler - this
repo has no job-scheduling infrastructure today (see
FnO_Margin_Engine_Design.md section 13, real-time MTM/auto square-off is
explicitly out of scope). Invoke `EodMarginSweepService().run_daily_sweep()`
from whatever process/cron the team wires up next (e.g. a small script under
scripts/, run once after market close).
"""

import logging
from datetime import date, datetime
from decimal import Decimal

from database.PostgresConnectionFactory import PostgresConnectionFactory
from database.marginenginepersistence.margin_sweep_persistence import MarginSweepPersistence
from database.marginenginepersistence.position_margin_persistence import PositionMarginPersistence
from database.marginenginepersistence.margin_wallet_persistence import MarginWalletPersistence
from database.positionCache import PositionCache
from database.positionPersistence import PositionPersistence
from service.marginengine.margin_engine import MarginEngine
from service.positionTickService import positionTickService

logger = logging.getLogger(__name__)


class EodMarginSweepService:
    """Runs the two EOD sweeps: expiry release, and MTM-breach flag-for-review."""

    def __init__(self):
        self.margin_engine = MarginEngine()
        self.sweep_persistence = MarginSweepPersistence()
        self.position_persistence = PositionMarginPersistence()
        self.wallet_persistence = MarginWalletPersistence()
        self.position_full_persistence = PositionPersistence()
        self.position_cache = PositionCache()
        self.logger = logger

    def run_daily_sweep(self, run_date: date = None) -> dict:
        """
        Runs both sweeps once. Never raises for an individual account's
        failure - one bad row must not abort the sweep for everyone else;
        failures are logged and counted instead.

        Returns:
            {"expiry_released": int, "snapshots_written": int, "flags_raised": int, "errors": int}
        """
        run_date = run_date or datetime.now().date()
        result = {"expiry_released": 0, "snapshots_written": 0, "flags_raised": 0, "errors": 0}

        try:
            result["expiry_released"] = self._release_expired_positions(run_date)
        except Exception as ex:
            self.logger.error(f"EOD expiry-release sweep failed: {str(ex)}")
            result["errors"] += 1

        try:
            snapshots, flags = self._snapshot_and_flag(run_date)
            result["snapshots_written"] = snapshots
            result["flags_raised"] = flags
        except Exception as ex:
            self.logger.error(f"EOD snapshot/flag sweep failed: {str(ex)}")
            result["errors"] += 1

        return result

    def _release_expired_positions(self, run_date: date) -> int:
        conn = None
        cursor = None
        released = 0
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            expiring = self.position_persistence.get_open_positions_expiring_by(cursor, run_date)

            for position in expiring:
                try:
                    self.margin_engine.reconcile_on_fill(
                        user_id=position["user_id"], side="CLOSE",
                        order_snapshot={"symbol": position["tsym"], "exchange": position["exchange"],
                                         "lot_size": None, "id": None},
                        new_net_qty=Decimal(0), cursor=cursor,
                    )
                    released += 1
                except Exception as ex:
                    self.logger.error(f"Failed to release expiry margin for user {position['user_id']}, "
                                       f"tsym {position['tsym']}: {str(ex)}")

                # Releasing the margin block alone leaves the position row
                # itself open forever ("zombie" position - margin looks
                # freed but netqty/status never change, no P&L ever
                # realized). Closing is attempted independently of whether
                # the margin release above succeeded, and a failure here
                # (e.g. no settlement price resolvable yet) never aborts the
                # sweep for other accounts - it just leaves this one
                # position open for tomorrow's sweep to retry.
                try:
                    self._close_expired_position(position, cursor)
                except Exception as ex:
                    self.logger.error(f"Failed to close expired position for user {position['user_id']}, "
                                       f"tsym {position['tsym']}: {str(ex)}")

            conn.commit()
            return released
        except Exception as ex:
            if conn is not None:
                conn.rollback()
            raise Exception(f"Error releasing expired-position margin: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    def _close_expired_position(self, position: dict, cursor) -> None:
        """
        Actually closes a still-open F&O position past its expiry, mirroring
        PositionService.apply_fill()'s netqty==0 close branch: marks the
        position CLOSED at netqty=0, realizes P&L against a resolved
        settlement price, evicts it from the Redis cache, and releases its
        feed subscription. Deliberately does NOT move wallet cash - no
        normal position close in this codebase settles P&L into wallet
        balance either (see service/positionService.py - realized_pnl is
        informational there too), so this stays consistent with existing
        behavior rather than inventing new cash-movement logic for expiry
        alone, which would be the highest-risk kind of change to get wrong.

        Silently returns (no-op) if the position was already closed by a
        concurrent path, or if no settlement price can be resolved yet -
        the latter leaves the position open for tomorrow's sweep to retry
        rather than closing it against a fabricated price.
        """
        user_id = position["user_id"]
        tsym = position["tsym"]

        full_position = self.position_persistence.get_full_position_for_expiry_close(cursor, user_id, tsym)
        if full_position is None:
            return

        netqty = Decimal(str(full_position.get("netqty") or 0))
        if netqty == 0:
            return

        instrument = self.margin_engine.resolve_contract_type(tsym, full_position.get("exchange"))
        contract_type = instrument.get("contract_type")
        if contract_type not in ("OPTION", "FUTURES"):
            self.logger.warning(
                f"Cannot resolve contract type for expired position {tsym} (user {user_id}); "
                f"leaving open for retry"
            )
            return

        resolution = self.margin_engine.price_resolver.resolve(
            tsym, contract_type, instrument.get("underlying"), full_position.get("exchange"),
            instrument.get("token"), strike=instrument.get("strike"),
            option_type=instrument.get("option_type"), expiry=instrument.get("expiry"),
        )
        settlement_price = resolution.price

        netavgprc = Decimal(str(full_position.get("netavgprc") or 0))
        realized_pnl = Decimal(str(full_position.get("realized_pnl") or 0)) + (settlement_price - netavgprc) * netqty

        closed_position = dict(full_position)
        closed_position["netqty"] = 0
        closed_position["netavgprc"] = Decimal(0)
        closed_position["realized_pnl"] = realized_pnl
        closed_position["status"] = "CLOSED"

        self.position_full_persistence.upsert_position(closed_position, cursor)
        self.position_cache.remove_position(user_id, tsym, full_position.get("exchange"), full_position.get("token"))
        positionTickService.release(full_position.get("exchange"), full_position.get("token"))
        self.logger.info(
            f"Expiry-closed position: user_id={user_id}, tsym={tsym}, "
            f"settlement_price={settlement_price}, realized_pnl={realized_pnl}"
        )

    def _snapshot_and_flag(self, run_date: date) -> tuple[int, int]:
        conn = None
        cursor = None
        snapshots = 0
        flags = 0
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()
            wallets = self.wallet_persistence.get_all_wallet_balances(cursor)

            for wallet in wallets:
                try:
                    available_balance = wallet["balance"] - wallet["blocked_margin"]
                    self.sweep_persistence.insert_snapshot(
                        cursor, wallet["user_id"], run_date, wallet["blocked_margin"], available_balance
                    )
                    snapshots += 1

                    if available_balance < 0:
                        self.sweep_persistence.insert_review_flag(
                            cursor, wallet["user_id"], run_date, "MTM_BREACH",
                            wallet["blocked_margin"], available_balance
                        )
                        flags += 1
                except Exception as ex:
                    self.logger.error(f"Failed to snapshot/flag user {wallet.get('user_id')}: {str(ex)}")

            conn.commit()
            return snapshots, flags
        except Exception as ex:
            if conn is not None:
                conn.rollback()
            raise Exception(f"Error running EOD snapshot/flag sweep: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
