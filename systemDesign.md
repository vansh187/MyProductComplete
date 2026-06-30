# System Design — PrimePipTrade (MyProductComplete)

## Overview

PrimePipTrade is a full-stack trading platform built with FastAPI and PostgreSQL. It provides
authenticated order management, live market data streaming, portfolio tracking, and payment
processing. The backend is structured in clean API / Service / Persistence layers.

> **Database note:** Early documentation described a MySQL-based design. The production system
> uses **PostgreSQL exclusively** via `database/PostgresConnectionFactory.py`. The MySQL file
> `database/ConnectionFactory.py` is **legacy** — it is only referenced by
> `scheduler/marketPriceSchedular.py` and `SeedDataScript/marketPriceSeed.py` (utility scripts,
> not the production app). Do not use it for new code.

---

## Architecture Layers

### 1. API Layer (`api/`)

| File | Endpoints |
|------|-----------|
| `api/signup.py` | `POST /signup` |
| `api/login.py` | `POST /login` |
| `api/auth_google.py` | Google OAuth callback |
| `api/orders.py` | `GET/POST /orders`, `GET /orders/{id}`, `DELETE /orders/{id}` |
| `api/portfolio.py` | `GET /portfolio` |
| `api/trade.py` | `GET /trade-history` |
| `api/Dashboard.py` | `GET /dashboard` |
| `api/marketquotes.py` | `GET /api/market/indices`, `GET /api/market/indices/stream`, `GET /api/market/marquee`, `POST /api/market/quotes`, `GET /api/market/quotes/stream` |
| `api/sectorPerformance.py` | `GET /api/market/sectors`, `GET /api/market/sectors/stream` |
| `api/topMovers.py` | `GET /api/market/top-movers`, `GET /api/market/top-movers/stream` |
| `api/AddfundstoWallet.py` | Razorpay payment initiation |
| `api/VerifyFundTransaction.py` | Razorpay payment verification |
| `api/admin_shoonya.py` | `GET /admin/shoonya/auth-url`, `POST /admin/shoonya/exchange-code`, `GET /admin/shoonya/status` |

### 2. Authentication and Security (`utils/`)

- `utils/jwt_handler.py` — Creates and validates JWT tokens. Secret key read from `SECRET_KEY` env var.
- `utils/auth_dependency.py` — `HTTPBearer` FastAPI dependency; validates tokens on every protected route.
- `utils/password_hasher.py` — bcrypt password hashing at signup; verification at login.
- `service/googleAuthService.py` — Verifies Google ID tokens via `GOOGLE_CLIENT_ID` env var.

### 3. Service Layer (`service/`)

| File | Responsibility |
|------|---------------|
| `service/signupService.py` | Validates and creates new users |
| `service/loginService.py` | Validates credentials, issues JWT |
| `service/googleAuthService.py` | Google OAuth token verification and user provisioning |
| `service/orderService.py` | Order creation, retrieval, cancellation |
| `service/portfolioService.py` | Holdings updates for buy/sell trades |
| `service/tradeHistoryService.py` | Trade recording and retrieval |
| `service/executionEngine.py` | Coordinates order matching, portfolio update, trade insert in a single DB transaction |
| `service/dashboard/dashboardservice/DashboardService.py` | Aggregates dashboard summary data |
| `service/walletbalance/WalletBalanceService.py` | Wallet balance queries |
| `service/razorpay/RazorPayMangerService.py` | Razorpay order creation and signature verification |
| `service/matchingEngine/matchingEngineService/matching_engine_service.py` | Order book matching logic |
| `service/sectorPerformance/SectorPerformanceService.py` | Fetches NSE sector indices in parallel via Shoonya |
| `service/topMovers/TopMoversService.py` | Fetches Nifty 50 top gainers/losers; maintains in-memory cache |

### 4. Persistence Layer (`database/`)

All production persistence classes use **`PostgresConnectionFactory`**.

| File | Responsibility |
|------|---------------|
| `database/PostgresConnectionFactory.py` | **Production** — creates PostgreSQL connections via `DATABASE_URL` or `PG*` env vars |
| `database/ConnectionFactory.py` | **LEGACY (MySQL)** — only used by legacy utility scripts; not used in production |
| `database/loginPersist.py` | User lookup for authentication |
| `database/signUpPersist.py` | User insertion at signup |
| `database/googleAuthPersistence.py` | User lookup and creation for Google OAuth |
| `database/orderPersistence.py` | Order CRUD operations |
| `database/portfolioPersistence.py` | Holdings insert/update |
| `database/tradeHistoryPersistence.py` | Trade record insertion and retrieval |
| `database/dashboardpersistence/DashboardPersistence.py` | Dashboard aggregation queries |
| `database/razorpaypersistence/RazorPayPersistence.py` | Payment record persistence |
| `database/walletbalancepersistence/WalletBalancePersistence.py` | Wallet balance persistence |

### 5. Market Data Layer (`marketengine/`)

| File | Responsibility |
|------|---------------|
| `marketengine/ShoonyaConnection.py` | **Primary** — Shoonya (Finvasia) REST API client via OAuth 2.0; provides `get_index_quote(exchange, token)` |
| `marketengine/BreezeProvider.py` | **Fallback** — ICICI Breeze WebSocket + REST client |
| `marketengine/BreezeSessionManager.py` | Daily Breeze session refresh (8:45 AM IST weekdays) |
| `marketengine/config.py` | Reads Breeze credentials from env vars |

**Data flow for market quotes:**
1. Shoonya `get_quotes(exchange, token)` → primary
2. Breeze `get_quotes()` → fallback if Shoonya unavailable
3. Breeze `get_historical_data()` → second fallback (NSE only)

