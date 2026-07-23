"""
Tests for ticket 12 groundwork: MasterAccountEntitlementPersistence.

This table/class has no live callers yet (see its module docstring) - these
tests only cover its own input validation and the caller-owned-cursor insert
path, using a mocked cursor (no real DB).
"""

from unittest.mock import MagicMock, patch

import pytest

from database.masterAccountEntitlementPersistence import MasterAccountEntitlementPersistence


class TestCreateEntitlementValidation:

    def test_invalid_internal_order_id_is_rejected(self):
        with pytest.raises(ValueError):
            MasterAccountEntitlementPersistence.create_entitlement(0, 1, "RELIANCE", "BUY", 10, MagicMock())

    def test_invalid_user_id_is_rejected(self):
        with pytest.raises(ValueError):
            MasterAccountEntitlementPersistence.create_entitlement(1, 0, "RELIANCE", "BUY", 10, MagicMock())

    def test_empty_symbol_is_rejected(self):
        with pytest.raises(ValueError):
            MasterAccountEntitlementPersistence.create_entitlement(1, 1, "  ", "BUY", 10, MagicMock())

    def test_invalid_side_is_rejected(self):
        with pytest.raises(ValueError):
            MasterAccountEntitlementPersistence.create_entitlement(1, 1, "RELIANCE", "SHORT", 10, MagicMock())

    def test_non_positive_quantity_is_rejected(self):
        with pytest.raises(ValueError):
            MasterAccountEntitlementPersistence.create_entitlement(1, 1, "RELIANCE", "BUY", 0, MagicMock())

    @patch("database.masterAccountEntitlementPersistence.QueryLoader")
    def test_valid_entitlement_is_inserted_with_null_broker_order_id_by_default(self, mock_loader):
        mock_loader.get.return_value = "INSERT ..."
        cursor = MagicMock()
        cursor.fetchone.return_value = (55,)

        entitlement_id = MasterAccountEntitlementPersistence.create_entitlement(
            1, 1, "RELIANCE", "BUY", 10, cursor
        )

        assert entitlement_id == 55
        params = cursor.execute.call_args.args[1]
        assert params[0] is None  # broker_order_id defaults to None

    @patch("database.masterAccountEntitlementPersistence.QueryLoader")
    def test_missing_returned_row_raises(self, mock_loader):
        mock_loader.get.return_value = "INSERT ..."
        cursor = MagicMock()
        cursor.fetchone.return_value = None

        with pytest.raises(Exception):
            MasterAccountEntitlementPersistence.create_entitlement(1, 1, "RELIANCE", "BUY", 10, cursor)


class TestSetBrokerOrderIdValidation:

    def test_invalid_entitlement_id_is_rejected(self):
        with pytest.raises(ValueError):
            MasterAccountEntitlementPersistence.set_broker_order_id(0, "SHOONYA123", MagicMock())

    def test_empty_broker_order_id_is_rejected(self):
        with pytest.raises(ValueError):
            MasterAccountEntitlementPersistence.set_broker_order_id(1, "  ", MagicMock())
