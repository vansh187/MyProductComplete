# Frontend handover: live market data speed

**For:** the React (Vite) frontend team
**Goal:** live data on screen within 300 ms of the tick, so the platform can
compete on speed before the strategy engine is built on top of it.
**Backend status:** done and deployed with this change (see [PERF.md](PERF.md)).
The current frontend keeps working unchanged; the tasks below unlock the rest
of the speed-up.

## The problem (measured live, 8 Oct 2026, market open, one user)

| Journey step | Now | Target |
|---|---|---|
| Click "Chain" → table visible | 1,530 ms (first stream message arrives after 130–180 ms, so ~1.35 s is spent in the frontend) | < 300 ms cold, < 50 ms when returning from the terminal |
| Option chain tick → screen | ~1.2 s p50, ~2 s p95 | < 300 ms p95 |
| Option chain traffic | 45 msgs/s × 22.7 KB full chain ≈ 1 MB/s; the browser re-parses and re-renders the whole table 45×/s | ≤ 4 msgs/s, < 20 KB/s, only changed cells re-render |
| Dashboard NIFTY | polls `/api/market/indices` every 10 s; both streams close ~3.4 s after page load, right before a `/v1/logout` call | pushed within ~250 ms of a change, stream stays open |
| Data | 21350 CE showed LTP 22220.55 (the NIFTY spot); stale LTPs; IV ~197% on illiquid strikes; `as_of: null` | correct |

The backend now batches ticks (≤ 4 msgs/s), offers a delta format, fixes the
data at the source, and pushes indices on change. What remains is in the
browser.

## Backend contract (what you build against)

### Option chain stream

`GET /api/market/{underlying}/optionchain/stream?expiry=YYYY-MM-DD&format=delta`

`underlying`: `nifty | banknifty | finnifty | sensex`; `expiry` optional
(defaults to the nearest). All messages arrive on the default `message`
event; switch on `t`. Lines starting with `:` are keep-alives that
`EventSource` ignores.

| `t` | When | Shape |
|---|---|---|
| `"s"` snapshot | on connect, and again after any resync | `{t, seq, sym, exch, exp, spot, srv_ts, rows: [[strike, ce_token, pe_token, ce\|null, pe\|null], ...]}` |
| `"d"` delta | at most every 250 ms, only if something changed | `{t, seq, spot, srv_ts, u: [[token, {changed fields}], ...]}` |
| `"e"` status | connecting / broker down / failure | `{t, sym, exp, errors: [{reason}], srv_ts}` |

- `seq`: every delta is the previous `seq + 1`. A gap means a message was
  missed: close and reopen the stream (you get a fresh snapshot).
- A new `"s"` always **replaces** the state.
- `"e"` reasons: `connecting`, `shoonya_disconnected`, `no_option_data`,
  `no_expiry_available`, `initialization_failed`, `option_chain_failed`.
  Keep showing the last data with a banner; do not clear the table.
- Leg fields: `tsym, token, lot_size, tick_size, ltp, bid, ask, volume, oi,
  poi, oi_change, iv, ltp_stale, ts, exch_ts`. A delta only carries the
  fields that changed; merge them into the leg.
- `srv_ts` (epoch ms) is when the server sent it: `Date.now() - srv_ts` is
  the network + browser lag (on a correctly synced clock).

`format=full` (the default, what the app uses today) is unchanged except
that it now arrives ≤ 4×/s and carries `seq` and `srv_ts`. It stays until
the app has moved to `format=delta`.

### Indices and sectors

- `GET /api/market/indices/stream`: first frame on connect, then a frame
  only when a value changes (checked every 250 ms). Shape unchanged:
  `{market_status, indices: [{name, stock_code, exchange, value, open, high,
  low, change, change_pct, as_of, source}], errors, last_updated, srv_ts}`.
  `as_of` is now the tick time (ISO, IST).
- `GET /api/market/sectors/stream`: same idea, checked every 1 s.
- `GET /api/market/indices` and `/sectors` are now in-memory (single-digit
  ms); use them only for a first paint or when a stream is down.
- No auth is needed for any of these market-data routes.

### Data display rules

| Field | Rule |
|---|---|
| `ltp`, `bid`, `ask` | `null` means "none": show `–`. **Never** fall back to spot or any other value (`ltp \|\| spot` caused the "CE = 22220.55" bug). |
| `ltp_stale: true` | No trade today: grey out the LTP, prefer showing bid/ask. |
| `iv: null` | No reliable IV (wide spread or no trade): show `–`. |
| `oi_change` | Change since the previous day's OI. |
| `as_of` (indices) | Drive an "updated Xs ago" label next to Live Prices. |

## Tasks

Do them in order; measure after each (task F1 gives the numbers). Each
prompt below can be pasted into Claude in the frontend repo as is.

### F1. Latency instrumentation (measure first)

