"""
PostgresMarginLedgerRepository - today's only MarginLedgerRepository
implementation. Composes the marginenginepersistence classes (each owning
one table) into the three ledger operations MarginEngine needs, always
inside the caller-owned transaction/cursor - this class never opens or
commits its own connection.
"""

import logging
from decimal import Decimal

from database.marginenginepersistence.margin_audit_persistence import MarginAuditPersistence
from database.marginenginepersistence.margin_block_persistence import MarginBlockPersistence
from database.marginenginepersistence.margin_wallet_persistence import MarginWalletPersistence
from database.marginenginepersistence.position_margin_persistence import PositionMarginPersistence
from service.marginengine.exceptions import MarginEngineError
from service.marginengine.interfaces import MarginLedgerRepository
from service.marginengine.models import MarginResult

logger = logging.getLogger(__name__)


class PostgresMarginLedgerRepository(MarginLedgerRepository):
    """Writes margin blocks/releases/reconciliations across order_margin_blocks,
    wallets.blocked_margin, positions.blocked_margin and margin_block_audit."""

    def __init__(self):
        self.block_persistence = MarginBlockPersistence()
        self.wallet_persistence = MarginWalletPersistence()
        self.position_persistence = PositionMarginPersistence()
        self.audit_persistence = MarginAuditPersistence()
        self.logger = logger

    def block_order_margin(self, cursor, user_id: int, order_id: int,
                            block_fields: dict, margin_result: MarginResult) -> int:
        try:
            block_row = {
                "order_id": order_id,
                "user_id": user_id,
                "tsym": block_fields["tsym"],
                "exchange": block_fields["exchange"],
                "contract_type": block_fields["contract_type"],
                "side": block_fields["side"],
                "qty": block_fields["qty"],
                "lot_size": block_fields["lot_size"],
                "blocked_amount": margin_result.blocked_amount,
                "premium_component": margin_result.premium_component,
                "notional_component": margin_result.notional_component,
                "reference_price": margin_result.reference_price,
                "reference_source": margin_result.reference_source,
                "reference_source_tier": margin_result.reference_source_tier,
                "price_source_multiplier_used": margin_result.price_source_multiplier_used,
                "moneyness_multiplier_used": margin_result.moneyness_multiplier_used,
                "expiry_multiplier_used": margin_result.expiry_multiplier_used,
                "notional_pct_or_span_pct": margin_result.notional_pct_or_span_pct_used,
            }
            block_id = self.block_persistence.insert_block(cursor, block_row)

            wallet = self.wallet_persistence.get_wallet_for_update(cursor, user_id)
            if wallet is None:
                raise MarginEngineError(f"No wallet found for user {user_id}")
            new_blocked = wallet["blocked_margin"] + margin_result.blocked_amount
            self.wallet_persistence.update_blocked_margin(cursor, user_id, new_blocked)

            self.audit_persistence.insert_audit(
                cursor, block_id, user_id, "BLOCK", margin_result.blocked_amount, "ORDER_PLACED"
            )
            return block_id
        except MarginEngineError:
            raise
        except Exception as ex:
            self.logger.error(f"Error blocking order margin for user {user_id}, order {order_id}: {str(ex)}")
            raise MarginEngineError(f"Error blocking order margin: {str(ex)}") from ex

    def release_order_margin(self, cursor, order_id: int, release_reason: str) -> None:
        try:
            block = self.block_persistence.get_active_block_for_order(cursor, order_id)
            if block is None:
                # Idempotent: no active block means nothing to release - a
                # closing trade never had one, or it was already released.
                return

            self.block_persistence.release_block(cursor, block["id"], release_reason)

            wallet = self.wallet_persistence.get_wallet_for_update(cursor, block["user_id"])
            if wallet is not None:
                new_blocked = max(Decimal(0), wallet["blocked_margin"] - block["blocked_amount"])
                self.wallet_persistence.update_blocked_margin(cursor, block["user_id"], new_blocked)

            self.audit_persistence.insert_audit(
                cursor, block["id"], block["user_id"], "RELEASE", -block["blocked_amount"], release_reason
            )
        except Exception as ex:
            self.logger.error(f"Error releasing order margin for order {order_id}: {str(ex)}")
            raise MarginEngineError(f"Error releasing order margin: {str(ex)}") from ex

    def reconcile_position_margin(self, cursor, user_id: int, tsym: str,
                                   new_required_margin, contract_type: str) -> None:
        try:
            position = self.position_persistence.get_position_for_margin(cursor, user_id, tsym)
            old_position_blocked = position["blocked_margin"] if position is not None else Decimal(0)

            self.position_persistence.update_blocked_margin(cursor, user_id, tsym, new_required_margin)

            wallet = self.wallet_persistence.get_wallet_for_update(cursor, user_id)
            if wallet is None:
                raise MarginEngineError(f"No wallet found for user {user_id}")

            delta = Decimal(str(new_required_margin)) - old_position_blocked
            new_wallet_blocked = max(Decimal(0), wallet["blocked_margin"] + delta)
            self.wallet_persistence.update_blocked_margin(cursor, user_id, new_wallet_blocked)

            self.audit_persistence.insert_audit(
                cursor, None, user_id, "RECONCILE", delta, "FILL_RECONCILE"
            )
        except MarginEngineError:
            raise
        except Exception as ex:
            self.logger.error(f"Error reconciling position margin for user {user_id}, tsym {tsym}: {str(ex)}")
            raise MarginEngineError(f"Error reconciling position margin: {str(ex)}") from ex
