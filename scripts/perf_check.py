"""
Market-data performance check - run any time during market hours.

    python scripts/perf_check.py                       # against http://127.0.0.1:8000
    PERF_BASE_URL=https://api.primepiptrade.com python scripts/perf_check.py

Env vars (never hard-code secrets):
    PERF_BASE_URL     default http://127.0.0.1:8000
    PERF_AUTH_TOKEN   optional bearer token; adds /api/internal/latency to the report
    PERF_SECONDS      stream sample length, default 30
    PERF_UNDERLYING   default nifty

Measures:
  - option chain stream (format=delta and format=full): time to first data
    frame, msgs/s, bytes/s, seq gaps, srv_ts -> received lag p50/p95
  - indices stream: time to first frame, msgs/s
  - REST p50/p95 for /indices, /top-movers, /sectors (10 calls each)
and prints PASS/FAIL against the targets in docs/PERF.md.

Lag uses the server's srv_ts against this machine's clock: run it ON the
server (127.0.0.1) for exact numbers; remotely the clock skew estimated from
the HTTP Date header (1 s resolution) is printed alongside.
"""

import asyncio
import json
import os
import statistics
import sys
import time
from email.utils import parsedate_to_datetime

import httpx

BASE_URL = os.getenv("PERF_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
AUTH_TOKEN = os.getenv("PERF_AUTH_TOKEN", "")
SECONDS = float(os.getenv("PERF_SECONDS", "30"))
UNDERLYING = os.getenv("PERF_UNDERLYING", "nifty")

TARGETS = {
    "chain_first_frame_ms": 300.0,
    "chain_lag_p95_ms": 300.0,
    "chain_delta_bytes_per_sec": 20_000.0,
    "chain_msgs_per_sec": 4.5,
    "rest_p95_ms": 100.0,
}


class StreamSample:

    def __init__(self, name: str):
        self.name = name
        self.first_frame_ms: float | None = None
        self.messages = 0
        self.bytes = 0
        self.lags_ms: list[float] = []
        self.seq_gaps = 0
        self.error = None
        self._last_seq = None

    def observe(self, payload: dict, started: float) -> None:
        now = time.time()
        if payload.get("errors") and not payload.get("strikes") and payload.get("t") != "s":
            return  # a status frame (connecting / broker down), not data
        if payload.get("t") == "e":
            return
        if self.first_frame_ms is None:
            self.first_frame_ms = (time.perf_counter() - started) * 1000.0
        srv_ts = payload.get("srv_ts")
        if isinstance(srv_ts, (int, float)):
            self.lags_ms.append(now * 1000.0 - srv_ts)
        seq = payload.get("seq")
        if payload.get("t") == "d" and isinstance(seq, int) and self._last_seq is not None and seq != self._last_seq + 1:
            self.seq_gaps += 1
        if isinstance(seq, int):
            self._last_seq = seq


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


async def sample_stream(client: httpx.AsyncClient, name: str, path: str) -> StreamSample:
    sample = StreamSample(name)
    started = time.perf_counter()
    deadline = started + SECONDS
    try:
        async with client.stream("GET", f"{BASE_URL}{path}", timeout=httpx.Timeout(10.0, read=SECONDS + 10)) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if time.perf_counter() >= deadline:
                    break
                if not line.startswith("data: "):
                    continue
                sample.messages += 1
                sample.bytes += len(line) + 2
                try:
                    sample.observe(json.loads(line[6:]), started)
                except ValueError:
                    pass
    except Exception as exc:
        sample.error = repr(exc)
    return sample


async def time_rest(client: httpx.AsyncClient, path: str, calls: int = 10) -> list[float]:
    durations = []
    for _ in range(calls):
        started = time.perf_counter()
        response = await client.get(f"{BASE_URL}{path}", timeout=15.0)
        durations.append((time.perf_counter() - started) * 1000.0)
        response.raise_for_status()
    return durations


async def clock_skew_ms(client: httpx.AsyncClient) -> float | None:
    try:
        response = await client.get(f"{BASE_URL}/healthz", timeout=5.0)
        server = parsedate_to_datetime(response.headers["date"]).timestamp()
        return (server - time.time()) * 1000.0
    except Exception:
        return None


def verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


async def main() -> int:
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"} if AUTH_TOKEN else {}
    async with httpx.AsyncClient(headers=headers) as client:
        skew = await clock_skew_ms(client)
        print(f"Target {BASE_URL} | sampling streams for {SECONDS:.0f}s | clock skew (server - local) "
              f"~{skew:.0f} ms (1 s resolution)" if skew is not None else f"Target {BASE_URL}")

        chain = f"/api/market/{UNDERLYING}/optionchain/stream"
        delta, full, indices = await asyncio.gather(
            sample_stream(client, "chain delta", f"{chain}?format=delta"),
            sample_stream(client, "chain full", f"{chain}?format=full"),
            sample_stream(client, "indices", "/api/market/indices/stream"),
        )

        failures = 0
        print("\nStreams")
        for sample in (delta, full, indices):
            if sample.error:
                print(f"  {sample.name:12} ERROR {sample.error}")
                failures += 1
                continue
            rate = sample.messages / SECONDS
            print(f"  {sample.name:12} first frame {sample.first_frame_ms or float('nan'):7.0f} ms | "
                  f"{rate:5.2f} msgs/s | {sample.bytes / SECONDS / 1024:7.1f} KB/s | "
                  f"lag p50 {_percentile(sample.lags_ms, 0.5) or float('nan'):6.0f} ms "
                  f"p95 {_percentile(sample.lags_ms, 0.95) or float('nan'):6.0f} ms | seq gaps {sample.seq_gaps}")

        print("\nREST (10 calls each)")
        rest = {}
        for path in ("/api/market/indices", "/api/market/top-movers", "/api/market/sectors"):
            try:
                durations = await time_rest(client, path)
                rest[path] = durations
                print(f"  {path:24} p50 {statistics.median(durations):6.1f} ms  p95 {_percentile(durations, 0.95):6.1f} ms")
            except Exception as exc:
                print(f"  {path:24} ERROR {exc!r}")
                failures += 1

        if AUTH_TOKEN:
            try:
                response = await client.get(f"{BASE_URL}/api/internal/latency", timeout=5.0)
                print("\nServer-side /api/internal/latency")
                print(json.dumps(response.json(), indent=2))
            except Exception as exc:
                print(f"\n/api/internal/latency ERROR {exc!r}")

    print("\nChecks")
    checks = []
    if not delta.error:
        checks += [
            ("chain first frame < 300 ms", (delta.first_frame_ms or 1e9) < TARGETS["chain_first_frame_ms"]),
            ("chain lag p95 < 300 ms", (_percentile(delta.lags_ms, 0.95) or 1e9) < TARGETS["chain_lag_p95_ms"]),
            ("chain delta < 20 KB/s", delta.bytes / SECONDS < TARGETS["chain_delta_bytes_per_sec"]),
            ("chain <= 4 msgs/s", delta.messages / SECONDS <= TARGETS["chain_msgs_per_sec"]),
            ("chain no seq gaps", delta.seq_gaps == 0),
        ]
    for path, durations in rest.items():
        checks.append((f"{path} p95 < 100 ms", _percentile(durations, 0.95) < TARGETS["rest_p95_ms"]))
    for label, ok in checks:
        print(f"  {verdict(ok)}  {label}")
        failures += 0 if ok else 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
