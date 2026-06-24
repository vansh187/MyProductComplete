# PrimePipTrade — High-Level Technical & API Documentation

**Prepared by:** Development Team  
**Date:** June 24, 2026  
**Version:** 1.0  
**Environment:** Production — https://www.primepiptrade.com

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Project Overview](#2-project-overview)
3. [Technology Stack & Versions](#3-technology-stack--versions)
4. [System Architecture](#4-system-architecture)
5. [Database Schema (Tables)](#5-database-schema-tables)
6. [Authentication & Security](#6-authentication--security)
7. [API Reference](#7-api-reference)
   - 7.1 [Health Check](#71-health-check)
   - 7.2 [User Signup](#72-user-signup)
   - 7.3 [User Login](#73-user-login)
   - 7.4 [Orders](#74-orders)
   - 7.5 [Trade History](#75-trade-history)
   - 7.6 [Portfolio](#76-portfolio)
   - 7.7 [Dashboard Summary](#77-dashboard-summary)
   - 7.8 [Wallet & Payments (Razorpay)](#78-wallet--payments-razorpay)
8. [Order Lifecycle & Matching Engine](#8-order-lifecycle--matching-engine)
9. [Market Data Feed](#9-market-data-feed)
10. [Payment Flow](#10-payment-flow)
11. [Error Handling & Response Conventions](#11-error-handling--response-conventions)
12. [Deployment & CORS Policy](#12-deployment--cors-policy)

---

## 1. Introduction

PrimePipTrade is a full-stack stock trading platform built for Indian retail investors. It enables users to register, log in, place buy/sell stock orders, monitor their live portfolio performance, view executed trade history, and fund their wallet using Razorpay — all through a secure, JWT-authenticated REST API.

The backend is built entirely in **Python** using the **FastAPI** framework and connects to a **MySQL** relational database. Live market data is sourced from the **ICICI Breeze API** (real NSE data). Payments are processed through **Razorpay**. The system includes an internal **Order Matching Engine** that matches buy and sell orders in real time.

---

## 2. Project Overview

| Attribute          | Detail                                         |
|--------------------|------------------------------------------------|
| Application Name   | PrimePipTrade                                  |
| Application Type   | Stock Trading Platform (REST API Backend)      |
| Entry Point        | `app.py` (FastAPI application root)            |
| Base URL (Prod)    | https://www.primepiptrade.com                  |
| Frontend Dev URL   | http://localhost:5173                          |
| API Protocol       | HTTPS / REST                                   |
| Data Format        | JSON (request & response)                      |
| Authentication     | JWT Bearer Token (HS256, 30-minute expiry)     |

### Key Functional Modules

| Module              | Purpose                                                        |
|---------------------|----------------------------------------------------------------|
| `api/`              | FastAPI route handlers (controllers layer)                     |
| `service/`          | Business logic layer                                           |
| `database/`         | Database persistence layer (raw SQL via mysql-connector)       |
| `productdto/`       | Data Transfer Objects (typed response models)                  |
| `marketengine/`     | Live market data provider (Breeze/ICICI API integration)       |
| `service/matchingEngine/` | Internal order book matching engine                      |
| `service/razorpay/` | Razorpay payment integration service                           |
| `utils/`            | JWT handler, password hasher, auth dependency                  |
| `repository/`       | In-memory market tick caching (live price store)               |
| `scheduler/`        | Background market price scheduler                              |

---

## 3. Technology Stack & Versions

### Core Framework & Server

| Technology         | Version    | Purpose                                    |
|--------------------|------------|--------------------------------------------|
| Python             | 3.14       | Primary language                           |
| FastAPI            | 0.136.3    | REST API framework                         |
| Uvicorn            | 0.49.0     | ASGI server                                |
| Starlette          | 1.2.1      | ASGI toolkit (FastAPI dependency)          |
| Pydantic           | 2.13.4     | Request/response data validation           |

### Database

| Technology                  | Version   | Purpose                                    |
|-----------------------------|-----------|--------------------------------------------|
| MySQL                       | (hosted)  | Primary relational database                |
| mysql-connector-python      | 9.7.0     | MySQL driver                               |
| Redis                       | 8.0.0     | Caching layer (live market prices)         |

### Authentication & Security

| Technology         | Version   | Purpose                                    |
|--------------------|-----------|--------------------------------------------|
| python-jose        | 3.5.0     | JWT creation and verification (HS256)      |
| bcrypt             | 5.0.0     | Password hashing                           |
| cryptography       | 48.0.0    | Cryptographic primitives                   |

### Payment Gateway

| Technology   | Version  | Purpose                              |
|--------------|----------|--------------------------------------|
| razorpay     | 2.0.1    | Razorpay SDK for payment processing  |

### Market Data

| Technology       | Version   | Purpose                                        |
|------------------|-----------|------------------------------------------------|
| breeze_connect   | 1.0.69    | ICICI Breeze API client (live NSE market data) |
| aiohttp          | 3.14.1    | Async HTTP client for market polling           |
| websockets       | 16.0      | WebSocket support                              |

### Utilities

| Technology         | Version   | Purpose                               |
|--------------------|-----------|---------------------------------------|
| python-dotenv      | 1.2.2     | Environment variable management       |
| APScheduler        | 3.11.2    | Background task scheduling            |
| aiohttp            | 3.14.1    | Async HTTP sessions                   |

---

## 4. System Architecture

```
                       ┌──────────────────────────────┐
                       │         React Frontend        │
                       │   (primepiptrade.com)         │
                       └──────────────┬───────────────┘
                                      │ HTTPS / REST (JSON)
                       ┌──────────────▼───────────────┐
                       │     FastAPI Application       │
                       │         (app.py)              │
                       │   ┌────────────────────┐      │
                       │   │   API Routes Layer  │      │
                       │   │  (api/*.py)         │      │
                       │   └────────┬───────────┘      │
                       │   ┌────────▼───────────┐      │
                       │   │  Service Layer      │      │
                       │   │  (service/*.py)     │      │
                       │   └────────┬───────────┘      │
                       │   ┌────────▼───────────┐      │
                       │   │  Persistence Layer  │      │
                       │   │  (database/*.py)    │      │
                       │   └────────┬───────────┘      │
                       └────────────┼─────────────────┘
                                    │
              ┌─────────────────────┼──────────────────────┐
              │                     │                       │
    ┌─────────▼──────┐   ┌─────────▼──────┐   ┌──────────▼────────┐
    │  MySQL Database │   │  Redis Cache   │   │  External APIs    │
    │  (Orders,       │   │  (Live Prices) │   │  Razorpay         │
    │   Holdings,     │   └────────────────┘   │  ICICI Breeze API │
    │   Trades, etc.) │                        └───────────────────┘
    └─────────────────┘

          ┌──────────────────────────────────────────┐
          │  Background: Market Data Feed (Breeze)    │
          │  Subscribes to NSE symbols on startup     │
          │  Writes live ticks to in-memory cache     │
          └──────────────────────────────────────────┘
```

### On Startup (Lifespan)
When the application starts, it:
1. Connects to the ICICI Breeze API via `AlphaVantageProvider`.
2. Subscribes to a symbol watchlist: `["RELIND", "INFTEC", "TANCAM"]`.
3. Starts a background loop that polls historical intraday data and caches live ticks in the `MarketRepository` in-memory store.
4. On shutdown, closes all async HTTP sessions cleanly.

---

## 5. Database Schema (Tables)

The application uses the following MySQL tables:

| Table             | Description                                                       |
|-------------------|-------------------------------------------------------------------|
| `users`           | Registered user accounts (name, email, hashed password, phone)   |
| `orders`          | All buy/sell orders placed by users                               |
| `order_book`      | Internal order book used by the matching engine                   |
| `holdings`        | User stock holdings (symbol, quantity, average price)             |
| `trade_history`   | All executed (matched) trades                                     |
| `market_prices`   | Cached live market prices per symbol                              |
| `wallet_ledger`   | Payment transaction log (Razorpay order + payment records)        |
| `wallets`         | User wallet balance                                               |

---

## 6. Authentication & Security

All protected endpoints require a **JWT Bearer Token** in the `Authorization` header.

### Token Format
```
Authorization: Bearer <jwt_token>
```

### Token Details
| Attribute       | Value                |
|-----------------|----------------------|
| Algorithm       | HS256                |
| Expiry          | 30 minutes           |
| Payload fields  | `sub` (email), `user_id`, `exp` |

### Password Security
- Passwords are hashed using **bcrypt** before storage.
- On login, the supplied password is verified against the stored bcrypt hash.

### How to Use
1. Call `POST /login` to receive a token.
2. Pass the token in `Authorization: Bearer <token>` on every subsequent protected API call.
3. Tokens expire after 30 minutes; the user must log in again to get a new token.

---

## 7. API Reference

> **Base URL:** `https://www.primepiptrade.com`  
> **Content-Type:** `application/json`  
> **Protected routes** require: `Authorization: Bearer <token>`

---

### 7.1 Health Check

#### `GET /`

A simple health check to confirm the server is running.

**Authentication:** Not required

**Request:** No body

**Response (200 OK):**
```json
{
  "Message": "Finnaly I am able to run my first API"
}
```

---

### 7.2 User Signup

#### `POST /signup`

Registers a new user. The password is hashed using bcrypt before being stored.

**Authentication:** Not required

**Request Body:**
```json
{
  "first_name": "Rahul",
  "last_name": "Sharma",
  "email": "rahul.sharma@example.com",
  "password": "SecureP@ssword123",
  "phone_number": "9876543210"
}
```

| Field          | Type   | Required | Description                   |
|----------------|--------|----------|-------------------------------|
| `first_name`   | string | Yes      | User's first name             |
| `last_name`    | string | Yes      | User's last name              |
| `email`        | string | Yes      | User's email address (unique) |
| `password`     | string | Yes      | Plain-text password (hashed before storage) |
| `phone_number` | string | Yes      | User's phone number           |

**Success Response (200 OK):**
```json
{
  "Message": "User Rahul Sharma with email rahul.sharma@example.com has been successfully registered."
}
```

**Error Response (200 OK with error):**
```json
{
  "Message": "An error occurred during signup: Duplicate entry 'rahul.sharma@example.com' for key 'email'"
}
```

---

### 7.3 User Login

#### `POST /login`

Authenticates a user and returns a JWT Bearer Token.

**Authentication:** Not required

**Request Body:**
```json
{
  "email": "rahul.sharma@example.com",
  "password": "SecureP@ssword123"
}
```

| Field      | Type   | Required | Description           |
|------------|--------|----------|-----------------------|
| `email`    | string | Yes      | Registered email      |
| `password` | string | Yes      | User's plain password |

**Success Response (200 OK):**
```json
{
  "Message": "User with email rahul.sharma@example.com has been successfully logged in.",
  "Token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJyYWh1bC5zaGFybWFAZXhhbXBsZS5jb20iLCJ1c2VyX2lkIjoxLCJleHAiOjE3NTA3Njk2MDB9.SIGNATURE",
  "TokenType": "Bearer"
}
```

**Invalid Credentials Response (200 OK):**
```json
{
  "Message": "Invalid email or password"
}
```

> **Note:** Use the `Token` value as `Bearer <Token>` in the `Authorization` header for all subsequent requests.

---

### 7.4 Orders

All order endpoints require authentication.

---

#### `GET /orders`

Retrieves all orders placed by the authenticated user.

**Authentication:** Required (Bearer Token)

**Request:** No body

**Success Response (200 OK):**
```json
{
  "Message": "Orders retrieved successfully",
  "User": {
    "sub": "rahul.sharma@example.com",
    "user_id": 1,
    "exp": 1750769600
  },
  "Orders": [
    [1, "RELIND", "BUY", 10, "2350.00", "EXECUTED"],
    [2, "INFTEC", "SELL", 5, "1800.00", "PENDING"],
    [3, "TANCAM", "BUY", 20, "900.00", "PARTIALLY_EXECUTED"]
  ]
}
```

> Each order tuple: `[id, symbol, side, quantity, price, status]`

---

#### `POST /orders`

Places a new buy or sell order. Internally this:
1. Persists the order to the `orders` table.
2. Adds it to the `order_book`.
3. Runs the matching engine to find a counter order.
4. If matched: records to `trade_history`, updates `holdings`, updates order status.

**Authentication:** Required (Bearer Token)

**Request Body:**
```json
{
  "symbol": "RELIND",
  "side": "BUY",
  "quantity": 10,
  "price": 2350.00,
  "status": "PENDING"
}
```

| Field      | Type    | Required | Description                                          |
|------------|---------|----------|------------------------------------------------------|
| `symbol`   | string  | Yes      | NSE stock symbol (e.g., `RELIND`, `INFTEC`)          |
| `side`     | string  | Yes      | `BUY` or `SELL`                                      |
| `quantity` | integer | Yes      | Number of shares                                     |
| `price`    | decimal | Yes      | Limit price per share (in INR)                       |
| `status`   | string  | Yes      | Must be `PENDING` when submitting a new order        |

**Response — Order Fully Executed (200 OK):**
```json
{
  "success": true,
  "status": "ORDER STATUS EXECUTED",
  "tradeOrderId": 42
}
```

**Response — Order Partially Executed (200 OK):**
```json
{
  "success": true,
  "status": "ORDER Partially EXECUTED",
  "tradeOrderId": 43
}
```

**Response — No Match Found, Order Queued (200 OK):**
```json
{
  "userId": 1,
  "message": "Orders execution is in process. Please wait"
}
```

**Response — Execution Failed (200 OK):**
```json
{
  "success": false,
  "status": "ORDER STATUS FAILED: <error detail>"
}
```

**Order Status Values:**

| Status               | Meaning                                                   |
|----------------------|-----------------------------------------------------------|
| `PENDING`            | Order placed, no matching counter order found yet         |
| `EXECUTED`           | Order fully matched and executed                          |
| `PARTIALLY_EXECUTED` | Order partially matched; remaining quantity still pending |
| `CANCELLED`          | Order cancelled by user                                   |
| `FAILED`             | Order failed due to a system error                        |

---

#### `GET /getOrderById/{orderId}`

Retrieves a specific order by its ID for the authenticated user.

**Authentication:** Required (Bearer Token)

**Path Parameter:**

| Parameter | Type    | Description         |
|-----------|---------|---------------------|
| `orderId` | integer | The order's numeric ID |

**Example Request:**
```
GET /getOrderById/1
Authorization: Bearer <token>
```

**Success Response (200 OK):**
```json
{
  "Message": "Order retrieved successfully",
  "User": {
    "sub": "rahul.sharma@example.com",
    "user_id": 1,
    "exp": 1750769600
  },
  "Order": [1, "RELIND", "BUY", 10, "2350.00", "EXECUTED"]
}
```

> Order tuple: `[id, symbol, side, quantity, price, status]`

---

#### `GET /cancelOrderById/{orderId}`

Cancels a specific order by its ID. Only orders with status `PENDING` can be cancelled.

**Authentication:** Required (Bearer Token)

**Path Parameter:**

| Parameter | Type    | Description         |
|-----------|---------|---------------------|
| `orderId` | integer | The order's numeric ID |

**Example Request:**
```
GET /cancelOrderById/2
Authorization: Bearer <token>
```

**Success Response (200 OK):**
```json
{
  "Message": "Order cancelled Success",
  "User": {
    "sub": "rahul.sharma@example.com",
    "user_id": 1,
    "exp": 1750769600
  },
  "Order": "Cancel Success"
}
```

**Response — Order Not Cancellable (non-PENDING):**
```json
{
  "Message": "Only pending orders are allowed",
  "User": {
    "sub": "rahul.sharma@example.com",
    "user_id": 1,
    "exp": 1750769600
  }
}
```

---

### 7.5 Trade History

#### `GET /trades`

Retrieves the complete trade execution history for the authenticated user, ordered by most recent first.

**Authentication:** Required (Bearer Token)

**Request:** No body

**Success Response (200 OK):**
```json
{
  "User": {
    "sub": "rahul.sharma@example.com",
    "user_id": 1,
    "exp": 1750769600
  },
  "tradeOrder": [
    [101, 1, 1, "RELIND", "BUY", 10, "2350.00", "2026-06-24T10:30:00"],
    [102, 3, 1, "TANCAM", "BUY", 5, "900.00", "2026-06-23T14:15:00"]
  ]
}
```

> Each trade tuple: `[trade_id, order_id, user_id, symbol, side, quantity, execution_price, executed_at]`

**Response — No Trades Found (200 OK):**
```json
{
  "User": {
    "sub": "rahul.sharma@example.com",
    "user_id": 1,
    "exp": 1750769600
  },
  "Message": "No Trade Orders Found"
}
```

---

### 7.6 Portfolio

#### `GET /getPortfolioForLoggedInUser`

Returns the basic portfolio (holdings) for the authenticated user — symbol, quantity, average purchase price, and last updated timestamp.

**Authentication:** Required (Bearer Token)

**Request:** No body

**Success Response (200 OK):**
```json
{
  "generated_at": "2026-06-24T11:00:00.000Z",
  "success": true,
  "userId": 1,
  "total_positions": 2,
  "portfolio": [
    {
      "symbol": "RELIND",
      "quantity": 10,
      "avg_price": "2350.00",
      "updated_at": "2026-06-24T10:30:00"
    },
    {
      "symbol": "TANCAM",
      "quantity": 15,
      "avg_price": "900.00",
      "updated_at": "2026-06-23T14:15:00"
    }
  ]
}
```

**Response — No Portfolio Found (200 OK):**
```json
{
  "userId": 1,
  "message": "No portfolio found for User"
}
```

---

#### `GET /getPortfolioOfLoggedInUserWithProfitLoss`

Returns the portfolio enriched with current market prices and Profit & Loss (P&L) calculations per holding. Current price is sourced from the `market_prices` table (live cache); falls back to `avg_price` if no live price is available.

**Authentication:** Required (Bearer Token)

**Request:** No body

**Success Response (200 OK):**
```json
{
  "success": true,
  "user_id": 1,
  "total_pnl": 3250.00,
  "portfolio": [
    {
      "symbol": "RELIND",
      "quantity": 10,
      "avg_price": 2350.00,
      "current_price": 2675.00,
      "pnl": 3250.00
    },
    {
      "symbol": "TANCAM",
      "quantity": 15,
      "avg_price": 900.00,
      "current_price": 900.00,
      "pnl": 0.00
    }
  ]
}
```

| Field           | Description                                          |
|-----------------|------------------------------------------------------|
| `total_pnl`     | Sum of unrealised P&L across all holdings (INR)      |
| `avg_price`     | Average purchase price per share                     |
| `current_price` | Latest market price per share                        |
| `pnl`           | `(current_price - avg_price) × quantity`             |

**Error Response (200 OK):**
```json
{
  "success": false,
  "message": "No portfolio data found for user"
}
```

---

### 7.7 Dashboard Summary

#### `GET /getDashboardSummary`

Returns a consolidated summary of the user's trading activity — orders count, trade counts, and portfolio valuation — all in a single call. Ideal for populating a home dashboard screen.

**Authentication:** Required (Bearer Token)

**Request:** No body

**Success Response (200 OK):**
```json
{
  "userId": 1,
  "dashboard": {
    "orders": {
      "total_orders": 15,
      "pending_orders": 2,
      "executed_orders": 10,
      "partially_executed_orders": 1,
      "cancelled_orders": 1,
      "failed_orders": 1
    },
    "trades": {
      "total_trades": 10,
      "buy_trades": 6,
      "sell_trades": 4
    },
    "portfolio": {
      "total_invested": 45000.00,
      "total_holdings": 48250.00,
      "unrealized_pnl": 3250.00,
      "return_percentage": 7.22,
      "total_positions": 2,
      "last_updated": "2026-06-24T10:30:00"
    },
    "last_updated": "2026-06-24T10:30:00"
  }
}
```

| Field               | Description                                            |
|---------------------|--------------------------------------------------------|
| `total_invested`    | Total capital deployed across all holdings (INR)       |
| `total_holdings`    | Current market value of all holdings (INR)             |
| `unrealized_pnl`    | Unrealised profit or loss (INR)                        |
| `return_percentage` | Portfolio return as a percentage                       |
| `total_positions`   | Number of active stock positions                       |

---

### 7.8 Wallet & Payments (Razorpay)

PrimePipTrade uses **Razorpay** to allow users to add funds to their in-platform wallet. The flow is split into three steps: order creation, payment verification, and webhook confirmation.

---

#### `POST /v1/addFundsToWallet`

**Step 1 of payment flow.** Creates a Razorpay order and records a `PENDING` entry in `wallet_ledger`. Returns the Razorpay order details needed by the frontend checkout widget.

**Authentication:** Required (Bearer Token)

**Request Body:**
```json
{
  "amount": 50000,
  "currency": "INR"
}
```

| Field      | Type   | Required | Description                                         |
|------------|--------|----------|-----------------------------------------------------|
| `amount`   | float  | Yes      | Amount in **paise** (e.g., `50000` = ₹500.00)      |
| `currency` | string | Yes      | ISO currency code — use `"INR"`                     |

**Success Response (200 OK):**
```json
{
  "status": "success",
  "userId": 1,
  "razorpay_order": {
    "id": "order_T3DvWN54raoa0B",
    "entity": "order",
    "amount": 50000,
    "amount_paid": 0,
    "amount_due": 50000,
    "currency": "INR",
    "receipt": "rcpt_1_50000",
    "status": "created",
    "attempts": 0,
    "notes": {
      "user_id": 1,
      "module": "wallet_funding"
    },
    "created_at": 1750769600
  },
  "amount_subunits": 50000,
  "currency": "INR",
  "key": "rzp_live_XXXXXXXXXX"
}
```

> The frontend should use `razorpay_order.id` and `key` to open the Razorpay checkout modal.

**Error Response (200 OK):**
```json
{
  "Message": "Invalid Request"
}
```

---

#### `POST /v1/VerifyFundPayements`

**Step 2 of payment flow.** Called by the frontend after the user completes payment in the Razorpay checkout. Verifies the cryptographic signature (HMAC-SHA256) to confirm the payment is authentic.

**Authentication:** Required (Bearer Token)

**Request Body:**
```json
{
  "razorpay_order_id": "order_T3DvWN54raoa0B",
  "razorpay_payment_id": "pay_MOCK_SUCCESS_12345",
  "razorpay_signature": "computed_hmac_sha256_signature"
}
```

| Field                  | Type   | Required | Description                                         |
|------------------------|--------|----------|-----------------------------------------------------|
| `razorpay_order_id`    | string | Yes      | Order ID returned in Step 1                         |
| `razorpay_payment_id`  | string | Yes      | Payment ID returned by Razorpay checkout            |
| `razorpay_signature`   | string | Yes      | HMAC-SHA256 of `order_id|payment_id` using secret   |

**Success Response (200 OK):**
```json
{
  "status": 200,
  "message": "Payment Verified"
}
```

**Failure Response (200 OK):**
```json
{
  "message": "Not Verified"
}
```

---

#### `POST /v1/razorpay-webhook`

**Razorpay server-to-server webhook.** Called directly by Razorpay servers when a `payment.captured` event occurs. Validates the webhook signature header, then updates the `wallet_ledger` status to SUCCESS and credits the user's wallet.

**Authentication:** None (secured via webhook signature header)

**Headers:**

| Header                  | Description                                        |
|-------------------------|----------------------------------------------------|
| `X-Razorpay-Signature`  | HMAC-SHA256 of raw request body using webhook secret |

**Request Body (sent by Razorpay — not by client):**
```json
{
  "event": "payment.captured",
  "payload": {
    "payment": {
      "entity": {
        "id": "pay_MOCK_SUCCESS_12345",
        "order_id": "order_T3DvWN54raoa0B",
        "amount": 50000,
        "currency": "INR",
        "status": "captured"
      }
    }
  }
}
```

**Success Response (200 OK):**
```json
{
  "status": "accepted",
  "message": "Webhook authenticated and queued"
}
```

**Error Response (400 Bad Request):**
```json
{
  "detail": "Invalid webhook signature"
}
```

---

## 8. Order Lifecycle & Matching Engine

### Order Flow Diagram

```
User places order (POST /orders)
         │
         ▼
  Saved to `orders` table (status=PENDING)
         │
         ▼
  Inserted into `order_book`
         │
         ▼
  Matching Engine runs
         │
    ┌────┴────┐
    │         │
No match   Match found
    │         │
    ▼         ▼
 Return   For each matched trade:
 "queued"   ├── Insert into `trade_history`
            ├── Update order status (EXECUTED / PARTIALLY_EXECUTED)
            ├── Update `order_book` remaining quantity
            └── Update `holdings` (BUY: add/avg, SELL: reduce qty)
```

### Matching Engine Logic

The matching engine (`service/matchingEngine/`) implements **limit order book matching**:

- A **BUY** order matches against the cheapest available **SELL** order where `sell_price <= buy_price`.
- A **SELL** order matches against the highest available **BUY** order where `buy_price >= sell_price`.
- Execution price is always the **resting order's price** (the order already in the book).
- If the incoming order quantity is larger than the matched order, **partial execution** occurs and the remaining quantity stays open.
- All matching, holdings update, and trade history insertion happen within a **single database transaction** (commit on success, rollback on failure).

### Holdings Update Logic

| Side | Condition               | Action                                                   |
|------|-------------------------|----------------------------------------------------------|
| BUY  | No existing holding     | Insert new row: `(user_id, symbol, quantity, price)`     |
| BUY  | Existing holding        | Update: `new_qty = old + qty`, `new_avg = weighted avg`  |
| SELL | Holding exists          | Update: `new_qty = old - qty`                            |
| SELL | Qty > available holding | Return error: "quantity you have is less than trade quantity" |

---

## 9. Market Data Feed

### Provider: ICICI Breeze API

On startup, the application authenticates with ICICI Breeze using an API key and session token, then polls for NSE intraday historical data for a predefined watchlist every **12 seconds**.

```
Startup
  └── AlphaVantageProvider.connect()       ← opens aiohttp session
  └── AlphaVantageProvider.subscribe([])   ← registers symbol watchlist
  └── Background loop starts
        └── For each symbol:
              ├── Calls Breeze get_historical_data (1-minute interval)
              ├── Normalises: { stock_code, exchange_code, timestamp, open, close, volume }
              └── Writes to MarketRepository (in-memory live tick cache)
```

**Normalised Tick Structure (internal):**
```json
{
  "stock_code": "RELIND",
  "exchange_code": "NSE",
  "timestamp": "2026-06-17T09:15:00.000Z",
  "open": 2340.50,
  "close": 2350.75,
  "volume": 4520
}
```

Live prices are used for portfolio P&L calculation and dashboard holdings valuation.

---

## 10. Payment Flow

The complete end-to-end wallet funding flow:

```
Step 1: Frontend calls POST /v1/addFundsToWallet
         └── Razorpay SDK creates an order
         └── wallet_ledger row inserted (status=PENDING)
         └── Returns: razorpay_order_id, key, amount

Step 2: User completes payment in Razorpay Checkout (frontend)
         └── Razorpay returns: razorpay_order_id, razorpay_payment_id, razorpay_signature

Step 3: Frontend calls POST /v1/VerifyFundPayements
         └── Backend verifies HMAC-SHA256 signature
         └── If valid → wallet_ledger status updated to SUCCESS
         └── Wallet balance credited

Step 4 (parallel): Razorpay calls POST /v1/razorpay-webhook (server-to-server)
         └── Webhook signature verified
         └── payment.captured event processed
         └── Wallet balance updated independently
```

### Wallet Ledger Transaction Types

| Code | Type             | Description                      |
|------|------------------|----------------------------------|
| `1`  | WALLET_FUNDING   | User adding money via Razorpay   |
| `2`  | WITHDRAWAL       | User withdrawing funds           |
| `3`  | ASSET_PURCHASE   | Debit for buying shares          |
| `4`  | ASSET_SALE       | Credit from selling shares       |

### Wallet Transaction Statuses

| Code | Status  | Description                    |
|------|---------|--------------------------------|
| `1`  | PENDING | Payment initiated, not settled |
| `2`  | SUCCESS | Payment confirmed & settled    |
| `3`  | FAILED  | Payment failed                 |

---

## 11. Error Handling & Response Conventions

All responses are HTTP 200 unless otherwise specified. Application-level errors are embedded in the JSON body.

### Common Response Patterns

| Scenario                | Response Shape                                     |
|-------------------------|----------------------------------------------------|
| Success                 | `{ "Message": "...", <data fields> }`              |
| Auth failure            | HTTP 401 `{ "detail": "Invalid or expired token" }` |
| Webhook failure         | HTTP 400 `{ "detail": "Invalid webhook signature" }` |
| Partial data            | `{ "success": false, "message": "..." }`           |
| Order not executable    | `{ "success": false, "message": "Order already ..." }` |

### HTTP Status Codes Used

| Code | Meaning                                         |
|------|-------------------------------------------------|
| 200  | All standard API responses (success and errors) |
| 400  | Bad request (webhook signature mismatch)        |
| 401  | Unauthorized — missing or invalid JWT           |

---

## 12. Deployment & CORS Policy

### CORS Allowed Origins

The API accepts cross-origin requests from the following domains:

| Origin                                    | Purpose                     |
|-------------------------------------------|-----------------------------|
| `http://localhost:5173`                   | Local React development     |
| `https://myproductreact.onrender.com`     | Staging frontend            |
| `https://primepiptrade.com`               | Production frontend         |
| `https://www.primepiptrade.com`           | Production frontend (www)   |

- **Allowed Methods:** All (`GET`, `POST`, `PUT`, `DELETE`, `OPTIONS`, etc.)
- **Allowed Headers:** All
- **Credentials:** Allowed (supports cookie-based auth if needed)

### Environment Variables Required

| Variable               | Description                              |
|------------------------|------------------------------------------|
| `MYSQLHOST`            | MySQL database host                      |
| `MYSQLUSER`            | MySQL username                           |
| `MYSQLPASSWORD`        | MySQL password                           |
| `MYSQLDATABASE`        | MySQL database name                      |
| `MYSQLPORT`            | MySQL port (default: 3306)               |
| `RAZORPAY_API_KEY`     | Razorpay API key (public)                |
| `RAZORPAY_SECRET_KEY`  | Razorpay secret key                      |
| `BREEZE_API_KEY`       | ICICI Breeze API key                     |
| `BREEZE_SECRET_KEY`    | ICICI Breeze secret key                  |
| `BREEZE_SESSION_TOKEN` | ICICI Breeze session token               |
| `MARKET_PROVIDER`      | Market provider selector (ALPHAVANTAGE)  |

---

*End of Document*

*PrimePipTrade — Empowering Retail Investors with Professional-Grade Trading Tools*
