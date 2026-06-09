# System Design for MyProductComplete

## Overview

`MyProductComplete` is an API-first Order Management System (OMS) implemented with FastAPI and MySQL. The product is built as a layered application with separate modules for API routing, business logic, persistence, and utilities.

The application supports:
- User signup and login
- JWT-based authentication
- Order creation, retrieval, and cancellation
- Portfolio updates for buy/sell orders
- Trade history tracking

## Architecture Layers

### 1. API Layer

The API layer exposes REST endpoints and delegates request handling to services.

- `app.py`
  - Instantiates the FastAPI application
  - Mounts routers for signup, login, orders, and trade history
  - Provides a health check or root endpoint

- `api/signup.py`
  - Endpoint: `POST /signup`
  - Registers new users

- `api/login.py`
  - Endpoint: `POST /login`
  - Authenticates users and issues JWT tokens

- `api/orders.py`
  - Endpoints for order lifecycle management:
    - `GET /orders`
    - `POST /orders`
    - `GET /getOrderById/{orderId}`
    - `GET /cancelOrderById/{orderId}`

- `api/trade.py`
  - Endpoints for trade history operations (if implemented)

### 2. Authentication and Security

Authentication is handled via JWT and request dependency injection:

- `utils/jwt_handler.py`
  - Creates and validates JWT tokens
  - Uses application secrets from environment variables

- `utils/auth_dependency.py`
  - Implements `HTTPBearer` security dependency
  - Verifies incoming tokens and exposes authenticated user data to endpoints

- `utils/password_hasher.py`
  - Hashes user passwords at signup
  - Verifies passwords at login

This design keeps endpoints stateless and protects sensitive actions with token validation.

### 3. Service Layer

The service layer contains business rules and orchestration logic.

- `service/signupService.py`
  - Validates user data
  - Hashes passwords
  - Calls persistence layer to create new users

- `service/loginService.py`
  - Validates credentials
  - Verifies passwords
  - Issues JWT tokens on successful login

- `service/orderService.py`
  - Creates and retrieves orders
  - Delegates persistence operations and executes order flows

- `service/portfolioService.py`
  - Adjusts holdings after buy/sell orders
  - Ensures portfolio state matches executed trades

- `service/tradeHistoryService.py`
  - Records trade execution details
  - Retrieves trade history for users

- `service/ExecutionEngine.py`
  - Coordinates order execution, trade insertion, portfolio updates, and order status transitions
  - Manages connection lifecycle and rollback/commit behavior

### 4. Persistence Layer

The persistence layer abstracts direct database operations.

- `database/ConnectionFactory.py`
  - Creates MySQL database connections using environment variables

- `database/loginPersist.py`
  - Queries user records for authentication

- `database/signUpPersist.py`
  - Inserts new user records

- `database/orderPersistence.py`
  - Inserts, selects, updates, and cancels orders

- `database/portfolioPersistence.py`
  - Updates holdings, balances, and order statuses

- `database/tradeHistoryPersistence.py`
  - Persists executed trade records

This separation keeps SQL handling away from higher-level business rules.

## Architecture Diagram

A simplified component diagram for `MyProductComplete`:

```text
Client
  |
  | HTTP
  v
FastAPI API Layer (`app.py`, routers)
  |
  | calls
  v
Service Layer
  - signupService
  - loginService
  - orderService
  - portfolioService
  - tradeHistoryService
  - ExecutionEngine
  |
  | uses
  v
Persistence Layer
  - ConnectionFactory
  - loginPersist
  - signUpPersist
  - orderPersistence
  - portfolioPersistence
  - tradeHistoryPersistence
  |
  | stores/retrieves
  v
MySQL Database
```

The flow is:
- Client -> API layer
- API layer -> Service layer
- Service layer -> Persistence layer
- Persistence layer -> MySQL

## Data Model and Schema

The project expects a MySQL database backed by environment configuration in `.env`.

Suggested tables and fields:

- `users`
  - `id`
  - `username`
  - `email`
  - `password_hash`
  - `created_at`

- `orders`
  - `id`
  - `user_id`
  - `symbol`
  - `side`
  - `quantity`
  - `price`
  - `status`
  - `created_at`

- `holdings`
  - `user_id`
  - `symbol`
  - `quantity`
  - `avg_price`

- `trade_history`
  - `id`
  - `order_id`
  - `user_id`
  - `symbol`
  - `side`
  - `quantity`
  - `price`
  - `executed_at`

## Request Flow

1. User signs up with `POST /signup`
2. User logs in with `POST /login` and receives a JWT
3. Authenticated user posts an order to `POST /orders`
4. The order is validated and stored
5. Order execution begins:
   - `ExecutionEngine` opens a database connection
   - `portfolioService` updates holdings based on `BUY` or `SELL`
   - `tradeHistoryService` records the trade
   - Order status is updated to `EXECUTED`
   - Transaction commits on success or rolls back on failure
6. User can retrieve orders and cancel pending orders

## Project Status and Implementation Notes

The system design is implemented through the existing modules. The current project covers:

- Complete authentication flow
- Order management endpoints
- Portfolio update logic for buy/sell orders
- Trade history recording
- Database connection management
- JWT-based protected routes

## Improvements and Next Steps

Future refinements should include:

- Stronger request and response validation with Pydantic models
- Explicit HTTP status codes for success and failure cases
- More robust error handling and logging
- Clearer domain boundaries between services and persistence
- A migration strategy or schema definition file for MySQL
- End-to-end tests for order execution and authentication

## Summary

`MyProductComplete` is a functioning OMS product built in Python with FastAPI and MySQL. It is structured in clean service/persistence layers and currently supports user onboarding, authenticated trading, portfolio updates, and trade history. The project is ready for formal schema documentation, validation hardening, and production-grade transaction handling.
