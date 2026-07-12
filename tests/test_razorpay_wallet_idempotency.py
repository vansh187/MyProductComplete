"""
Unit tests for Razorpay wallet-credit idempotency.

Guards against duplicate/retried webhook or verification-call deliveries for
the same payment double-crediting the wallet:

- RazorPayPersistence.updatePaymentStatus() atomically flips a ledger row
  PENDING -> SUCCESS and returns True only for the delivery that actually
  performed that transition (rowcount > 0). Any later delivery for the same
  payment finds the row already SUCCESS, matches 0 rows, and gets False.
- RazorPayManagerService.invokeCallToDatabase() only calls
  insertUpdateWallet() when updatePaymentStatus() returned True, so a
  duplicate delivery never reaches the wallet-credit code path.
- RazorPayPersistence.insertUpdateWallet() correctly creates a new wallet
  row when none exists yet, and otherwise adds the transaction amount to
  the existing balance.

No real network or DB calls are made: the Razorpay SDK client and the
Postgres connection factory are mocked throughout.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from database.razorpaypersistence.RazorPayPersistence import RazorPayPersistence
from service.razorpay.RazorPayMangerService import RazorPayManagerService


def _mock_conn_with_rowcount(rowcount: int):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.rowcount = rowcount
    conn.cursor.return_value = cursor
    return conn, cursor


class TestUpdatePaymentStatusIdempotency:

    def test_first_call_transitions_pending_to_success_returns_true(self):
        """First webhook delivery: row is still PENDING -> UPDATE affects 1 row -> True"""
        conn, cursor = _mock_conn_with_rowcount(1)
        with patch(
            "database.razorpaypersistence.RazorPayPersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            result = RazorPayPersistence().updatePaymentStatus("order_1", "pay_1", 42)

        assert result is True
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    def test_duplicate_call_finds_already_success_returns_false(self):
        """Second/third webhook delivery for the same payment: row is already
        SUCCESS, so `WHERE status = PENDING` matches 0 rows -> False"""
        conn, cursor = _mock_conn_with_rowcount(0)
        with patch(
            "database.razorpaypersistence.RazorPayPersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            result = RazorPayPersistence().updatePaymentStatus("order_1", "pay_1", 42)

        assert result is False
        conn.commit.assert_called_once()

    def test_db_error_during_update_returns_false_and_rolls_back(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("db exploded")
        conn.cursor.return_value = cursor
        with patch(
            "database.razorpaypersistence.RazorPayPersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            result = RazorPayPersistence().updatePaymentStatus("order_1", "pay_1", 42)

        assert result is False
        conn.rollback.assert_called_once()


class TestInsertUpdateWallet:
    """
    insertUpdateWallet no longer reads a balance and writes a precomputed
    total back (the lost-update race) - it now either (a) runs a single
    atomic `balance = balance + %s` UPDATE for an existing wallet, or
    (b) takes a pg_advisory_xact_lock keyed on the user before deciding
    whether to INSERT a brand-new wallet row. These tests assert on the
    actual SQL text executed (real query_loader/yaml, not mocked) so a
    regression back to the old read-then-write pattern would be caught.
    """

    def test_creates_new_wallet_when_none_exists(self):
        conn = MagicMock()
        cursor = MagicMock()
        # 1st fetchone: select_wallet_for_update -> no wallet row yet.
        # 2nd fetchone: get_wallet_balance_for_update (post-lock re-check)
        # -> still no row, so this call must INSERT.
        cursor.fetchone.side_effect = [
            {"wallet_id": None, "balance": None, "transaction_amount": 500.0, "transaction_status": "2"},
            None,
        ]
        conn.cursor.return_value = cursor
        with patch(
            "database.razorpaypersistence.RazorPayPersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            RazorPayPersistence().insertUpdateWallet(42, "order_1")

        executed_sql = [call.args[0] for call in cursor.execute.call_args_list]
        assert any("pg_advisory_xact_lock" in sql for sql in executed_sql), \
            "must take an advisory lock before deciding to insert a brand-new wallet row"
        insert_call = cursor.execute.call_args_list[-1]
        assert "INSERT INTO wallets" in insert_call.args[0]
        assert insert_call.args[1] == (42, Decimal("500.0"))
        conn.commit.assert_called_once()

    def test_wallet_created_concurrently_between_first_check_and_lock_credits_instead_of_double_inserting(self):
        """If another transaction created the wallet row in the gap between
        the first SELECT and this transaction acquiring the advisory lock,
        the post-lock re-check must see it and switch to an atomic
        increment rather than attempting a second INSERT (which would
        either duplicate the row or fail)."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"wallet_id": None, "balance": None, "transaction_amount": 500.0, "transaction_status": "2"},
            {"user_id": 42, "balance": 200.0},  # row now exists, seen post-lock
        ]
        conn.cursor.return_value = cursor
        with patch(
            "database.razorpaypersistence.RazorPayPersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            RazorPayPersistence().insertUpdateWallet(42, "order_1")

        last_call = cursor.execute.call_args_list[-1]
        assert "balance = COALESCE(balance, 0) + %s" in last_call.args[0]
        assert last_call.args[1] == (Decimal("500.0"), 42)
        assert not any("INSERT INTO wallets" in call.args[0] for call in cursor.execute.call_args_list)

    def test_adds_transaction_amount_to_existing_balance_atomically(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "wallet_id": 7,
            "balance": 1000.0,
            "transaction_amount": 500.0,
            "transaction_status": "2",
        }
        conn.cursor.return_value = cursor
        with patch(
            "database.razorpaypersistence.RazorPayPersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            RazorPayPersistence().insertUpdateWallet(42, "order_1")

        update_call = cursor.execute.call_args_list[-1]
        # The delta (transaction_amount), not a precomputed total, must be
        # the parameter - the whole point of the fix is that the new
        # balance is never computed from a possibly-stale read.
        assert "balance = COALESCE(balance, 0) + %s" in update_call.args[0]
        assert update_call.args[1] == (Decimal("500.0"), 42)
        # Only one query should run for the already-exists path - no
        # advisory lock needed since the atomic UPDATE alone is race-safe.
        assert cursor.execute.call_count == 2  # select_wallet_for_update + increment
        conn.commit.assert_called_once()

    def test_no_ledger_record_skips_wallet_write_entirely(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn.cursor.return_value = cursor
        with patch(
            "database.razorpaypersistence.RazorPayPersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            RazorPayPersistence().insertUpdateWallet(42, "order_1")

        # Only the SELECT should have run - no INSERT/UPDATE into wallets.
        assert cursor.execute.call_count == 1
        conn.commit.assert_not_called()

    def test_db_error_during_credit_rolls_back_and_does_not_raise(self):
        """A DB failure mid-credit must roll back (never leave a half-applied
        state) and must not propagate - the webhook background task has no
        retry/dead-letter handling, so an unhandled exception here would
        just vanish into FastAPI's background-task error logging with the
        wallet never credited and nobody alerted synchronously. Swallowing
        and logging matches the existing behavior for the rest of this
        method; it does not silently pretend to succeed - the ledger row
        stays PENDING->SUCCESS already transitioned by updatePaymentStatus,
        but the wallet credit itself failed and is visible in logs for
        reconciliation (see the nodal-account reconciliation job)."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "wallet_id": 7, "balance": 1000.0, "transaction_amount": 500.0, "transaction_status": "2",
        }
        cursor.execute.side_effect = [None, Exception("connection reset by peer")]
        conn.cursor.return_value = cursor
        with patch(
            "database.razorpaypersistence.RazorPayPersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            RazorPayPersistence().insertUpdateWallet(42, "order_1")  # must not raise

        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_zero_transaction_amount_still_runs_atomic_increment(self):
        """A zero-amount ledger row (e.g. a test/₹0 payment) must not be
        special-cased into skipping the credit silently - it should still
        go through the same atomic path with a no-op-equivalent delta."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "wallet_id": 7, "balance": 1000.0, "transaction_amount": 0.0, "transaction_status": "2",
        }
        conn.cursor.return_value = cursor
        with patch(
            "database.razorpaypersistence.RazorPayPersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            RazorPayPersistence().insertUpdateWallet(42, "order_1")

        update_call = cursor.execute.call_args_list[-1]
        assert update_call.args[1] == (Decimal("0.0"), 42)
        conn.commit.assert_called_once()

    def test_fractional_paise_amount_preserved_as_decimal_not_float(self):
        """Money must never round-trip through float - Decimal('1234.56')
        stored as a Python float can drift (e.g. 1234.5600000000001) across
        repeated credits. Guards against a regression back to float()."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "wallet_id": 7, "balance": 1000.0, "transaction_amount": "1234.56", "transaction_status": "2",
        }
        conn.cursor.return_value = cursor
        with patch(
            "database.razorpaypersistence.RazorPayPersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            RazorPayPersistence().insertUpdateWallet(42, "order_1")

        update_call = cursor.execute.call_args_list[-1]
        amount_param = update_call.args[1][0]
        assert isinstance(amount_param, Decimal)
        assert amount_param == Decimal("1234.56")


class TestWalletCreditConcurrencySimulated:
    """
    No local Postgres server is available in this environment (only the
    psql client; the only configured DATABASE_URL points at a shared
    Supabase instance that must not be used for concurrency stress tests).
    Real row-level/advisory-lock contention can't be exercised against an
    actual database here, so this simulates the same hazard - a shared
    mutable balance credited from many threads - against a minimal
    in-memory stand-in that mimics Postgres's atomic
    `UPDATE ... SET balance = balance + %s` semantics (single lock guarding
    the read+write of one statement). This proves the *algorithm* (atomic
    delta application, no read-then-write in application code) is race-free
    under genuine thread contention; it is not a substitute for a real
    integration test against Postgres, which should be run in CI/staging
    where a database is actually available.
    """

    def test_many_concurrent_atomic_credits_lose_no_updates(self):
        import threading

        class FakeWalletRow:
            """Mimics one Postgres row: a single lock scopes each atomic
            UPDATE statement, exactly like a row-level lock would."""

            def __init__(self, balance):
                self.balance = Decimal(str(balance))
                self._lock = threading.Lock()

            def atomic_increment(self, delta: Decimal):
                with self._lock:
                    self.balance += delta

        wallet = FakeWalletRow(Decimal("0"))
        credit_amount = Decimal("100.00")
        num_threads = 50

        def credit_once():
            wallet.atomic_increment(credit_amount)

        threads = [threading.Thread(target=credit_once) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "a credit thread hung - indicates a deadlock in the locking scheme"

        assert wallet.balance == credit_amount * num_threads, (
            f"lost update detected: expected {credit_amount * num_threads}, got {wallet.balance}"
        )

    def test_stale_read_then_write_pattern_does_lose_updates(self):
        """Control case: proves the *old* (buggy) read-then-write pattern
        this fix replaces really does lose updates under contention, so the
        atomic-increment fix above is solving a real, reproducible problem
        and not a hypothetical one."""
        import threading
        import time

        class RacyWalletRow:
            def __init__(self, balance):
                self.balance = Decimal(str(balance))

            def racy_increment(self, delta: Decimal):
                current = self.balance          # unlocked read (the old bug)
                time.sleep(0.001)                # widen the race window deterministically
                self.balance = current + delta   # write back a stale total

        wallet = RacyWalletRow(Decimal("0"))
        credit_amount = Decimal("100.00")
        num_threads = 20

        threads = [threading.Thread(target=wallet.racy_increment, args=(credit_amount,)) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert wallet.balance != credit_amount * num_threads, (
            "expected the unlocked read-then-write pattern to lose updates under contention "
            "(if this assertion fails, the test environment isn't exercising real thread "
            "interleaving - re-check the sleep-based race window)"
        )


class TestInvokeCallToDatabaseGating:

    def test_credits_wallet_only_on_first_processing(self):
        """invokeCallToDatabase must call insertUpdateWallet when this is the
        first time the payment is processed."""
        with patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence:
            instance = MockPersistence.return_value
            instance.updatePaymentStatus.return_value = True

            RazorPayManagerService().invokeCallToDatabase("order_1", "pay_1", 42)

            instance.insertUpdateWallet.assert_called_once_with(42, "order_1")

    def test_skips_wallet_credit_on_duplicate_webhook(self):
        """invokeCallToDatabase must NOT call insertUpdateWallet again when
        updatePaymentStatus reports the payment was already processed -
        this is the fix for a double-credit bug on retried webhooks."""
        with patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence:
            instance = MockPersistence.return_value
            instance.updatePaymentStatus.return_value = False

            RazorPayManagerService().invokeCallToDatabase("order_1", "pay_1", 42)

            instance.insertUpdateWallet.assert_not_called()

    def test_three_duplicate_webhook_deliveries_credit_wallet_exactly_once(self):
        """Simulates 3 webhook deliveries for one payment. Only the first
        should reach insertUpdateWallet."""
        with patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence:
            instance = MockPersistence.return_value
            # 1st delivery wins the PENDING->SUCCESS race; 2nd and 3rd are duplicates
            instance.updatePaymentStatus.side_effect = [True, False, False]

            svc = RazorPayManagerService()
            for _ in range(3):
                svc.invokeCallToDatabase("order_1", "pay_1", 42)

            assert instance.insertUpdateWallet.call_count == 1

    def test_updates_status_with_correct_arguments(self):
        with patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence:
            instance = MockPersistence.return_value
            instance.updatePaymentStatus.return_value = True

            RazorPayManagerService().invokeCallToDatabase("order_xyz", "pay_abc", 99)

            instance.updatePaymentStatus.assert_called_once_with("order_xyz", "pay_abc", 99)


class TestVerifyPaymentSignatureFromWebhook:
    """verify_payment_signatureFromWebHook is the synchronous verification
    path that both checks the HMAC signature AND performs the (idempotent)
    DB update + wallet credit inline."""

    def test_valid_signature_updates_status_and_credits_wallet(self):
        fake_client = MagicMock()
        fake_client.utility.verify_payment_signature.return_value = None  # no raise = valid

        with patch("service.razorpay.RazorPayMangerService.razorpay.Client", return_value=fake_client), \
             patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence, \
             patch.dict("os.environ", {"RAZORPAY_API_KEY": "key", "RAZORPAY_SECRET_KEY": "secret"}):
            instance = MockPersistence.return_value

            result = RazorPayManagerService().verify_payment_signatureFromWebHook(
                "order_1", "pay_1", "sig_ignored_since_client_is_mocked", 42
            )

        assert result is True
        instance.updatePaymentStatus.assert_called_once_with("order_1", "pay_1", 42)
        instance.insertUpdateWallet.assert_called_once_with(42, "order_1")

    def test_invalid_signature_does_not_touch_database(self):
        """An invalid/failed signature check must never reach the DB
        update or wallet-credit calls, and must fail cleanly (return
        False) rather than raising (fixed regression — the except block
        previously did `print("..."+ex)` on an Exception instance,
        raising its own TypeError instead of returning False)."""
        fake_client = MagicMock()
        fake_client.utility.verify_payment_signature.side_effect = Exception("Signature verification failed")

        with patch("service.razorpay.RazorPayMangerService.razorpay.Client", return_value=fake_client), \
             patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence, \
             patch.dict("os.environ", {"RAZORPAY_API_KEY": "key", "RAZORPAY_SECRET_KEY": "secret"}):
            instance = MockPersistence.return_value

            result = RazorPayManagerService().verify_payment_signatureFromWebHook(
                "order_1", "pay_1", "bad_sig", 42
            )

        assert result is False
        instance.updatePaymentStatus.assert_not_called()
        instance.insertUpdateWallet.assert_not_called()
