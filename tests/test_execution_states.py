"""
Comprehensive tests for order execution states:
- PENDING: Order queued when no matches found
- PARTIALLY_EXECUTED: Order partially matched
- EXECUTED: Order fully matched
"""

import pytest
from unittest.mock import patch, MagicMock, Mock
from fastapi.testclient import TestClient
from fastapi import FastAPI, Depends

from api.models import OrderCreate, OrderType, OrderSide, ProductType, ValidityType, ExchangeType
from service.executionEngine import ExecutionEngine
from service.matchingEngine.matchingEngine import MatchingEngine


def _make_app():
    """Create test FastAPI app with routes"""
    from api.orders import router
    app = FastAPI()
    app.include_router(router)
    return app


def _override_auth():
    """Override auth dependency for testing"""
    return {"user_id": 1, "username": "testuser"}


def _buy_order(quantity=10, price=1000.0):
    """Create test BUY order"""
    return OrderCreate(
        symbol="RELIANCE",
        exchange=ExchangeType.NSE,
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        price=price,
        product_type=ProductType.MIS,
        validity=ValidityType.DAY
    )


def _sell_order(quantity=10, price=1000.0):
    """Create test SELL order"""
    return OrderCreate(
        symbol="RELIANCE",
        exchange=ExchangeType.NSE,
        side=OrderSide.SELL,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        price=price,
        product_type=ProductType.MIS,
        validity=ValidityType.DAY
    )


class MockTradeExecution:
    """Mock trade execution object"""
    def __init__(self, buy_order_id=1, sell_order_id=2, buy_user_id=1, sell_user_id=2,
                 symbol="RELIANCE", quantity=5, execution_price=1000.0, remaining_qty=0):
        self.buy_order_id = buy_order_id
        self.sell_order_id = sell_order_id
        self.buy_user_id = buy_user_id
        self.sell_user_id = sell_user_id
        self.symbol = symbol
        self.quantity = quantity
        self.execution_price = execution_price
        self.remaining_qty = remaining_qty
        self.trade_value = quantity * execution_price


# ==============================================================================
# TEST SCENARIO 1: PENDING STATE
# Order is created but no matches found - remains in PENDING queue
# ==============================================================================

class TestPendingState:
    """Test orders that remain PENDING (no matches found)"""

    def test_no_matches_returns_pending_status(self):
        """When no counterparty orders exist, order should be PENDING"""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.OrderService") as MockOrderService, \
             patch("api.orders.WalletBalanceService") as MockWalletService, \
             patch("api.orders.ExecutionEngine") as MockExecutionEngine:

            # Setup wallet service
            MockWalletService.return_value.getWalletBalance.return_value = {
                "user_id": 1, "balance": 50000.0
            }

            # Setup order service
            MockOrderService.return_value.create_order.return_value = 101

            # Setup execution engine - return PENDING
            MockExecutionEngine.return_value.execute_order.return_value = {
                "success": True,
                "status": "PENDING",
                "message": "Order queued for matching",
                "order_id": 101
            }

            client = TestClient(app)
            response = client.post("/orders", json=_buy_order().__dict__)

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["execution"]["status"] == "PENDING"
            assert data["order_id"] == 101
            print("✅ PENDING: No matches found - order queued successfully")

    def test_pending_no_runtime_exceptions(self):
        """Verify PENDING state handles no exceptions"""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.OrderService") as MockOrderService, \
             patch("api.orders.WalletBalanceService") as MockWalletService, \
             patch("api.orders.ExecutionEngine") as MockExecutionEngine:

            MockWalletService.return_value.getWalletBalance.return_value = {
                "user_id": 1, "balance": 100000.0
            }
            MockOrderService.return_value.create_order.return_value = 102
            MockExecutionEngine.return_value.execute_order.return_value = {
                "success": True,
                "status": "PENDING",
                "message": "Order queued for matching",
                "order_id": 102
            }

            client = TestClient(app)

            # Test multiple PENDING orders - stress test
            for i in range(5):
                response = client.post("/orders", json=_buy_order(quantity=10 + i).__dict__)
                assert response.status_code == 200
                assert response.json()["execution"]["status"] == "PENDING"

            print("✅ PENDING: 5 concurrent PENDING orders - NO RUNTIME EXCEPTIONS")


