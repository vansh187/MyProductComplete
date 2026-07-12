"""
End-to-end tests for the POST /v1/razorpay-webhook route
(api/VerifyFundTransaction.py), using the REAL razorpay SDK's HMAC-SHA256
signature verification (not mocked) against the actual
RAZORPAY_WEBHOOK_SECRET configured in .env - proving genuine
interoperability with Razorpay's real signing scheme, not just that our
own mocked assumptions about it hold.

This guards the fix in service/razorpay/RazorPayMangerService.py
(webhook_secret now read from os.getenv("RAZORPAY_WEBHOOK_SECRET")
instead of the hardcoded "WEBHOOK_9897" literal) against a regression -
if the fix had used the wrong env var name, wrong encoding, or broken the
HMAC comparison somehow, a signature computed with the real secret would
fail to verify here exactly as it would against Razorpay's real servers.

Only the DB persistence layer (RazorPayPersistence) is mocked - no real
Postgres connection is made. The HTTP layer, signature verification, and
background-task wiring all run for real.
"""

import hashlib
import hmac
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.VerifyFundTransaction import router

load_dotenv()

WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _sign(body_bytes: bytes, secret: str) -> str:
    """Computes a signature the exact same way Razorpay's real webhook
    sender does, and the exact same way razorpay.utility.Utility.
    verify_signature() verifies it - HMAC-SHA256(secret, body), hex."""
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def _payment_captured_payload(order_id="order_real_test", payment_id="pay_real_test", user_id="42", amount=50000):
    return {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "notes": {"user_id": user_id, "module": "wallet_funding"},
                }
            }
        },
    }


@pytest.mark.skipif(not WEBHOOK_SECRET, reason="RAZORPAY_WEBHOOK_SECRET not configured in this environment")
class TestRazorpayWebhookEndpointRealSignature:

    def test_genuinely_signed_payload_is_accepted(self):
        app = _make_app()
        payload = _payment_captured_payload()
        body_bytes = json.dumps(payload).encode("utf-8")
        signature = _sign(body_bytes, WEBHOOK_SECRET)

        # Only the DB persistence layer is mocked - Razorpay(), the real
        # RazorPayManagerService, and the real razorpay SDK's HMAC
        # verification all run unmocked.
        with patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence:
            mock_persistence_instance = MockPersistence.return_value
            mock_persistence_instance.updatePaymentStatus.return_value = True

            client = TestClient(app)
            resp = client.post(
                "/v1/razorpay-webhook",
                content=body_bytes,
                headers={"x-razorpay-signature": signature, "content-type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"
        # The background task must have actually run and reached the DB
        # layer for this genuinely-signed, well-formed payment.captured event.
        mock_persistence_instance.updatePaymentStatus.assert_called_once_with(
            "order_real_test", "pay_real_test", "42"
        )
        mock_persistence_instance.insertUpdateWallet.assert_called_once_with("42", "order_real_test")

    def test_tampered_body_after_signing_is_rejected(self):
        """Signature computed over the original body must not verify
        against a body that was altered afterward (e.g. a MITM changing
        the amount) - proves the verification actually binds to body
        content, not just checking a header exists."""
        app = _make_app()
        original_payload = _payment_captured_payload(amount=50000)
        signature = _sign(json.dumps(original_payload).encode("utf-8"), WEBHOOK_SECRET)

        tampered_payload = _payment_captured_payload(amount=99999999)
        tampered_body = json.dumps(tampered_payload).encode("utf-8")

        with patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence:
            client = TestClient(app)
            resp = client.post(
                "/v1/razorpay-webhook",
                content=tampered_body,
                headers={"x-razorpay-signature": signature, "content-type": "application/json"},
            )

        assert resp.status_code == 400
        assert "Invalid webhook signature" in resp.json()["detail"]
        MockPersistence.return_value.insertUpdateWallet.assert_not_called()

    def test_wrong_secret_produces_rejected_signature(self):
        """Simulates the exact regression this fix targets: if a DIFFERENT
        secret than the one actually configured in RAZORPAY_WEBHOOK_SECRET
        were ever used (e.g. a stale hardcoded literal, or an out-of-sync
        .env value), a signature computed with that wrong secret must be
        rejected, not accepted. Deliberately does not assume any specific
        string is "the wrong one" - WEBHOOK_9897 turned out to be the
        REAL secret Razorpay's dashboard uses for this endpoint (confirmed
        via a live test), so this just uses an arbitrary different value
        instead of hardcoding an assumption about which literal is wrong."""
        app = _make_app()
        payload = _payment_captured_payload()
        body_bytes = json.dumps(payload).encode("utf-8")
        wrong_signature = _sign(body_bytes, "some_other_secret_not_configured_anywhere")

        with patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence:
            client = TestClient(app)
            resp = client.post(
                "/v1/razorpay-webhook",
                content=body_bytes,
                headers={"x-razorpay-signature": wrong_signature, "content-type": "application/json"},
            )

        assert resp.status_code == 400
        MockPersistence.return_value.insertUpdateWallet.assert_not_called()

    def test_missing_signature_header_is_rejected(self):
        app = _make_app()
        body_bytes = json.dumps(_payment_captured_payload()).encode("utf-8")

        with patch("service.razorpay.RazorPayMangerService.RazorPayPersistence") as MockPersistence:
            client = TestClient(app)
            resp = client.post(
                "/v1/razorpay-webhook",
                content=body_bytes,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 400
        MockPersistence.return_value.insertUpdateWallet.assert_not_called()

    def test_malformed_json_body_rejected_before_signature_check(self):
        app = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/v1/razorpay-webhook",
            content=b"not valid json{{{",
            headers={"x-razorpay-signature": "irrelevant", "content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert "Malformed JSON" in resp.json()["detail"]
