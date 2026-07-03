"""
Unit tests for the Razorpay wallet-credit idempotency fix.

Bug: retried/duplicate `payment.captured` webhook deliveries each
unconditionally re-credited the wallet (500 -> 1500 for 3 deliveries)
because insertUpdateWallet() had no guard tied to whether the payment
had already been processed.

Fix: updatePaymentStatus() now performs the PENDING->SUCCESS transition
atomically and returns True only on the delivery that actually performed
it; invokeCallToDatabase() only credits the wallet when that's True.
"""

from unittest.mock import MagicMock, patch

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
        this is the fix for the 500 -> 1500 triple-credit bug."""
        with patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence:
            instance = MockPersistence.return_value
            instance.updatePaymentStatus.return_value = False

            RazorPayManagerService().invokeCallToDatabase("order_1", "pay_1", 42)

            instance.insertUpdateWallet.assert_not_called()

    def test_three_duplicate_webhook_deliveries_credit_wallet_exactly_once(self):
        """Simulates the exact reported bug: 3 webhook deliveries for one
        payment. Only the first should reach insertUpdateWallet."""
        with patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence:
            instance = MockPersistence.return_value
            # 1st delivery wins the PENDING->SUCCESS race; 2nd and 3rd are duplicates
            instance.updatePaymentStatus.side_effect = [True, False, False]

            svc = RazorPayManagerService()
            for _ in range(3):
                svc.invokeCallToDatabase("order_1", "pay_1", 42)

            assert instance.insertUpdateWallet.call_count == 1
