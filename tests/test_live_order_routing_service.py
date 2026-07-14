"""
Tests for service/liveOrderRoutingService.py - orchestrates a real Shoonya
order placement for an F&O order.

No real DB/network calls: OrderPersistence and ShoonyaOrderService are both
mocked. The broker's `_api` is never touched directly here (ShoonyaOrderService
is mocked at the LiveOrderRoutingService boundary), consistent with never
letting a test reach a real broker.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from service.liveOrderRoutingService import (
    LiveOrderRejectedError,
    LiveOrderRoutingService,
    LiveOrderStatusUncertainError,
    LotSizeMismatchError,
    live_orders_enabled,
)


def _order(**overrides):
    order = MagicMock()
    order.symbol = "NIFTY14JUL2623950CE"
    order.side = MagicMock(value="BUY")
    order.product_type = MagicMock(value="MIS")
    order.order_type = MagicMock(value="LIMIT")
    order.exchange = MagicMock(value="NFO")
    order.quantity = 75
    order.price = 101.15
    order.trigger_price = None
    for key, value in overrides.items():
        setattr(order, key, value)
    return order


def _instrument(lot_size=75):
    return {"contract_type": "OPTION", "lot_size": lot_size, "token": "12345", "expiry": "2026-07-14"}


class TestLiveOrdersEnabledFlag:

    def test_defaults_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("SHOONYA_LIVE_ORDERS_ENABLED", raising=False)
        assert live_orders_enabled() is False

    def test_true_only_for_exact_string_true(self, monkeypatch):
        monkeypatch.setenv("SHOONYA_LIVE_ORDERS_ENABLED", "true")
        assert live_orders_enabled() is True

    def test_false_for_any_other_value(self, monkeypatch):
        monkeypatch.setenv("SHOONYA_LIVE_ORDERS_ENABLED", "1")
        assert live_orders_enabled() is False
        monkeypatch.setenv("SHOONYA_LIVE_ORDERS_ENABLED", "True")
        assert live_orders_enabled() is True  # case-insensitive by design


class TestLotSizeValidation:

    def test_multiple_of_lot_size_passes(self):
        LiveOrderRoutingService.validate_lot_size(150, 75)  # must not raise

    def test_non_multiple_raises_before_any_broker_call(self):
        with pytest.raises(LotSizeMismatchError):
            LiveOrderRoutingService.validate_lot_size(80, 75)

    def test_missing_lot_size_is_not_validated(self):
        """If lot_size is unknown (None/0), don't block placement on a guess -
        the broker's own validation is the backstop in that case."""
        LiveOrderRoutingService.validate_lot_size(80, None)
        LiveOrderRoutingService.validate_lot_size(80, 0)