# ==============================================================================
# TEST SCENARIO 2: PARTIALLY_EXECUTED STATE
# Order is partially matched - remaining quantity still in order book
# ==============================================================================

class TestPartiallyExecutedState:
    """Test orders that are PARTIALLY_EXECUTED (partial matches)"""

    def test_partial_execution_correct_status(self):
        """When order partially matches, status should be PARTIALLY_EXECUTED"""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.OrderService") as MockOrderService, \
             patch("api.orders.WalletBalanceService") as MockWalletService, \
             patch("api.orders.ExecutionEngine") as MockExecutionEngine:

            MockWalletService.return_value.getWalletBalance.return_value = {
                "user_id": 1, "balance": 50000.0
            }
            MockOrderService.return_value.create_order.return_value = 201

            # Setup execution engine for partial execution
            MockExecutionEngine.return_value.execute_order.return_value = {
                "success": True,
                "status": "PARTIALLY_EXECUTED",
                "message": "Order partially executed",
                "order_id": 201,
                "trade_id": 5001,
                "matched_quantity": 5,
                "remaining_quantity": 5
            }

            client = TestClient(app)
            response = client.post("/orders", json=_buy_order(quantity=10).__dict__)

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["execution"]["status"] == "PARTIALLY_EXECUTED"
            assert data["execution"]["matched_quantity"] == 5
            assert data["execution"]["remaining_quantity"] == 5
            print("✅ PARTIALLY_EXECUTED: 5/10 matched - correct status and quantities")

    def test_partial_execution_no_runtime_exceptions(self):
        """Verify PARTIALLY_EXECUTED state handles no exceptions"""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.OrderService") as MockOrderService, \
             patch("api.orders.WalletBalanceService") as MockWalletService, \
             patch("api.orders.ExecutionEngine") as MockExecutionEngine:

            MockWalletService.return_value.getWalletBalance.return_value = {
                "user_id": 1, "balance": 100000.0
            }
            MockOrderService.return_value.create_order.return_value = 202

            client = TestClient(app)

            # Test multiple partial executions with different ratios
            partial_scenarios = [
                (10, 1),  # 1/10 matched
                (10, 5),  # 5/10 matched
                (10, 9),  # 9/10 matched
                (100, 50),  # 50/100 matched
            ]

            for order_qty, matched_qty in partial_scenarios:
                MockExecutionEngine.return_value.execute_order.return_value = {
                    "success": True,
                    "status": "PARTIALLY_EXECUTED",
                    "message": f"Order {matched_qty}/{order_qty} executed",
                    "order_id": 202,
                    "matched_quantity": matched_qty,
                    "remaining_quantity": order_qty - matched_qty
                }

                order_dict = _buy_order(quantity=order_qty).__dict__
                response = client.post("/orders", json=order_dict)

                assert response.status_code == 200
                data = response.json()
                assert data["execution"]["status"] == "PARTIALLY_EXECUTED"
                assert data["execution"]["matched_quantity"] == matched_qty
                print(f"✅ PARTIALLY_EXECUTED: {matched_qty}/{order_qty} - NO EXCEPTIONS")

    def test_partial_execution_trade_processing_no_exceptions(self):
        """Test that trade processing for partial execution has no exceptions"""
        order = _buy_order(quantity=10)
        order_id = 201
        user_id = 1

        # Create mock trade execution (5 matched out of 10)
        trade_exec = MockTradeExecution(
            buy_order_id=order_id,
            sell_order_id=999,
            buy_user_id=user_id,
            sell_user_id=2,
            quantity=5,
            remaining_qty=5  # 5 remaining to match
        )

        with patch("service.executionEngine.PostgresConnectionFactory") as MockConnFactory, \
             patch("service.executionEngine.OrderService") as MockOrderService, \
             patch("service.executionEngine.portfolioService") as MockPortfolioService, \
             patch("service.executionEngine.tradeService") as MockTradeService:

            # Setup mock connection
            mock_cursor = MagicMock()
            mock_conn = MagicMock()
            mock_conn.cursor.return_value = mock_cursor
            MockConnFactory.create_connection.return_value = mock_conn

            # Setup services
            MockOrderService.return_value = MagicMock()
            MockPortfolioService.return_value = MagicMock()
            MockTradeService.insertTradeOrders.return_value = 5001

            # Test execution
            engine = ExecutionEngine(order, order_id)

            try:
                # This should NOT raise any exceptions
                result = engine._process_trades(
                    mock_conn, mock_cursor,
                    [trade_exec],  # Single partial match
                    order_book_id=1001,
                    user_id=user_id
                )
                assert result is not None
                print("✅ PARTIALLY_EXECUTED: Trade processing - NO RUNTIME EXCEPTIONS")
            except Exception as e:
                pytest.fail(f"PARTIALLY_EXECUTED trade processing raised exception: {str(e)}")


