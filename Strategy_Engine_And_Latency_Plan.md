# Plan: Lower latency, accurate system design, paper strategy engine

## Context
After the VM migration (now 8.231.74.171, unit `backend.service`, path `~/primepip-backend-server/MyProductComplete`), the backend works but broker-dependent endpoints are slow: `/explore` 7–8s, `/search` 5–8s, `/market/indices` ~4s. The goal is to cut latency, document the real architecture, then build a daily strategy engine.

The user decided:
- Three separate efforts, done in order: A → B → C.
- Strategy engine: **paper trading first**, **index options only** (NIFTY, BANKNIFTY, FINNIFTY), **master account only**.

---

## A. Latency (do first)

**Root cause (verified in code).**
- `StocksService._fetch_watchlist_summaries` (`service/stocksService/StocksService.py:174-215`) makes **40 blocking `shoonya.get_stock_quote` REST calls** on every cache miss of `/explore`, all with `run_in_executor(None, …)`.
- `None` means Python's shared default pool, which on a small VM has about 5–6 threads. The 40 calls queue up behind it.
- The same pool runs `/quote`, `/chart`, `/indices`, the option chain and top movers, so a single `/explore` miss slows all of them at once.

Fixes, in order:

1. **Dedicated, bounded executor for broker I/O.**
   - Create `ThreadPoolExecutor(max_workers=16, thread_name_prefix="broker-io")` in the `app.py` lifespan and set it with `loop.set_default_executor(...)`. Every existing `run_in_executor(None, …)` then uses it with no edits at the call sites.
   - Shut it down in the lifespan teardown.
2. **Serve `/explore` from the WebSocket tick cache, not REST.**
   - Add `pin(keys)` to `marketengine/ShoonyaStockFeed.py`: keys in a `_pinned` set are subscribed once and skipped by `_evict_idle_tokens` (:103).
   - In `app.py` (~:326), after `ShoonyaStockFeed` is created, pin the 40 watchlist tokens from `StockWatchlist`.
   - In `_fetch_watchlist_summaries`, read `stock_feed.get_tick(f"{exch}|{token}")` first. Use REST only for stocks with no tick yet, with an `asyncio.Semaphore(8)`.
   - `StocksService` already receives `stock_feed` for quotes; reuse the same tick-to-summary normalization as `get_quote`.
   - Expected result: a miss on `/explore` goes from 7–8s to under 50ms once ticks are flowing.
3. **Stale-while-revalidate on `/explore`.** If cached data exists but is past its TTL, return it right away and refresh in the background, instead of holding `_explore_lock` (:144) through the fetch.
4. **`GZipMiddleware(minimum_size=1000)`** in `app.py` next to `CORSMiddleware` (~:428).
5. **Single worker stays.** Keep one uvicorn worker, because the tick cache and the WebSocket live in one process. Commit the VM's real unit file to `deploy/backend.service` along with the nginx site config, so they're versioned. Add nginx upstream `keepalive 16` and `proxy_http_version 1.1`.
6. **Config hygiene found during the incident:**
   - There are two `.env` files on the VM. Delete the outer `~/primepip-backend-server/.env`; the code only reads the inner one (`ShoonyaConnection.py:18`).
   - Fix the Supabase pooler user format error (`EINVALIDUSERINFO`): the user must be `postgres.<project-ref>`.
   - Refresh the expired Breeze session key.

**Measure.** The existing `[latency]` / `Server-Timing` middleware logs per-request ms. Record p50 and p95 for `/explore`, `/quote`, `/chart`, `/market/indices` before and after the fixes, using `journalctl -u backend.service | grep latency`.

Tests: extend `tests/` for `pin()` surviving eviction, explore reading from ticks with REST fallback only for missing stocks, and stale-while-revalidate.

---

## B. System design doc
Update the existing `systemDesign.md` instead of writing a new file. Cover:
- Components and the request → service → broker data flow.
- The single shared WebSocket, tick caches and their TTLs, executors, background loops (`_supervised_background_task`, `app.py:118`).
- Failure modes: broker down, token expiry, stale feed, OOM.
- Deployment (VM, nginx, systemd, env vars list without values).
- The latency budget per endpoint from A.

---

## C. Strategy engine (paper, index options, master account) — a separate tier