class TestPlaceLiveOrder:

    def _service_with_mocked_broker(self):
        with patch("service.liveOrderRoutingService.OrderPersistence") as MockPersistence, \
             patch("service.liveOrderRoutingService.ShoonyaOrderService") as MockShoonyaOrderService:
            service = LiveOrderRoutingService(shoonya_api=MagicMock())
            return service, MockPersistence.return_value, MockShoonyaOrderService.return_value

    def test_lot_size_mismatch_rejected_before_broker_call(self):
        service, mock_persistence, mock_shoonya_order_service = self._service_with_mocked_broker()
        order = _order(quantity=80)
        with pytest.raises(LotSizeMismatchError):
            service.place_live_order(order, 101, _instrument(lot_size=75))
        mock_shoonya_order_service.place_order.assert_not_called()

    def test_successful_placement_persists_broker_order_id(self):
        service, mock_persistence, mock_shoonya_order_service = self._service_with_mocked_broker()
        mock_shoonya_order_service.place_order.return_value = {"stat": "Ok", "norenordno": "20052000000017"}

        result = service.place_live_order(_order(), 101, _instrument())

        assert result["broker_order_id"] == "20052000000017"
        assert result["status"] == "PENDING"
        mock_persistence.set_broker_order_id.assert_called_once_with(101, "20052000000017")

    def test_broker_rejection_raises_rejected_error_with_reason(self):
        service, mock_persistence, mock_shoonya_order_service = self._service_with_mocked_broker()
        mock_shoonya_order_service.place_order.return_value = {"stat": "Not_Ok", "emsg": "RMS:Margin Exceeds"}

        with pytest.raises(LiveOrderRejectedError) as exc_info:
            service.place_live_order(_order(), 101, _instrument())

        assert exc_info.value.reason == "RMS:Margin Exceeds"
        mock_persistence.set_broker_order_id.assert_not_called()

    def test_success_with_no_order_number_is_status_uncertain_not_success(self):
        """stat=Ok but no norenordno is an anomaly - must not be treated as a
        clean success (nothing to track the order by)."""
        service, mock_persistence, mock_shoonya_order_service = self._service_with_mocked_broker()
        mock_shoonya_order_service.place_order.return_value = {"stat": "Ok"}

        with pytest.raises(LiveOrderStatusUncertainError):
            service.place_live_order(_order(), 101, _instrument())
        mock_persistence.set_broker_order_id.assert_not_called()

    def test_broker_call_timeout_is_status_uncertain_not_rejected(self):
        """A timeout must be distinguished from a clean reject - the order
        may have actually gone through at the broker."""
        service, mock_persistence, mock_shoonya_order_service = self._service_with_mocked_broker()
        service._executor = MagicMock()
        future = MagicMock()
        from concurrent.futures import TimeoutError as FutureTimeoutError
        future.result.side_effect = FutureTimeoutError()
        service._executor.submit.return_value = future

        with pytest.raises(LiveOrderStatusUncertainError):
            service.place_live_order(_order(), 101, _instrument())
        mock_persistence.set_broker_order_id.assert_not_called()

    def test_unexpected_broker_exception_is_status_uncertain_not_rejected(self):
        service, mock_persistence, mock_shoonya_order_service = self._service_with_mocked_broker()
        mock_shoonya_order_service.place_order.side_effect = Exception("connection reset by peer")

        with pytest.raises(LiveOrderStatusUncertainError):
            service.place_live_order(_order(), 101, _instrument())
        mock_persistence.set_broker_order_id.assert_not_called()

    def test_unexpected_response_shape_is_status_uncertain(self):
        service, mock_persistence, mock_shoonya_order_service = self._service_with_mocked_broker()
        mock_shoonya_order_service.place_order.return_value = {"weird": "response"}

        with pytest.raises(LiveOrderStatusUncertainError):
            service.place_live_order(_order(), 101, _instrument())

    def test_broker_persistence_failure_after_success_does_not_lose_the_order(self):
        """If persisting broker_order_id keeps failing across every retry,
        that exception must still propagate (caller can't silently think the
        order isn't tracked) - this documents current behavior rather than
        swallowing the error."""
        service, mock_persistence, mock_shoonya_order_service = self._service_with_mocked_broker()
        mock_shoonya_order_service.place_order.return_value = {"stat": "Ok", "norenordno": "999"}
        mock_persistence.set_broker_order_id.side_effect = Exception("db write failed")

        with pytest.raises(Exception, match="db write failed"):
            service.place_live_order(_order(), 101, _instrument())
        assert mock_persistence.set_broker_order_id.call_count == 3

    def test_transient_persistence_failure_recovers_on_retry(self):
        """A single transient DB blip right after the broker accepted the
        order must not orphan it - a later retry succeeding must NOT raise."""
        service, mock_persistence, mock_shoonya_order_service = self._service_with_mocked_broker()
        mock_shoonya_order_service.place_order.return_value = {"stat": "Ok", "norenordno": "999"}
        mock_persistence.set_broker_order_id.side_effect = [Exception("transient blip"), None]

        result = service.place_live_order(_order(), 101, _instrument())

        assert result["broker_order_id"] == "999"
        assert mock_persistence.set_broker_order_id.call_count == 2
