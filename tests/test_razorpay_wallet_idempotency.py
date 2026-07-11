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

    def test_creates_new_wallet_when_none_exists(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "wallet_id": None,
            "balance": 0.00,
            "transaction_amount": 500.0,
            "transaction_status": "2",
        }
        conn.cursor.return_value = cursor
        with patch(
            "database.razorpaypersistence.RazorPayPersistence.PostgresConnectionFactory.create_connection",
            return_value=conn,
        ):
            RazorPayPersistence().insertUpdateWallet(42, "order_1")

        insert_call = cursor.execute.call_args_list[-1]
        assert insert_call.args[1] == (42, 500.0)

    def test_adds_transaction_amount_to_existing_balance(self):
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
        assert update_call.args[1] == (1500.0, 42)

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