```
Add opt-in latency instrumentation to the React app. Nothing user-visible.
- Create src/lib/perf.ts with mark(name), measure(name, startMark) using
  performance.mark/measure, enabled only when localStorage.perf === '1'.
- For every option-chain / indices stream message that carries srv_ts, sample
  1 in 20 and log Date.now() - srv_ts (network + parse lag). Also log the
  time from message receipt to the next paint (requestAnimationFrame).
- Mark "chain:click" (Chain button), "chain:first-msg" (first stream
  message), "chain:first-paint" (useLayoutEffect after the first rows
  render) and log click→first-msg and first-msg→first-paint.
Acceptance: with localStorage.perf='1', the console shows click→first-msg,
first-msg→first-paint and a rolling tick→screen lag.
```

### F2. Chain first paint < 300 ms

```
Clicking "Chain" on /terminal/fno shows the table after ~1530 ms although the
SSE first event arrives in ~150 ms. Find and remove the ~1.35 s delay.
Look for: the component waiting on another request (expiries, positions, lot
size, profile) before rendering rows; setTimeout/debounce/throttle on the
first message; a loading state waiting for N messages or a ready flag; a
React.lazy route chunk fetched only on click; the stream being closed and
reopened when moving terminal -> chain.
Fix:
- Render rows from the FIRST stream message; load positions/expiries in
  parallel and fill them in later.
- One shared option-chain stream per (symbol, expiry) in a module-level store
  (Zustand or useSyncExternalStore), reused by the terminal and the chain
  view, keeping the last snapshot; the chain view paints instantly from it.
- Prefetch the chain route chunk on hover/focus of "Chain" and when the
  NIFTY terminal mounts.
- Skeleton with fixed row heights (no layout shift).
Acceptance (perf logging on): chain:click -> chain:first-paint < 300 ms cold,
< 50 ms when returning from the terminal.
```

### F3. Consume the delta protocol, re-render only changed cells

```
Switch the option chain to /api/market/{underlying}/optionchain/stream?format=delta.
Protocol (backend is live): messages on the default event, JSON with t:
  "s" snapshot {seq, sym, exch, exp, spot, srv_ts, rows:[[strike, ce_token,
      pe_token, ce|null, pe|null]]} -> replace state
  "d" delta {seq, spot, srv_ts, u:[[token, {changed fields}]]} -> merge fields
  "e" status {errors:[{reason}]} -> banner, keep the data
If a delta's seq != last seq + 1, close and reopen the stream.
Implementation:
- Normalized store: rowsByStrike (stable order) + quotesByToken; each price
  cell subscribes to ONE token via a selector (Zustand or
  useSyncExternalStore), so a tick re-renders that cell only.
- Strike rows are React.memo(strike, ceToken, peToken); key = strike, never
  the array index.
- Queue incoming messages and apply them once per requestAnimationFrame.
- Price flash (green/red) via a CSS class toggle with a 300 ms animation, no
  extra React state.
- Remove any JSON.parse + setState of the whole chain per message.
Display rules: null ltp/bid/ask/iv -> "–"; ltp_stale -> grey LTP; never show
spot (or any fallback) in place of a missing option price.
Acceptance: React Profiler shows only changed cells re-rendering; tick ->
screen p95 < 300 ms with perf logging on; Network tab shows < 20 KB/s.
```

### F4. Dashboard indices: one stream, no polling, no logout on load

```
Dashboard / header indices freshness.
1. Find why /v1/logout fires on page load and why /api/market/indices/stream
   and the option-chain stream close ~3.4 s after load (likely a token
   refresh race or a stale-token check on mount). Market-data streams need
   no auth: they must not depend on the session, and a transient 401 from
   any request must not log the user out.
2. One shared indices stream (module-level store) used by the dashboard,
   explore page, header ticker and terminal header. Remove the 10 s
   setInterval polling of /api/market/indices. Use the REST call only for a
   first paint, or as a fallback when the stream has been down > 5 s, with
   exponential-backoff reconnect.
3. Show "updated Xs ago" next to Live Prices from indices[].as_of.
Acceptance: NIFTY updates within ~250 ms of a change; no
/api/market/indices polling in the Network tab while the stream is healthy;
no /v1/logout on load; the stream stays open > 10 minutes.
```

### F5. Option-chain display correctness

```
Audit every place the option chain, positions and order ticket display an
option price. Remove any fallback that substitutes another value for a
missing price (ltp || spot, ltp ?? underlying, etc.). Rules: null
ltp/bid/ask/iv -> "–"; ltp_stale === true -> grey LTP and show bid/ask;
never display 0 for a missing price. Add unit tests for the formatting
helpers with null, 0, stale and normal values.
Acceptance: no option row ever shows the index value; illiquid strikes show
"–" for LTP/IV.
```

## How to verify

- Backend numbers, any time during market hours:
  `PERF_BASE_URL=https://api.primepiptrade.com python scripts/perf_check.py`
  (in the backend repo). It prints PASS/FAIL for first frame, lag, KB/s,
  msgs/s and REST p95.
- Browser: DevTools → Network → the stream request → EventStream tab shows
  each message; with `localStorage.perf='1'` the console shows the F1 numbers.
- Server live view: `GET /api/internal/latency` (logged-in user) shows
  messages/s, bytes/s and server-side lag per stream.

Questions about the contract: see `api/optionChain.py` and
`service/optionChain/ChainBroadcaster.py` in the backend repo.
