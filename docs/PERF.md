# Market-data performance

How live market data flows from Shoonya to the browser, what each step may
cost, how to measure it, and how the server should be run. Frontend work is
in [FRONTEND_PERF_HANDOVER.md](FRONTEND_PERF_HANDOVER.md).

## Latency budget (tick at the exchange → pixel on screen)

| Step | Budget | Where |
|---|---|---|
| Exchange → Shoonya → our WebSocket | outside our control | `exch_to_recv_ms` in `/api/internal/latency` |
| Tick handling (merge, mark changed) | < 0.2 ms | `OptionChainCache.apply_tick` |
| Batching window | ≤ 250 ms (`CHAIN_FLUSH_MS`) | `ChainBroadcaster` |
| Serialize once, fan out to all clients | < 5 ms | `ChainBroadcaster.flush` |
| Network | 30–80 ms | |
| Browser: apply delta, paint changed cells | < 16 ms (one frame) | frontend |
| **Total (our part)** | **< 300 ms p95** | |

## Measurements

Before (production, 8 Oct 2026, market open, one client):

| Metric | Value |
|---|---|
| Option chain stream | 45 msgs/s, ~1,000 KB/s per client, full chain every tick |
| Indices stream | fixed 5 s timer |
| `/api/market/sectors` | p50 710 ms, p95 821 ms (8 broker REST calls per request) |
| `/api/market/indices` | p50 36 ms, p95 105 ms |

After (same tick rate replayed through the real cache + broadcaster):

| Metric | Value |
|---|---|
| Option chain, `format=full` | ~4 msgs/s, ~75 KB/s per client |
| Option chain, `format=delta` | ~4 msgs/s, ~5 KB/s per client |
| Tick handling | ~73 µs per tick (was: snapshot + IV + JSON per tick per client) |
| Tick received → frame queued | p50 ~220 ms, p95 ~265 ms (the batching window) |
| `/api/market/sectors`, `/indices` | in-memory from ticks (REST only for an index without a tick) |

Re-measure after every deploy:

```bash
# on the server (exact lag, no clock skew)
cd /home/primepiptrade/primepip-backend-server/MyProductComplete
venv/bin/python scripts/perf_check.py

# from anywhere
PERF_BASE_URL=https://api.primepiptrade.com python scripts/perf_check.py
# add PERF_AUTH_TOKEN=<a logged-in user's token> to include /api/internal/latency
```

Server-side live numbers: `GET /api/internal/latency` (admin-only) gives per
stream `msgs_per_sec(_per_client)`, `bytes_per_sec(_per_client)`,
`recv_to_send_ms` and `exch_to_recv_ms` (p50/p95/max over the last 60 s).

Every REST response carries `X-Process-Time` / `Server-Timing` (server time in
ms; for a stream, the time until it opened).

## Morning check (after 09:15 IST)

1. Tick health, logged in: `GET /api/internal/latency` → `pinned_ticks`.
   Every index/sector row should show `subscriptions >= 1`, `age_secs` of a
   few seconds and a close in `close_in_tick` or `known_close`. A row with
   `has_tick: false` or a large `age_secs` is a token the broker isn't
   sending; the server re-subscribes such tokens once a minute and logs
   `[StockFeed] Re-subscribing ...`:
   `sudo journalctl -u backend.service --since "today 09:00" | grep Re-subscribing`
2. Index tiles from ticks: `GET /api/market/indices` → every item has an
   `as_of` (an empty `as_of` means it came from REST).
3. Run `scripts/perf_check.py` on the server (127.0.0.1) for exact lag:
   expect `/indices` and `/sectors` p95 < 100 ms and chain first frame
   < 300 ms once all index ticks are healthy.

## How the pipeline works

1. **One WebSocket** to Shoonya (`ShoonyaOptionFeed`). Index, sector and the
   Nifty 50 watchlist tokens are pinned on it at startup; option tokens are
   subscribed while someone views that chain.