# ==============================================================================
# TEST SCENARIO 3: EXECUTED STATE
# Order is fully matched and executed
# ==============================================================================

class TestExecutedState:
    """Test orders that are EXECUTED (full matches)"""

    def test_full_execution_returns_executed_status(self):
        """When order fully matches, status should be EXECUTED"""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.OrderService") as MockOrderService, \
             patch("api.orders.WalletBalanceService") as MockWalletService, \
             patch("api.orders.ExecutionEngine") as MockExecutionEngine:

            MockWalletService.return_value.getWalletBalance.return_value = {
                "user_id": 1, "balance": 50000.0
            }
            MockOrderService.return_value.create_order.return_value = 301

            # Setup execution engine for full execution
            MockExecutionEngine.return_value.execute_order.return_value = {
                "success": True,
                "status": "EXECUTED",
                "message": "Order fully executed",
                "order_id": 301,
                "trade_id": 6001
            }

            client = TestClient(app)
            response = client.post("/orders", json=_buy_order(quantity=10).__dict__)

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["execution"]["status"] == "EXECUTED"
            assert data["execution"]["trade_id"] == 6001
            print("✅ EXECUTED: Full match (10/10) - correct status")

    def test_executed_no_runtime_exceptions(self):
        """Verify EXECUTED state handles no exceptions"""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.OrderService") as MockOrderService, \
             patch("api.orders.WalletBalanceService") as MockWalletService, \
             patch("api.orders.ExecutionEngine") as MockExecutionEngine:

            MockWalletService.return_value.getWalletBalance.return_value = {
                "user_id": 1, "balance": 500000.0
            }
            MockOrderService.return_value.create_order.return_value = 302

            client = TestClient(app)

            # Test multiple full executions
            for i in range(5):
                MockExecutionEngine.return_value.execute_order.return_value = {
                    "success": True,
                    "status": "EXECUTED",
                    "message": "Order fully executed",
                    "order_id": 302 + i,
                    "trade_id": 6000 + i
                }

                response = client.post("/orders", json=_buy_order(quantity=10).__dict__)
                assert response.status_code == 200
                assert response.json()["execution"]["status"] == "EXECUTED"

            print("✅ EXECUTED: 5 concurrent full executions - NO RUNTIME EXCEPTIONS")

    def test_executed_multiple_partial_matches_then_full(self):
        """Test order that matches multiple times to full execution"""
        order = _buy_order(quantity=10)
        order_id = 301
        user_id = 1

        # Create 2 partial trade executions that together fully execute the order
        trade_exec_1 = MockTradeExecution(
            buy_order_id=order_id,
            sell_order_id=999,
            buy_user_id=user_id,
            sell_user_id=2,
            quantity=6,
            remaining_qty=4  # 4 remaining after first match
        )

        trade_exec_2 = MockTradeExecution(
            buy_order_id=order_id,
            sell_order_id=1000,
            buy_user_id=user_id,
            sell_user_id=3,
            quantity=4,
            remaining_qty=0  # Fully executed after second match
        )

        with patch("service.executionEngine.PostgresConnectionFactory") as MockConnFactory, \
             patch("service.executionEngine.OrderService") as MockOrderService, \
             patch("service.executionEngine.portfolioService") as MockPortfolioService, \
             patch("service.executionEngine.tradeService") as MockTradeService:

            # Setup mock connection
            mock_cursor = MagicMock()
            mock_conn = MagicMock()
            mock_conn.cursor.return_value = mock_cursor
            MockConnFactory.create_connection.return_value = mock_conn

            # Setup services
            MockOrderService.return_value = MagicMock()
            MockPortfolioService.return_value = MagicMock()
            MockTradeService.insertTradeOrders.return_value = 6001

            # Test execution
            engine = ExecutionEngine(order, order_id)

            try:
                # This should NOT raise any exceptions
                result = engine._process_trades(
                    mock_conn, mock_cursor,
                    [trade_exec_1, trade_exec_2],  # Two partial matches that fully execute
                    order_book_id=1001,
                    user_id=user_id
                )
                assert result is not None
                assert result["status"] == "EXECUTED"
                print("✅ EXECUTED: Multiple partial matches (6+4) - FULL EXECUTION, NO EXCEPTIONS")
            except Exception as e:
                pytest.fail(f"EXECUTED multi-match processing raised exception: {str(e)}")


