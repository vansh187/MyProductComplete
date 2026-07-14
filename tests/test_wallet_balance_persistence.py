"""
Unit tests for WalletBalancePersistence.debitWalletIfSufficient and
creditWalletStandalone - the atomic check-and-debit / atomic-credit methods
added to fix the wallet check-then-debit race on order placement
(api/orders.py previously read the balance unlocked, compared in Python,
then wrote a separately-computed total back with no re-check at write
time - two concurrent BUY orders could both pass the check and both debit).

No real network or DB calls are made: the Postgres connection factory is
mocked throughout.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from database.walletbalancepersistence.WalletBalancePersistence import WalletBalancePersistence


class TestDebitWalletIfSufficient:

    def test_sufficient_balance_debits_and_returns_true(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 1
        conn.cursor.return_value = cursor
        with patch(
            "database.walletbalancepersistence.WalletBalancePersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            result = WalletBalancePersistence().debitWalletIfSufficient(42, Decimal("500.00"))

        assert result is True
        executed_sql, params = cursor.execute.call_args.args
        assert "balance = balance - %s" in executed_sql
        assert "balance >= %s" in executed_sql
        assert params == (Decimal("500.00"), 42, Decimal("500.00"))
        conn.commit.assert_called_once()

    def test_insufficient_balance_does_not_debit_and_returns_false(self):
        """rowcount == 0 means the WHERE clause's balance >= %s guard
        didn't match any row - either the balance was too low or no
        wallet row exists at all. Either way, nothing is written."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 0
        conn.cursor.return_value = cursor
        with patch(
            "database.walletbalancepersistence.WalletBalancePersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            result = WalletBalancePersistence().debitWalletIfSufficient(42, Decimal("500.00"))

        assert result is False
        conn.commit.assert_called_once()  # the UPDATE-affecting-0-rows still commits cleanly, no error

    def test_no_wallet_row_at_all_returns_false_not_an_error(self):
        """A user with no wallets row yet must fail closed (False), not
        raise - the same atomic UPDATE naturally matches 0 rows."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 0
        conn.cursor.return_value = cursor
        with patch(
            "database.walletbalancepersistence.WalletBalancePersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            result = WalletBalancePersistence().debitWalletIfSufficient(999, Decimal("1.00"))

        assert result is False

    def test_db_error_rolls_back_and_raises(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("connection reset by peer")
        conn.cursor.return_value = cursor
        with patch(
            "database.walletbalancepersistence.WalletBalancePersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            with pytest.raises(Exception, match="Exception while debiting wallet balance"):
                WalletBalancePersistence().debitWalletIfSufficient(42, Decimal("500.00"))

        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_zero_or_negative_amount_rejected_before_any_db_call(self):
        conn = MagicMock()
        with patch(
            "database.walletbalancepersistence.WalletBalancePersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            with pytest.raises(ValueError, match="Debit amount must be positive"):
                WalletBalancePersistence().debitWalletIfSufficient(42, Decimal("0"))
            with pytest.raises(ValueError, match="Debit amount must be positive"):
                WalletBalancePersistence().debitWalletIfSufficient(42, Decimal("-10"))

        conn.cursor.assert_not_called()

    def test_invalid_user_id_rejected_before_any_db_call(self):
        conn = MagicMock()
        with patch(
            "database.walletbalancepersistence.WalletBalancePersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            with pytest.raises(ValueError, match="User ID must be a positive integer"):
                WalletBalancePersistence().debitWalletIfSufficient(0, Decimal("10"))

        conn.cursor.assert_not_called()


class TestCreditWalletStandalone:

    def test_credits_atomically_via_delta_not_absolute_write(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        with patch(
            "database.walletbalancepersistence.WalletBalancePersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            WalletBalancePersistence().creditWalletStandalone(42, Decimal("2500.00"))

        executed_sql, params = cursor.execute.call_args.args
        assert "balance = COALESCE(balance, 0) + %s" in executed_sql
        assert params == (Decimal("2500.00"), 42)
        conn.commit.assert_called_once()

    def test_db_error_rolls_back_and_raises(self):
        """A refund failure must be visible (raised), not swallowed -
        api/orders.py's _refund_wallet_after_order_creation_failure is the
        caller responsible for catching and logging this loudly, precisely
        because a silent failure here would leave a user debited for an
        order that was never created."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("connection reset by peer")
        conn.cursor.return_value = cursor
        with patch(
            "database.walletbalancepersistence.WalletBalancePersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            with pytest.raises(Exception, match="Exception while crediting wallet balance"):
                WalletBalancePersistence().creditWalletStandalone(42, Decimal("2500.00"))

        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_zero_or_negative_amount_rejected_before_any_db_call(self):
        conn = MagicMock()
        with patch(
            "database.walletbalancepersistence.WalletBalancePersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            with pytest.raises(ValueError, match="Credit amount must be positive"):
                WalletBalancePersistence().creditWalletStandalone(42, Decimal("0"))

        conn.cursor.assert_not_called()


class TestWalletDebitConcurrencySimulated:
    """
    No local Postgres server is available in this environment (see the
    equivalent note in tests/test_razorpay_wallet_idempotency.py). This
    simulates the same hazard the atomic conditional UPDATE closes - many
    threads racing to debit a shared balance, some of which must be
    correctly refused once funds run out - against an in-memory stand-in
    that mimics `UPDATE ... SET balance = balance - %s WHERE balance >= %s`
    semantics (single lock scoping the check-and-decrement as one step).
    """

    def test_concurrent_debits_never_overdraw_the_balance(self):
        import threading

        class FakeWalletRow:
            def __init__(self, balance):
                self.balance = Decimal(str(balance))
                self._lock = threading.Lock()

            def debit_if_sufficient(self, amount: Decimal) -> bool:
                with self._lock:
                    if self.balance >= amount:
                        self.balance -= amount
                        return True
                    return False

        wallet = FakeWalletRow(Decimal("1000.00"))
        debit_amount = Decimal("100.00")
        num_threads = 20  # 20 x 100 = 2000 requested against only 1000 available
        results = []
        results_lock = threading.Lock()

        def worker():
            ok = wallet.debit_if_sufficient(debit_amount)
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "a debit thread hung - indicates a deadlock in the locking scheme"

        successful = sum(1 for r in results if r)
        assert successful == 10, f"expected exactly 10 successful debits of 100 from a balance of 1000, got {successful}"
        assert wallet.balance == Decimal("0.00"), f"balance must land at exactly 0, got {wallet.balance} (over/under-debit)"
        assert wallet.balance >= 0, "balance must never go negative"
