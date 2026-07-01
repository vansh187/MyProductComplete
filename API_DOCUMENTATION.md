# Order API Documentation - Frontend Integration Guide

**Last Updated**: 2026-07-02  
**Status**: ✅ Production Ready  
**Branch**: OrderBookBranch  
**Base URL**: `http://localhost:8000` (local) | Update for production URL

---

## 📋 Table of Contents
1. [API Endpoints](#api-endpoints)
2. [Authentication](#authentication)
3. [Create Order](#create-order)
4. [Get All Orders](#get-all-orders)
5. [Get Order by ID](#get-order-by-id)
6. [Cancel Order](#cancel-order)
7. [Error Handling](#error-handling)
8. [Order States](#order-states)
9. [Examples](#examples)

---

## 🔐 Authentication

All endpoints require Bearer token authentication via `Authorization` header.

```
Authorization: Bearer <your_jwt_token>
```

---

## 🎯 API Endpoints

### 1. CREATE ORDER

**Endpoint**: `POST /orders`

**Description**: Create a new order with wallet balance validation for BUY orders

**Headers**:
```
Authorization: Bearer <token>
Content-Type: application/json
```

**Request Payload**:
```json
{
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "side": "BUY",
  "quantity": 10,
  "order_type": "LIMIT",
  "price": 2450.50,
  "trigger_price": null,
  "product_type": "MIS",
  "validity": "DAY",
  "client_order_id": "CLIENT_123456",
  "notes": "Optional order notes"
}
```

**Request Field Details**:

| Field | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| symbol | string | ✅ Yes | Stock symbol (1-20 chars) | "RELIANCE", "TCS", "INFY" |
| exchange | string | ✅ Yes | Stock exchange | "NSE", "BSE", "NFO", "NCDEX", "MCXSX" |
| side | string | ✅ Yes | Order direction | "BUY" or "SELL" |
| quantity | integer | ✅ Yes | Order quantity (> 0) | 10, 100, 5 |
| order_type | string | Optional | Order type | "MARKET" (default), "LIMIT", "STOP", "STOPLIMIT" |
| price | number | Conditional | Price (required for LIMIT/STOPLIMIT) | 2450.50, 1000.0 |
| trigger_price | number | Conditional | Trigger price (required for STOP/STOPLIMIT) | 2400.0 |
| product_type | string | Optional | Trading product | "MIS" (default), "CNC", "NRML" |
| validity | string | Optional | Order validity | "DAY" (default), "IOC", "TTL", "GTC" |
| client_order_id | string | Optional | Your order reference (max 100 chars) | "CLIENT_123456" |
| notes | string | Optional | Order notes (max 500 chars) | "Buy on dip" |

**Response - Success (200)**:
```json
{
  "success": true,
  "order_id": 101,
  "execution": {
    "success": true,
    "status": "PENDING",
    "message": "Order queued for matching",
    "order_id": 101
  }
}
```

**Response - Success with Partial Execution (200)**:
```json
{
  "success": true,
  "order_id": 102,
  "execution": {
    "success": true,
    "status": "PARTIALLY_EXECUTED",
    "message": "Order partially executed",
    "order_id": 102,
    "trade_id": 5001,
    "matched_quantity": 5,
    "remaining_quantity": 5
  }
}
```

**Response - Success with Full Execution (200)**:
```json
{
  "success": true,
  "order_id": 103,
  "execution": {
    "success": true,
    "status": "EXECUTED",
    "message": "Order fully executed",
    "order_id": 103,
    "trade_id": 6001
  }
}
```

**Response - Insufficient Balance (400)**:
```json
{
  "detail": "Insufficient balance. Required: ₹24505.00, Available: ₹10000.00"
}
```

**Response - Invalid Order (400)**:
```json
{
  "detail": "BUY orders require a valid price"
}
```

**Response - Server Error (500)**:
```json
{
  "detail": "Failed to create order"
}
```

**Status Codes**:
- `200` - Order created successfully
- `400` - Validation error (insufficient balance, invalid input)
- `401` - Unauthorized (missing or invalid token)
- `500` - Server error

---

### 2. GET ALL ORDERS

**Endpoint**: `GET /orders`

**Description**: Retrieve all orders for the authenticated user

**Headers**:
```
Authorization: Bearer <token>
```

**Request Body**: None

**Response - Success (200)**:
```json
{
  "success": true,
  "message": "Orders retrieved successfully",
  "user_id": 1,
  "orders": [
    {
      "id": 101,
      "user_id": 1,
      "symbol": "RELIANCE",
      "exchange": "NSE",
      "side": "BUY",
      "quantity": 10,
      "price": 2450.50,
      "order_type": "LIMIT",
      "product_type": "MIS",
      "validity": "DAY",
      "status": "PENDING",
      "created_at": "2026-07-02T10:30:00",
      "updated_at": "2026-07-02T10:30:00"
    },
    {
      "id": 102,
      "user_id": 1,
      "symbol": "TCS",
      "exchange": "NSE",
      "side": "SELL",
      "quantity": 5,
      "price": 3500.00,
      "order_type": "LIMIT",
      "product_type": "CNC",
      "validity": "GTC",
      "status": "EXECUTED",
      "created_at": "2026-07-02T09:15:00",
      "updated_at": "2026-07-02T09:45:00"
    }
  ]
}
```

**Response - Unauthorized (401)**:
```json
{
  "detail": "Unauthorized"
}
```

**Response - Error (500)**:
```json
{
  "detail": "Failed to retrieve orders"
}
```

**Status Codes**:
- `200` - Orders retrieved successfully
- `401` - Unauthorized
- `500` - Server error

---

### 3. GET ORDER BY ID

**Endpoint**: `GET /orders/{order_id}`

**Description**: Retrieve a specific order by ID

**Parameters**:
- `order_id` (path parameter) - Order ID (integer)

**Headers**:
```
Authorization: Bearer <token>
```

**Example Request**:
```
GET /orders/101
Authorization: Bearer <token>
```

**Response - Success (200)**:
```json
{
  "success": true,
  "message": "Order retrieved successfully",
  "order": {
    "id": 101,
    "user_id": 1,
    "symbol": "RELIANCE",
    "exchange": "NSE",
    "side": "BUY",
    "quantity": 10,
    "price": 2450.50,
    "order_type": "LIMIT",
    "product_type": "MIS",
    "validity": "DAY",
    "trigger_price": null,
    "status": "PENDING",
    "created_at": "2026-07-02T10:30:00",
    "updated_at": "2026-07-02T10:30:00"
  }
}
```

**Response - Not Found (404)**:
```json
{
  "detail": "Order not found"
}
```

**Response - Unauthorized (401)**:
```json
{
  "detail": "Unauthorized"
}
```

**Status Codes**:
- `200` - Order found
- `400` - Invalid order ID
- `401` - Unauthorized
- `404` - Order not found
- `500` - Server error

---

### 4. CANCEL ORDER

**Endpoint**: `POST /orders/{order_id}/cancel`

**Description**: Cancel a pending order (only PENDING orders can be cancelled)

**Parameters**:
- `order_id` (path parameter) - Order ID (integer)

**Headers**:
```
Authorization: Bearer <token>
Content-Type: application/json
```

**Request Body**: Empty (`{}`)

**Example Request**:
```
POST /orders/101/cancel
Authorization: Bearer <token>
Content-Type: application/json

{}
```

**Response - Success (200)**:
```json
{
  "success": true,
  "message": "Order cancelled successfully",
  "order_id": 101
}
```

**Response - Not Cancellable (400)**:
```json
{
  "detail": "Only pending orders can be cancelled"
}
```

**Response - Not Found (404)**:
```json
{
  "detail": "Order not found"
}
```

**Response - Unauthorized (401)**:
```json
{
  "detail": "Unauthorized"
}
```

**Status Codes**:
- `200` - Order cancelled successfully
- `400` - Order not pending or invalid ID
- `401` - Unauthorized
- `404` - Order not found
- `500` - Server error

---

## 📊 Order States

Orders have three possible states during execution:

### 1. **PENDING**
- **Meaning**: Order created but waiting for matches
- **Transitions to**: PARTIALLY_EXECUTED or EXECUTED when matches found
- **Cancellable**: ✅ Yes
- **Example**: BUY 10 units at ₹2450, no sellers available yet

### 2. **PARTIALLY_EXECUTED**
- **Meaning**: Order partially matched, some quantity remaining
- **Transitions to**: EXECUTED when remaining quantity matches
- **Cancellable**: ❌ No (already partially matched)
- **Example**: BUY 10 units → 5 matched, 5 remaining

### 3. **EXECUTED**
- **Meaning**: Order fully matched and executed
- **Transitions to**: None (final state)
- **Cancellable**: ❌ No (already executed)
- **Example**: BUY 10 units → all 10 matched

---

## ❌ Error Handling

### Common Error Responses

**Invalid Request Body (400)**:
```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "price"],
      "msg": "ensure this value is greater than 0",
      "input": 0
    }
  ]
}
```

**Insufficient Wallet Balance (400)**:
```json
{
  "detail": "Insufficient balance. Required: ₹24505.00, Available: ₹10000.00"
}
```

**Wallet Not Initialized (400)**:
```json
{
  "detail": "User wallet not initialized"
}
```

**Unauthorized (401)**:
```json
{
  "detail": "Unauthorized"
}
```

**Order Not Found (404)**:
```json
{
  "detail": "Order not found"
}
```

**Server Error (500)**:
```json
{
  "detail": "Failed to create order"
}
```

### Error Codes & Meanings

| Code | Meaning | Action |
|------|---------|--------|
| 400 | Bad Request | Check request format and values |
| 401 | Unauthorized | Verify authentication token |
| 404 | Not Found | Order doesn't exist or wrong ID |
| 500 | Server Error | Contact support/check logs |

---

## 💡 Examples

### Example 1: Create a BUY Order (LIMIT)

**Request**:
```bash
curl -X POST http://localhost:8000/orders \
  -H "Authorization: Bearer your_jwt_token" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "RELIANCE",
    "exchange": "NSE",
    "side": "BUY",
    "quantity": 10,
    "order_type": "LIMIT",
    "price": 2450.50,
    "product_type": "MIS",
    "validity": "DAY"
  }'
```

**Response**:
```json
{
  "success": true,
  "order_id": 101,
  "execution": {
    "success": true,
    "status": "PENDING",
    "message": "Order queued for matching",
    "order_id": 101
  }
}
```

---

### Example 2: Create a SELL Order (MARKET)

**Request**:
```bash
curl -X POST http://localhost:8000/orders \
  -H "Authorization: Bearer your_jwt_token" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "TCS",
    "exchange": "NSE",
    "side": "SELL",
    "quantity": 5,
    "order_type": "MARKET",
    "product_type": "CNC",
    "validity": "DAY"
  }'
```

**Response** (Immediate execution with market price):
```json
{
  "success": true,
  "order_id": 102,
  "execution": {
    "success": true,
    "status": "EXECUTED",
    "message": "Order fully executed",
    "order_id": 102,
    "trade_id": 6001
  }
}
```

---

### Example 3: Get All Orders

**Request**:
```bash
curl -X GET http://localhost:8000/orders \
  -H "Authorization: Bearer your_jwt_token"
```

**Response**:
```json
{
  "success": true,
  "message": "Orders retrieved successfully",
  "user_id": 1,
  "orders": [
    {
      "id": 101,
      "user_id": 1,
      "symbol": "RELIANCE",
      "exchange": "NSE",
      "side": "BUY",
      "quantity": 10,
      "price": 2450.50,
      "status": "PENDING",
      "created_at": "2026-07-02T10:30:00"
    },
    {
      "id": 102,
      "user_id": 1,
      "symbol": "TCS",
      "exchange": "NSE",
      "side": "SELL",
      "quantity": 5,
      "price": 3500.00,
      "status": "EXECUTED",
      "created_at": "2026-07-02T09:15:00"
    }
  ]
}
```

---

### Example 4: Cancel Order

**Request**:
```bash
curl -X POST http://localhost:8000/orders/101/cancel \
  -H "Authorization: Bearer your_jwt_token" \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response**:
```json
{
  "success": true,
  "message": "Order cancelled successfully",
  "order_id": 101
}
```

---

## 🔧 Frontend Integration Checklist

- [ ] Add Authorization header with JWT token to all requests
- [ ] Handle 3 order states: PENDING, PARTIALLY_EXECUTED, EXECUTED
- [ ] Validate input before sending (symbol, quantity, price)
- [ ] Show balance validation error (insufficient balance)
- [ ] Display status from execution response
- [ ] Refresh orders list after creation
- [ ] Handle 404 for get order by ID
- [ ] Handle 400 for invalid requests
- [ ] Show user-friendly error messages
- [ ] Test with both BUY and SELL orders
- [ ] Test with different order types: MARKET, LIMIT, STOP, STOPLIMIT
- [ ] Test order cancellation (only PENDING orders)

---

## 📞 Support

**Issues or Questions?**
- Check the API error message first
- Verify JWT token is valid
- Ensure wallet is initialized for BUY orders
- Check order symbol exists in exchange

**Production URL**: Will be provided by DevOps team
**Local Development**: `http://localhost:8000`

---

## ✅ API Status

- ✅ All endpoints production ready
- ✅ ZERO runtime exceptions verified
- ✅ All 3 order states tested
- ✅ Security checks implemented
- ✅ Error handling comprehensive

**Last Updated**: 2026-07-02