**Two tiers, decided by the user.** The strategy engine runs as its own process: a separate systemd unit, `strategy-engine.service`, which can later move to its own VM. Both tiers use the same repo and venv.

- **The server tier stays the only owner** of the Shoonya session and WebSocket. Brokers limit concurrent sessions, and keeping one owner gives one tick cache.
- **Ticks reach the strategy tier over Redis pub/sub**, using the Redis already behind `PositionCache`.
  - New module `marketengine/TickPublisher.py`, registered with `option_feed.on_tick`, publishes compact JSON to `ticks:{EXCH|TOKEN}`.
  - It only publishes tokens that are listed in a Redis set, `strategy:subscriptions`.
  - The WebSocket thread only does a non-blocking `publish` through a small queue and worker thread.
- **The strategy tier sends subscription requests** by writing to `strategy:subscriptions` and sending a `strategy:control` message. The server tier calls `ensure_subscribed` / `release` with reference counting.
- **Option chains and greeks for strategies.** The strategy tier calls a new internal endpoint, `GET /internal/optionchain/{underlying}/{expiry}`, which wraps `OptionChainService.get_cache_for_stream`, or reads a periodic chain snapshot published to Redis.
- **Orders.** PAPER fills happen inside the strategy tier (PaperBroker below). LIVE orders, later, go to a new internal endpoint, `POST /internal/orders/live`, which reuses `_create_order_row_with_checks` and `LiveOrderRoutingService`. That way wallet, margin, the kill switch and rate limits stay in one place.
- **Internal endpoints:**
  - Bound under the `/internal` prefix.
  - Authenticated with an `INTERNAL_API_TOKEN` header.
  - Blocked from the public internet in nginx (`location /internal { allow 127.0.0.1; deny all; }`, or a private VPC IP once the tier moves to its own VM).
- **The strategy process** has its own entrypoint, `strategy_engine_main.py`. It starts an asyncio loop that runs the runner, the Redis subscriber and its own small admin API, or exposes control through the server tier's `/admin/strategies`, which talks to it over Redis `strategy:control`. It has a separate systemd unit with `MemoryMax=300M`, so a strategy bug can't take down the user-facing API.

**Reuse what exists:**
- `ShoonyaOptionFeed.on_tick` / `ensure_subscribed` / `release` (`marketengine/ShoonyaOptionFeed.py:90,204,218`)
- `OptionChainService.get_cache_for_stream` (:294) and `release_chain` (:201) to hold chains with greeks by reference count. A plain `get_chain` does not take a reference.
- `OptionMaster` and `appconfig/fno_lot_sizes.json` for contracts and lot sizes
- `_supervised_background_task`
- The `positionTickService.py:117-132` → `stopOrderTriggerService.py:48` pattern for tick triggers
- The `PostgresConnectionFactory` persistence style used in `database/marginenginepersistence/`

**Don't reuse:**
- `ExecutionEngine` / `MatchingEngine`, because they match users against each other, not against market prices.
- Paper fills must never touch wallets, `MarginEngine`, `positions` or `order_book`.

New package `service/strategyengine/`:
- **`base.py`**: `Strategy` ABC with hooks `on_day_start`, `on_entry`, `on_tick`, `on_fill`, `on_exit`. `StrategyContext` exposes `chain()`, `spot()`, `place_basket()`, `square_off()`, `params`, `log()` and `now_ist`. Strategies never call the broker directly.
- **`models.py`**: dataclasses `LegSpec`, `Quote`, `Fill`, `RunState`.
- **`registry.py`**: an `@register(key)` decorator; instances are built from rows in the `strategies` table and validated against `PARAMS_SCHEMA`.
- **`strategies/short_straddle.py`**: the reference strategy. NIFTY, enter at 09:20, sell an ATM CE+PE pair, 30% stop loss per leg, square off at 15:15, 1 lot.
- **`runner.py`**: one supervised loop with a 1-second scheduler tick.
  - Entry and exit fire once per run, made idempotent by DB state.
  - WebSocket ticks are handed to an asyncio queue with `call_soon_threadsafe`, so the WebSocket thread never blocks, and the queue keeps only the latest quote per token.
  - On startup it recovers OPEN runs. A restart after exit time squares off immediately.
