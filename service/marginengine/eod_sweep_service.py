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
from service.marginengine.margin_engine import MarginEngine

logger = logging.getLogger(__name__)


class EodMarginSweepService:
    """Runs the two EOD sweeps: expiry release, and MTM-breach flag-for-review."""

    def __init__(self):
        self.margin_engine = MarginEngine()
        self.sweep_persistence = MarginSweepPersistence()
        self.position_persistence = PositionMarginPersistence()
        self.wallet_persistence = MarginWalletPersistence()
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