---

## Architecture Diagram

```
Client (Browser / Mobile)
  │
  │ HTTPS
  ▼
FastAPI  (app.py — lifespan manages Shoonya + Breeze startup)
  │
  ├── Auth Middleware (JWT / Google OAuth)
  │
  ├── API Layer (routers)
  │     ├── /signup  /login  /auth/google
  │     ├── /orders  /portfolio  /trade-history  /dashboard
  │     ├── /api/market/indices[/stream]
  │     ├── /api/market/sectors[/stream]
  │     ├── /api/market/top-movers[/stream]
  │     ├── /api/market/quotes[/stream]  /api/market/marquee
  │     ├── /api/wallet  /api/payment
  │     └── /admin/shoonya/*
  │
  ├── Service Layer
  │     ├── Business logic, order matching, portfolio updates
  │     └── Market data fetchers (parallel asyncio.gather)
  │
  ├── Persistence Layer
  │     └── PostgresConnectionFactory → PostgreSQL (psycopg2 + SSL)
  │
  ├── Market Data
  │     ├── ShoonyaConnection (primary — REST + OAuth)
  │     └── BreezeConnect (fallback — REST + WebSocket)
  │
  ├── Payment
  │     └── Razorpay SDK
  │
  └── Cache
        └── Redis (session / rate limiting)
```

---

## Database: PostgreSQL

Connection: `psycopg2` via `DATABASE_URL` (preferred) or individual `PG*` env vars. SSL required.

### Schema

**`users`**
| Column | Type | Notes |
|--------|------|-------|
| `user_id` | serial PK | |
| `first_name` | varchar | |
| `last_name` | varchar | |
| `email` | varchar UNIQUE | |
| `password` | varchar | bcrypt hash; `'GOOGLE_AUTH'` for OAuth users |
| `phone_number` | varchar UNIQUE NULLABLE | NULL for Google OAuth users |
| `created_at` | timestamp | |

**`orders`**
| Column | Type | Notes |
|--------|------|-------|
| `order_id` | serial PK | |
| `user_id` | int FK → users | |
| `symbol` | varchar | NSE ticker |
| `order_type` | varchar | `BUY` / `SELL` |
| `quantity` | int | |
| `price` | numeric | |
| `status` | varchar | `PENDING`, `EXECUTED`, `CANCELLED` |
| `created_at` | timestamp | |

**`holdings`**
| Column | Type | Notes |
|--------|------|-------|
| `holding_id` | serial PK | |
| `user_id` | int FK → users | |
| `symbol` | varchar | |
| `quantity` | int | |
| `avg_price` | numeric | |

**`trade_history`**
| Column | Type | Notes |
|--------|------|-------|
| `trade_id` | serial PK | |
| `buy_order_id` | int FK → orders | |
| `sell_order_id` | int FK → orders | |
| `symbol` | varchar | |
| `quantity` | int | |
| `price` | numeric | |
| `executed_at` | timestamp | |

---

## Real-Time Streaming (SSE)

All streaming endpoints use Server-Sent Events (SSE) via FastAPI `StreamingResponse`.

| Endpoint | Push interval (open) | Push interval (closed) | Data source |
|----------|---------------------|----------------------|-------------|
| `/api/market/indices/stream` | 5s | 60s | Shoonya / Breeze |
| `/api/market/sectors/stream` | 5s | 60s | Shoonya (8 NSE tokens in parallel) |
| `/api/market/top-movers/stream` | On cache refresh | On cache refresh | In-memory cache |
| `/api/market/quotes/stream` | 12s | 60s | Breeze historical |

Top movers cache refreshes every 5 minutes (market open) / 10 minutes (closed) via a background asyncio task started at lifespan.

---

## Startup Sequence (lifespan)

```
1. ShoonyaConnection.connect()  — tries stored token from .env
2. If token invalid → ShoonyaConnection.auto_login()  — headless Chrome + TOTP
3. If Shoonya unavailable → BreezeConnect session
4. Background tasks started:
     - shoonya_daily_refresh  (8:30 AM IST weekdays)
     - top_movers_refresh     (every 5/10 min)
     - breeze_daily_refresh   (8:45 AM IST weekdays, if Breeze active)
5. App ready to serve requests
```

---

## Environment Variables

| Variable | Used by |
|----------|---------|
| `SECRET_KEY` | JWT signing |
| `DATABASE_URL` / `PG*` | PostgreSQL connection |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` | Redis |
| `GOOGLE_CLIENT_ID` | Google OAuth verification |
| `RAZORPAY_API_KEY`, `RAZORPAY_SECRET_KEY` | Razorpay |
| `SHOONYA_USER_ID`, `SHOONYA_PASSWORD`, `SHOONYA_VENDOR_CODE`, `SHOONYA_API_SECRET`, `SHOONYA_IMEI`, `SHOONYA_TOTP_SECRET` | Shoonya login |
| `SHOONYA_SESSION_TOKEN`, `SHOONYA_ACCESS_TOKEN` | Shoonya session (auto-populated) |
| `BREEZE_API_KEY`, `BREEZE_SECRET_KEY`, `BREEZE_SESSION_TOKEN` | Breeze |

---

## Deployment

| Component | Platform |
|-----------|----------|
| Frontend (React) | Vercel / Render — `https://www.primepiptrade.com` |
| Backend (FastAPI) | GCP / Azure VM — `https://api.primepiptrade.com` |
| Database | PostgreSQL (cloud-hosted, SSL required) |
| Redis | Redis Cloud (`megasafe-dreamy-inerrant-47439.db.redis.io`) |