- **`brokers/paper_broker.py`**: SELL fills at the bid, BUY at the ask. Fallback is LTP ± 0.5% slippage, rounded to the ₹0.05 tick. Rejects quotes older than 3 seconds or with a spread wider than the configured maximum. Baskets are all-or-nothing. `brokers/live_broker.py` is a stub for now.
- **`risk.py`**: per-strategy max loss and max lots, global daily max loss with a kill switch, freeze-qty slicing (`appconfig/fno_freeze_qty.json`), and an order-rate token bucket. The halt flag is kept in the DB so it survives restarts.
- **`appconfig/MarketCalendar.py`** + `appconfig/nse_holidays.json`: trading-day, market-open and next-trading-day helpers. There is no holiday calendar today.
- **Migration `migrations/0006_create_strategy_engine.sql`**: tables `strategies`, `strategy_runs` (unique on strategy_id + trade_date), `strategy_orders`, `strategy_legs`, `strategy_pnl_snapshots`, `strategy_events` (audit trail) and `strategy_engine_state`. Persistence code goes in `database/strategypersistence/`, with all DB calls run in the executor. PnL snapshots are written every 60 seconds.
- **`api/strategies.py`** (`/admin/strategies`): list, enable/disable, edit params, runs/legs/PnL, square-off, halt/resume and engine status. Protected by a new `require_admin` dependency that checks the `ADMIN_USER_IDS` env variable.
- **Wiring**: start the runner in the `app.py` lifespan after `option_feed` is created (~:317) and register the router (~:409).

**Phase 1** (end-to-end paper straddle): calendar, migration, persistence, PaperBroker, base and registry, straddle, runner with entry/exit and tick stop loss, `/halt` and `/runs`.

**Phase 2**: full risk layer, recovery, PnL snapshots, staleness watchdog (no ticks for more than 10 seconds blocks new entries), and the remaining API.

**Phase 3**: strangle and iron condor (hedge legs placed first and exited last), a charges model, and a daily report.

**Phase 4**: LiveBroker behind the mode flag and `SHOONYA_LIVE_ORDERS_ENABLED`.

---

## D. TradingView + Claude (after C, Phase 1)

**TradingView charts in the frontend**
- Use TradingView **Lightweight Charts** (open source, Apache-2.0) now. Apply for the free **Advanced Charting Library** licence if drawing tools and indicators are wanted.
- **Backend:** add a UDF-compatible datafeed router, `api/tvDatafeed.py`, mounted at `/api/tv/`, with `/config`, `/symbols`, `/search`, `/history` and `/time`.
  - It reuses `StocksService.get_chart` / `CandleService.get_index_candles`, the `StockSymbolMaster` search and `OptionMaster`.
  - Live bars come from the existing tick SSE/stream endpoints.
  - Strategy entries and exits are exposed as chart marks (`/marks`) from `strategy_legs`.

**TradingView webhook signals**
- New route `POST /webhooks/tradingview` (`api/tvWebhook.py`), with these checks:
  - Allow only TradingView's published webhook source IPs, enforced in nginx and in the app.
  - Require a per-strategy `secret` field in the JSON body.
  - Use idempotency on `alert_id` + timestamp, and reject alerts older than 30 seconds.
  - Apply a rate limit.
- **Payload schema:** `{secret, strategy_key, action: ENTER|EXIT, underlying, params?}`.
- **Signal flow:**
  - The webhook only publishes the validated signal to Redis `strategy:signals`. It never places orders.
  - The strategy tier's `WebhookSignalStrategy` consumes signals, and every signal still passes through `RiskManager`.
  - Every signal is stored in `strategy_events`.
- This needs a paid TradingView plan on the user's side.

**Claude assistant**
- **New module:** `service/aiAssistant/`, using the `anthropic` SDK.
  - Model `claude-sonnet-5-5` for most features; `claude-opus-5-5` for strategy drafting.
  - Key in `ANTHROPIC_API_KEY`.
  - Enable prompt caching on the system prompt and reference data.
- **Features:**
  1. **Pre-market brief** (08:45 IST job): spot, VIX, event calendar, OI build-up and the strategy plan for the day. Stored, and shown in the app.
  2. **Strategy drafting:** plain English → a validated `params_json` for a registered strategy class, or Pine Script. It is always saved as **disabled**, and an admin must review and enable it.
  3. **Explain this chart/position:** the client asks about a symbol or position; Claude gets structured data (quote, candles summary, greeks), not screenshots.
  4. **Post-trade journal:** an end-of-day summary per `strategy_run` built from `strategy_events`.