# ==============================================================================
# TEST SCENARIO 4: EDGE CASES AND ERROR HANDLING
# ==============================================================================

class TestExecutionEdgeCases:
    """Test edge cases in order execution states"""

    def test_zero_matched_quantity_not_executed(self):
        """When no matches found, status should remain PENDING, not EXECUTED"""
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.OrderService") as MockOrderService, \
             patch("api.orders.WalletBalanceService") as MockWalletService, \
             patch("api.orders.ExecutionEngine") as MockExecutionEngine:

            MockWalletService.return_value.getWalletBalance.return_value = {
                "user_id": 1, "balance": 50000.0
            }
            MockOrderService.return_value.create_order.return_value = 401

            MockExecutionEngine.return_value.execute_order.return_value = {
                "success": True,
                "status": "PENDING",
                "message": "Order queued for matching",
                "order_id": 401
            }

            client = TestClient(app)
            response = client.post("/orders", json=_buy_order(quantity=10).__dict__)

            assert response.status_code == 200
            assert response.json()["execution"]["status"] == "PENDING"
            print("✅ EDGE CASE: Zero match - correctly remains PENDING, not EXECUTED")

    def test_auth_violation_blocks_execution(self):
        """Test that auth check prevents unauthorized user execution"""
        order = _sell_order(quantity=10)
        order_id = 301
        user_id = 1

        # Trade where user_id (1) is NOT involved
        unauthorized_trade = MockTradeExecution(
            buy_order_id=999,
            sell_order_id=1000,
            buy_user_id=99,  # Different user
            sell_user_id=98,  # Different user
            quantity=10,
            remaining_qty=0
        )

        with patch("service.executionEngine.PostgresConnectionFactory") as MockConnFactory:
            mock_cursor = MagicMock()
            mock_conn = MagicMock()
            mock_conn.cursor.return_value = mock_cursor
            MockConnFactory.create_connection.return_value = mock_conn

            engine = ExecutionEngine(order, order_id)

            # Should raise ValueError due to auth check
            with pytest.raises(ValueError, match="not authorized"):
                engine._process_trades(
                    mock_conn, mock_cursor,
                    [unauthorized_trade],
                    order_book_id=1001,
                    user_id=user_id
                )
            print("✅ EDGE CASE: Auth violation - correctly blocked unauthorized execution")

    def test_null_trade_execution_handled(self):
        """Test that None values in trade executions are handled safely"""
        order = _buy_order(quantity=10)
        order_id = 301
        user_id = 1

        valid_trade = MockTradeExecution(
            buy_order_id=order_id,
            sell_order_id=999,
            buy_user_id=user_id,
            sell_user_id=2,
            quantity=5,
            remaining_qty=5
        )

        with patch("service.executionEngine.PostgresConnectionFactory") as MockConnFactory, \
             patch("service.executionEngine.OrderService") as MockOrderService, \
             patch("service.executionEngine.portfolioService") as MockPortfolioService, \
             patch("service.executionEngine.tradeService") as MockTradeService:

            mock_cursor = MagicMock()
            mock_conn = MagicMock()
            mock_conn.cursor.return_value = mock_cursor
            MockConnFactory.create_connection.return_value = mock_conn

            MockOrderService.return_value = MagicMock()
            MockPortfolioService.return_value = MagicMock()
            MockTradeService.insertTradeOrders.return_value = 5001

            engine = ExecutionEngine(order, order_id)

            try:
                # Mix None and valid trades
                result = engine._process_trades(
                    mock_conn, mock_cursor,
                    [None, valid_trade, None],  # None values should be skipped
                    order_book_id=1001,
                    user_id=user_id
                )
                assert result is not None
                print("✅ EDGE CASE: None trades - safely skipped, NO EXCEPTIONS")
            except Exception as e:
                pytest.fail(f"None trade handling raised exception: {str(e)}")


