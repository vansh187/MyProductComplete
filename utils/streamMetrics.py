"""
Rolling, in-memory latency/throughput metrics for the market-data streams,
exposed at GET /api/internal/latency.

Per stream name ("optionchain", "indices", ...):
  - msgs/sec and bytes/sec actually written to clients (last WINDOW_SECS)
  - recv->send ms: broker tick received -> frame queued to clients
  - exch->recv ms: exchange feed time -> tick received (second resolution;
    reflects broker + network delay and clock skew)

Only ever touched from the event loop thread (sends happen in the flush
tasks and SSE generators; ticks are recorded after the hop onto the loop),
so no locking. Bounded memory: fixed-size deques.
"""

import time
from collections import deque

WINDOW_SECS = 60.0
MAX_SAMPLES = 2000


class _StreamStats:

    def __init__(self):
        self.sends: deque[tuple[float, int]] = deque(maxlen=MAX_SAMPLES * 10)
        self.recv_to_send_ms: deque[float] = deque(maxlen=MAX_SAMPLES)
        self.exch_to_recv_ms: deque[float] = deque(maxlen=MAX_SAMPLES)
        self.subscribers = 0


class StreamMetrics:

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._streams: dict[str, _StreamStats] = {}

    def _stats(self, stream: str) -> _StreamStats:
        stats = self._streams.get(stream)
        if stats is None:
            stats = _StreamStats()
            self._streams[stream] = stats
        return stats

    def record_send(self, stream: str, nbytes: int) -> None:
        self._stats(stream).sends.append((self._clock(), nbytes))

    def record_recv_to_send(self, stream: str, millis: float) -> None:
        if millis >= 0:
            self._stats(stream).recv_to_send_ms.append(millis)

    def record_exch_to_recv(self, stream: str, millis: float) -> None:
        self._stats(stream).exch_to_recv_ms.append(millis)

    def subscriber_added(self, stream: str) -> None:
        self._stats(stream).subscribers += 1

    def subscriber_removed(self, stream: str) -> None:
        stats = self._stats(stream)
        stats.subscribers = max(0, stats.subscribers - 1)

    def snapshot(self) -> dict:
        now = self._clock()
        return {name: self._summarise(stats, now) for name, stats in self._streams.items()}

    def _summarise(self, stats: _StreamStats, now: float) -> dict:
        recent = [(at, nbytes) for at, nbytes in stats.sends if now - at <= WINDOW_SECS]
        span = WINDOW_SECS
        if recent:
            span = max(1.0, min(WINDOW_SECS, now - recent[0][0]))
        msgs_per_sec = len(recent) / span
        bytes_per_sec = sum(nbytes for _, nbytes in recent) / span
        clients = stats.subscribers
        return {
            "subscribers": clients,
            "msgs_per_sec": round(msgs_per_sec, 2),
            "bytes_per_sec": round(bytes_per_sec),
            "msgs_per_sec_per_client": round(msgs_per_sec / clients, 2) if clients else None,
            "bytes_per_sec_per_client": round(bytes_per_sec / clients) if clients else None,
            "recv_to_send_ms": self._percentiles(stats.recv_to_send_ms),
            "exch_to_recv_ms": self._percentiles(stats.exch_to_recv_ms),
        }

    def _percentiles(self, samples) -> dict:
        if not samples:
            return {"p50": None, "p95": None, "max": None, "samples": 0}
        ordered = sorted(samples)
        return {
            "p50": round(ordered[len(ordered) // 2], 1),
            "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
            "max": round(ordered[-1], 1),
            "samples": len(ordered),
        }


stream_metrics = StreamMetrics()
