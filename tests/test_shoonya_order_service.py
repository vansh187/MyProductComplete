"""
Tests for service/shoonyaOrderService.py - the correctness-critical field/code
translation layer between our internal order representation and Shoonya's
real place_order/modify_order/cancel_order API (confirmed from Shoonya's own
ShoonyaApi-py repo README/tests, not guessed).

The previous dead adapter (service/brokerAdapters/shoonya_adapter.py) used
field names/codes that don't match the real API at all - these tests exist
specifically to prevent that class of bug from recurring here.

No real Shoonya call is ever made - the underlying `_api` is a MagicMock.
"""

from unittest.mock import MagicMock

import pytest

from service.shoonyaOrderService import ShoonyaOrderMappingError, ShoonyaOrderService


def _service():
    mock_api = MagicMock()
    return ShoonyaOrderService(mock_api), mock_api


class TestPlaceOrderFieldMapping:

    def test_buy_maps_to_B(self):
        service, mock_api = _service()
        service.place_order(side="BUY", product_type="MIS", exchange="NFO", tradingsymbol="X",
                             quantity=75, order_type="LIMIT", price=100.0)
        assert mock_api.place_order.call_args.kwargs["buy_or_sell"] == "B"

    def test_sell_maps_to_S(self):
        service, mock_api = _service()
        service.place_order(side="SELL", product_type="MIS", exchange="NFO", tradingsymbol="X",
                             quantity=75, order_type="LIMIT", price=100.0)
        assert mock_api.place_order.call_args.kwargs["buy_or_sell"] == "S"

    @pytest.mark.parametrize("product_type,expected_code", [
        ("CNC", "C"), ("NRML", "M"), ("MIS", "I"),
    ])
    def test_product_type_mapping(self, product_type, expected_code):
        service, mock_api = _service()
        service.place_order(side="BUY", product_type=product_type, exchange="NFO", tradingsymbol="X",
                             quantity=75, order_type="LIMIT", price=100.0)
        assert mock_api.place_order.call_args.kwargs["product_type"] == expected_code

    @pytest.mark.parametrize("order_type,expected_code", [
        ("MARKET", "MKT"), ("LIMIT", "LMT"), ("STOP", "SL-MKT"), ("STOPLIMIT", "SL-LMT"),
    ])
    def test_price_type_mapping(self, order_type, expected_code):
        service, mock_api = _service()
        service.place_order(side="BUY", product_type="MIS", exchange="NFO", tradingsymbol="X",
                             quantity=75, order_type=order_type, price=100.0, trigger_price=95.0)
        assert mock_api.place_order.call_args.kwargs["price_type"] == expected_code

    def test_unmapped_side_raises_mapping_error_not_silently_defaulted(self):
        """A bad/unknown side value must never silently become a guessed
        buy_or_sell code - that's a real financial risk (wrong direction)."""
        service, mock_api = _service()
        with pytest.raises(ShoonyaOrderMappingError):
            service.place_order(side="SHORT", product_type="MIS", exchange="NFO", tradingsymbol="X",
                                 quantity=75, order_type="LIMIT", price=100.0)
        mock_api.place_order.assert_not_called()

    def test_market_order_sends_zero_price(self):
        service, mock_api = _service()
        service.place_order(side="BUY", product_type="MIS", exchange="NFO", tradingsymbol="X",
                             quantity=75, order_type="MARKET", price=None)
        assert mock_api.place_order.call_args.kwargs["price"] == 0.0

    def test_stop_market_sends_zero_price_but_real_trigger(self):
        service, mock_api = _service()
        service.place_order(side="SELL", product_type="MIS", exchange="NFO", tradingsymbol="X",
                             quantity=75, order_type="STOP", price=None, trigger_price=95.0)
        kwargs = mock_api.place_order.call_args.kwargs
        assert kwargs["price"] == 0.0
        assert kwargs["trigger_price"] == 95.0

    def test_stoplimit_sends_real_limit_price_and_trigger(self):
        service, mock_api = _service()
        service.place_order(side="SELL", product_type="MIS", exchange="NFO", tradingsymbol="X",
                             quantity=75, order_type="STOPLIMIT", price=94.5, trigger_price=95.0)
        kwargs = mock_api.place_order.call_args.kwargs
        assert kwargs["price"] == 94.5
        assert kwargs["trigger_price"] == 95.0

    def test_tradingsymbol_and_quantity_passed_through_unchanged(self):
        service, mock_api = _service()
        service.place_order(side="BUY", product_type="MIS", exchange="NFO",
                             tradingsymbol="NIFTY14JUL26C23950", quantity=150, order_type="LIMIT", price=100.0)
        kwargs = mock_api.place_order.call_args.kwargs
        assert kwargs["tradingsymbol"] == "NIFTY14JUL26C23950"
        assert kwargs["quantity"] == 150

    def test_returns_raw_broker_response_unmodified(self):
        service, mock_api = _service()
        mock_api.place_order.return_value = {"stat": "Ok", "norenordno": "12345"}
        result = service.place_order(side="BUY", product_type="MIS", exchange="NFO", tradingsymbol="X",
                                      quantity=75, order_type="LIMIT", price=100.0)
        assert result == {"stat": "Ok", "norenordno": "12345"}


class TestOtherBrokerCalls:

    def test_cancel_order_passes_orderno(self):
        service, mock_api = _service()
        service.cancel_order("20052000000017")
        mock_api.cancel_order.assert_called_once_with(orderno="20052000000017")

    def test_modify_order_only_passes_provided_fields(self):
        service, mock_api = _service()
        service.modify_order(orderno="123", exchange="NFO", tradingsymbol="X", newquantity=150)
        kwargs = mock_api.modify_order.call_args.kwargs
        assert kwargs["newquantity"] == 150
        assert "newprice" not in kwargs
        assert "newtrigger_price" not in kwargs

    def test_get_order_book_delegates(self):
        service, mock_api = _service()
        service.get_order_book()
        mock_api.get_order_book.assert_called_once()

    def test_get_positions_delegates(self):
        service, mock_api = _service()
        service.get_positions()
        mock_api.get_positions.assert_called_once()

    def test_get_limits_delegates(self):
        service, mock_api = _service()
        service.get_limits()
        mock_api.get_limits.assert_called_once()


def test_none_api_rejected_at_construction():
    with pytest.raises(ValueError):
        ShoonyaOrderService(None)
