# MyProductComplete
My Project Target for years
Phase 1: API-first OMS

Build a backend with:

User authentication
Portfolio APIs
Order APIs
Transaction APIs
Market data APIs

Using:

FastAPI
PostgreSQL (psycopg2)
Shoonya / Breeze for live market data
Razorpay for payments
Redis for caching

Deployment:

Vercel / Render (React frontend) — https://www.primepiptrade.com
GCP / Azure VM (FastAPI backend) — https://api.primepiptrade.com
PostgreSQL (cloud-hosted, SSL)
Redis Cloud

Market-data performance:

docs/PERF.md — latency budget, how the live data pipeline works, server settings
docs/FRONTEND_PERF_HANDOVER.md — frontend tasks and the stream contract
python scripts/perf_check.py — measures streams and REST during market hours, prints PASS/FAIL
  (PERF_BASE_URL=https://api.primepiptrade.com to run against production)