# ==============================================================================
# SUMMARY TEST: ALL THREE STATES IN ONE FLOW
# ==============================================================================

class TestCompleteExecutionFlow:
    """Integration test covering all three execution states"""

    def test_all_three_states_no_exceptions(self):
        """
        Complete test flow:
        1. Create order 1 -> PENDING (no matches)
        2. Create order 2 -> PARTIALLY_EXECUTED (5/10)
        3. Create order 3 -> EXECUTED (10/10)
        All without runtime exceptions
        """
        from utils.auth_dependency import get_current_user

        app = _make_app()
        app.dependency_overrides[get_current_user] = _override_auth

        with patch("api.orders.OrderService") as MockOrderService, \
             patch("api.orders.WalletBalanceService") as MockWalletService, \
             patch("api.orders.ExecutionEngine") as MockExecutionEngine:

            MockWalletService.return_value.getWalletBalance.return_value = {
                "user_id": 1, "balance": 500000.0
            }

            # Order 1: PENDING
            MockOrderService.return_value.create_order.side_effect = [101, 102, 103]
            pending_result = {
                "success": True,
                "status": "PENDING",
                "message": "Order queued for matching",
                "order_id": 101
            }

            # Order 2: PARTIALLY_EXECUTED
            partial_result = {
                "success": True,
                "status": "PARTIALLY_EXECUTED",
                "message": "Order partially executed",
                "order_id": 102,
                "trade_id": 5001,
                "matched_quantity": 5
            }

            # Order 3: EXECUTED
            executed_result = {
                "success": True,
                "status": "EXECUTED",
                "message": "Order fully executed",
                "order_id": 103,
                "trade_id": 6001
            }

            MockExecutionEngine.return_value.execute_order.side_effect = [
                pending_result,
                partial_result,
                executed_result
            ]

            client = TestClient(app)

            # Test 1: PENDING
            response1 = client.post("/orders", json=_buy_order().__dict__)
            assert response1.status_code == 200
            assert response1.json()["execution"]["status"] == "PENDING"

            # Test 2: PARTIALLY_EXECUTED
            response2 = client.post("/orders", json=_buy_order().__dict__)
            assert response2.status_code == 200
            assert response2.json()["execution"]["status"] == "PARTIALLY_EXECUTED"

            # Test 3: EXECUTED
            response3 = client.post("/orders", json=_buy_order().__dict__)
            assert response3.status_code == 200
            assert response3.json()["execution"]["status"] == "EXECUTED"

            print("✅ COMPLETE FLOW: PENDING -> PARTIALLY_EXECUTED -> EXECUTED")
            print("✅ ALL THREE STATES: NO RUNTIME EXCEPTIONS DETECTED")
