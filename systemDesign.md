# System Design for MyProductComplete

## Overview

`MyProductComplete` is an API-first Order Management System (OMS) built with FastAPI and MySQL. The application is structured using a layered architecture:

- API layer (`api/`) for request handling and routing
- Service layer (`service/`) for business logic
- Persistence layer (`database/`) for direct database access
- Utility layer (`utils/`) for authentication and support functions

The current implementation includes user authentication, order creation, order retrieval, order cancellation, and portfolio updates.

## Architecture Components

### 1. API Layer

Entry point:
- `app.py`
  - Creates the FastAPI application
  - Includes routers for signup, login, and orders
  - Defines a health endpoint at `/`

Routers:
- `api/signup.py` - user registration endpoint `/signup`
- `api/login.py` - login endpoint `/login`
- `api/orders.py` - order-related endpoints:
  - `GET /orders` - fetch user orders
  - `POST /orders` - create a new order
  - `GET /getOrderById/{orderId}` - retrieve specific order by id
  - `GET /cancelOrderById/{orderId}` - cancel an order

### 2. Authentication

Authentication uses JWT tokens and FastAPI dependency injection:
- `utils/jwt_handler.py`
  - `create_access_token()` generates JWTs with HS256 and expiration
  - `verify_token()` validates incoming tokens
- `utils/auth_dependency.py`
  - `get_current_user()` protects endpoints using `HTTPBearer`
  - Decodes and validates the token, returning the user payload

This means protected endpoints can access `current_user` and obtain `user_id`.

### 3. Service Layer

Business logic is routed through service classes and modules:
- `service/signupService.py`
  - Hashes passwords using `utils/password_hasher.py`
  - Persists new users via `database/signUpPersist.py`
- `service/loginService.py`
  - Verifies credentials using `database/loginPersist.py`
  - Checks password with `verify_password()`
  - Returns JWT token on success
- `service/orderService.py`
  - Delegates order creation, retrieval, and cancellation to persistence
- `service/portfolioService.py`
  - Manages portfolio updates after buy/sell orders

### 4. Persistence Layer

Database operations are centralized in `database/`:
- `database/ConnectionFactory.py` - creates MySQL connections
- `database/orderPersistence.py` - order CRUD operations
- `database/portfolioPersistence.py` - holdings and order status updates
- `database/loginPersist.py` - login user lookup
- `database/signUpPersist.py` - signup user insertion

The persistence layer executes SQL queries and returns raw results to the service layer.

### 5. Database and Schema

The application is designed to use MySQL with environment-driven configuration from `.env`.

Key tables implied by the code:
- `orders`
  - Columns: `id`, `user_id`, `symbol`, `side`, `quantity`, `price`, `status`, `created_at`
- `holdings`
  - Columns: `user_id`, `symbol`, `quantity`, `avg_price`
- `users` or equivalent signup table
  - Required by signup and login persistence

## Current Order Workflow

1. Client POSTs `/orders` with order payload and bearer token
2. `api/orders.py` extracts `user_id` from token
3. `service/orderService.create_order()` stores the order
4. If `side == BUY`:
   - `portfolioService.process_buyer()` updates holdings
   - `portfolioPersistence.updateorderStatus()` marks the order executed
5. If `side == SELL`:
   - `portfolioService.process_seller()` updates holdings quantity
   - `portfolioPersistence.updateorderStatus()` marks the order executed

## Current Features Implemented

- User signup with hashed password storage
- User login with JWT generation
- Authenticated order creation
- Fetching user orders
- Retrieving order by ID
- Cancelling an order
- Portfolio holdings update on buy/sell

## Design Notes and Observations

- The architecture follows a clean separation: API → Service → Persistence
- Authentication is stateless using JWT
- Current order flow mixes persistence and business logic in portfolio persistence
- There is room to add stronger validation and richer domain models

## Suggested Next Improvements

1. Add Pydantic request/response models for all APIs
2. Add proper HTTP status codes and error handling
3. Move portfolio and order-status logic into dedicated service methods
4. Add transaction management for atomic buy/sell + order updates
5. Add unit tests for service and persistence logic
6. Document the expected database schema in migration files

## Summary

The system is a FastAPI-backed OMS with JWT auth, user onboarding, order management, and portfolio updates. The current layers are functional and ready for refinement toward more robust validation, transaction safety, and formal data modeling.