- **Guardrails:**
  - Claude never calls order endpoints and has no tool access to them.
  - Its outputs are labelled as not investment advice.
  - Per-user rate limits and a token budget.
  - Responses are cached per symbol and minute.
  - All calls run in the background or are streamed, so they never sit on the latency-critical paths from A.

**Compliance gate (before any live algo):** SEBI's retail algo framework requires the broker or exchange to approve the algo, every order to carry an algo ID, and a static IP (already whitelisted). Order rates above the threshold need registration. Get confirmation from Shoonya/Finvasia before Phase 4 of C.

### Factors to consider when writing strategies (Indian index options)
- **Expiry:** Weekly and monthly expiry days change by NSE/SEBI circular, so read them from `OptionMaster` and never hardcode a weekday. Gamma and theta spike on expiry day.
- **Lot size and freeze quantity:** Both are revised by circular. Keep them in config and review every quarter.
- **Charges:** STT on sold premium, plus STT on intrinsic value if an ITM leg is exercised, so don't hold ITM legs to expiry. Also exchange fees, SEBI fee, stamp duty, GST and brokerage. Model all of them in paper PnL.
- **Spread and slippage:** Far-OTM strikes and FINNIFTY are thin. Cap the spread % and prefer strikes with high open interest.
- **Time of day:** 09:15–09:20 has wide spreads, liquidity is best mid-session, and expiry afternoons whipsaw.
- **IV regime:** Filter entries on India VIX and IV percentile. Short premium suffers when VIX is rising.
- **Event days:** RBI policy, Budget, elections, US CPI/FOMC and heavyweight results. Keep a blackout calendar.
- **Gaps:** Stops can be gapped through. Close intraday positions before the close.
- **Margin and hedges:** SPAN plus exposure margin. Hedged structures need far less margin; place hedges first.
- **Broker limits:** API rate limits, order latency, idempotent order tags (`primepip_{id}`), and reconciliation through the order book.
- **Feed staleness:** Never trade on a stale or cached snapshot; check quote age before every fill.
- **Square-off:** The broker auto-squares MIS positions around 15:15–15:20, so exit earlier.
- **Sizing and drawdown:** Fixed lots per unit of capital, a daily loss cap, and a maximum drawdown that disables the strategy.
- **Backtest vs live:** Backtests often assume LTP fills, use wrong historical lot sizes and ignore latency. Paper-trade for several weeks before going live.
- **Overfitting:** Use few parameters, test out of sample and across regimes, and don't tune to one expiry cycle.

---

## Verification
- **A:**
  - `pytest tests/` passes.
  - On the VM, restart `backend.service` and compare `[latency]` p50/p95 before and after. Target: `/explore` under 300ms warm and under 1.5s cold.
  - `/market/indices` and `/quote` must stay fast while `/explore` is being hit in a loop.
  - Memory stays flat (`ps`/`free -h`).
- **B:** Review `systemDesign.md` against the code paths listed.
- **C:**
  - Run the new tests: calendar, paper broker, risk, straddle (with a fake clock and fake chain), runner recovery, tick dispatch, API.
  - On a trading day, enable the straddle in paper mode at 1 lot. Expect a run at 09:20 with 2 legs filled between bid and ask, the stop loss firing on a spike, and square-off at 15:15.
  - Restart mid-session and confirm the run resumes.
  - Confirm the wallet, `positions` and `order_book` tables are unchanged.
  - Two tiers:
    - Stop `strategy-engine.service`: the API stays healthy.
    - Kill `backend.service`: the strategy tier detects that ticks are stale within 10 seconds and blocks entries.
    - `/internal/*` returns 403 from the public internet.
- **D:**
  - The TradingView chart loads history and live bars from `/api/tv/*`.
  - Webhook tests:
    - A replayed alert is rejected.
    - A wrong secret returns 401.
    - A valid alert produces a `strategy_events` row and a paper fill.
  - Claude features:
    - A drafted strategy is saved disabled.
    - The brief job runs once on a trading day.
    - No Claude call appears in the `[latency]` logs for `/quote`, `/chart` or `/explore`.