2. **Tick → cache, O(1).** `OptionChainCache.apply_tick` merges only the
   fields that changed and records them. No JSON, no IV, no I/O per tick.
   Index ticks update every chain's spot live.
3. **Flush every 250 ms** (`CHAIN_FLUSH_MS`, env, 50–2000). If nothing
   changed nothing is sent. Otherwise each frame kind is built **once**:
   - `format=full` (default, current frontend): the whole chain, same
     shape as before plus `seq` and `srv_ts`;
   - `format=delta`: `{"t":"s"}` snapshot on connect, then `{"t":"d"}`
     with only changed fields; `seq` +1 per delta.
4. **Fan-out.** The same bytes go on every client's queue (max 8). A full
   queue is emptied: a delta client gets a fresh snapshot, a full client the
   newest frame. A slow browser never delays anyone else.
5. **IV** is recomputed at flush time only for legs whose price changed (all
   legs at most once a second when spot moves), from the bid/ask mid when
   the spread ≤ 20% of mid, else from a price traded today, else `null`.
6. **Pinned tokens stay complete.** The previous close only arrives in the
   broker's first full frame, so it is remembered for the day (from that
   frame or the first REST answer). A pinned token without a fresh tick or
   a previous close for 60 s during market hours is re-subscribed.
7. **Indices / sectors / top movers** are read from the tick cache. The
   indices stream checks every 250 ms and sends only when a value changed;
   sectors every 1 s; top movers rebuild every 5 s. When a value has to come
   from REST (feed not up yet), the next rebuild waits 5–300 s so the
   broker is never hammered.

Data rules the frontend can rely on: a price is `null` when there is none
(never `0`), `ltp_stale: true` means no trade today, `iv: null` means no
reliable IV, `as_of` is the tick time.

## Running the server

`requirements.txt` adds `orjson` (fast JSON; the code falls back to the
standard library if it is missing) and `uvloop` (Linux only).

```bash
cd /home/primepiptrade/primepip-backend-server/MyProductComplete
git pull
venv/bin/pip install -r requirements.txt
sudo systemctl restart backend.service
```

Recommended `ExecStart` (`sudo systemctl edit --full backend.service`, then
`sudo systemctl daemon-reload && sudo systemctl restart backend.service`):

```ini
ExecStart=/home/primepiptrade/primepip-backend-server/MyProductComplete/venv/bin/uvicorn app:app \
    --host 127.0.0.1 --port 8000 --workers 1 \
    --loop uvloop --http httptools --no-access-log --timeout-keep-alive 75
Restart=always
RestartSec=5
```

- **One worker.** Ticks, chains and stream subscribers live in this process's
  memory, and the broker allows one session. More workers would each log in
  and split the data. Scale by moving streaming to its own process first.
- `--no-access-log`: the latency middleware already logs every request once.
- Also delete the stray `EOF` line at the end of the current unit file.

nginx in front of the app (`location /` for the API server block):

```nginx
proxy_http_version 1.1;
proxy_set_header Connection "";
proxy_buffering off;          # SSE frames must not wait in a buffer
proxy_read_timeout 3600s;     # streams stay open for hours
proxy_send_timeout 3600s;
# gzip: never list text/event-stream in gzip_types
```

The app also sends `X-Accel-Buffering: no` on every stream, which nginx
honours per response.

Health: `GET /healthz` → `{"status":"ok","broker_connected":…,"market_feed_connected":…}`.

### Sizing the VM

Check CPU at market open before changing the machine:

```bash
pidstat -u -p $(systemctl show -p MainPID --value backend.service) 5 12   # 1 minute, 5 s samples
```

Under 40% on the e2-medium (2 shared vCPU) for one user: stay. Sustained
over 60%, or many concurrent chain viewers: move to `c3-standard-4` (or
`c2d-standard-2`) in `asia-south1`, close to the exchange.
