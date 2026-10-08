"""
Fan-out of one live option chain to its SSE clients.

One flush task per (underlying, expiry) wakes every CHAIN_FLUSH_SECS, drains
the cache's changes (OptionChainCache.take_changes) and, only if something
changed, builds each frame kind ONCE and puts the same bytes on every
subscriber's queue. Cost per flush is independent of the tick rate, and
serialization is independent of the number of clients.

Two wire formats (?format= on the stream endpoint):
  full  (default, what the current frontend reads): the same envelope as
        before - {symbol, exchange, expiry, spot, strikes, errors,
        last_updated} plus seq/srv_ts - sent at most once per flush instead
        of once per tick.
  delta: {"t":"s", seq, sym, exch, exp, spot, srv_ts, rows:[[strike,
        ce_token, pe_token, ce|null, pe|null], ...]} on connect, then
        {"t":"d", seq, spot, srv_ts, u:[[token, {changed fields}], ...]}.
        seq increases by one per delta; a client that sees a gap reconnects.

Slow clients: each subscriber has a small bounded queue. When it is full the
queue is emptied - a delta subscriber is then sent a fresh snapshot on the
next flush, a full subscriber just gets the newest full frame - so one slow
browser can never delay the others or grow memory.

Only used from the event loop thread.
"""

import asyncio
import logging
import os
import time
from datetime import datetime

from utils.fastjson import json_encoder
from utils.market_hours import IST_OFFSET
from utils.streamMetrics import stream_metrics

logger = logging.getLogger(__name__)


def _flush_secs_from_env() -> float:
    try:
        millis = float(os.getenv("CHAIN_FLUSH_MS", "250"))
    except ValueError:
        millis = 250.0
    return min(max(millis, 50.0), 2000.0) / 1000.0


CHAIN_FLUSH_SECS = _flush_secs_from_env()
SUBSCRIBER_QUEUE_SIZE = 8
FORMAT_FULL = "full"
FORMAT_DELTA = "delta"
STREAM_FORMATS = (FORMAT_FULL, FORMAT_DELTA)
METRIC_STREAM = "optionchain"


class ChainSubscriber:

    def __init__(self, fmt: str, queue_size: int = SUBSCRIBER_QUEUE_SIZE):
        self.fmt = fmt
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_size)
        self.needs_snapshot = True
        self.dropped = 0


class ChainBroadcaster:

    def __init__(self, cache, flush_secs: float | None = None, encoder=json_encoder,
                 metrics=stream_metrics, clock=time.monotonic):
        self._cache = cache
        self._flush_secs = flush_secs if flush_secs is not None else CHAIN_FLUSH_SECS
        self._encoder = encoder
        self._metrics = metrics
        self._clock = clock
        self._subscribers: set[ChainSubscriber] = set()
        self._seq = 0
        self._task: asyncio.Task | None = None
        self._failure_logged = False

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, fmt: str) -> ChainSubscriber:
        """Registers a client and queues its first frame right away (when
        the chain has data), so it never waits for the next flush."""
        subscriber = ChainSubscriber(fmt if fmt in STREAM_FORMATS else FORMAT_FULL)
        self._subscribers.add(subscriber)
        self._metrics.subscriber_added(METRIC_STREAM)
        frame = self._snapshot_frame(subscriber.fmt)
        if frame is not None:
            subscriber.needs_snapshot = False
            self._offer(subscriber, frame)
        self._ensure_running()
        return subscriber

    def unsubscribe(self, subscriber: ChainSubscriber) -> None:
        if subscriber in self._subscribers:
            self._subscribers.discard(subscriber)
            self._metrics.subscriber_removed(METRIC_STREAM)

    def resync(self, subscriber: ChainSubscriber) -> None:
        """Send this client a full snapshot on the next flush (e.g. after
        the broker session came back)."""
        subscriber.needs_snapshot = True

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name=f"chain-flush-{self._cache.underlying}-{self._cache.expiry}"
            )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_secs)
            try:
                self.flush()
            except Exception as exc:
                # Never let one bad flush end the chain's stream.
                if not self._failure_logged:
                    self._failure_logged = True
                    logger.warning(f"[ChainBroadcaster] flush failed for {self._cache.underlying} "
                                   f"{self._cache.expiry} (further failures at debug): {exc!r}", exc_info=True)
                else:
                    logger.debug(f"[ChainBroadcaster] flush failed: {exc!r}")

    def flush(self) -> None:
        """One flush: drain the cache's changes and deliver each frame kind,
        built at most once, to every subscriber that needs it."""
        updates, spot_changed, oldest = [], False, None
        if self._cache.has_pending_changes():
            updates, spot_changed, oldest = self._cache.take_changes()
        if not self._subscribers:
            return
        changed = bool(updates) or spot_changed
        if changed:
            self._seq += 1

        frames: dict[str, bytes | None] = {}
        for subscriber in list(self._subscribers):
            if subscriber.needs_snapshot:
                # A full-format frame is always the whole chain, so its
                # snapshot and its update are the same bytes.
                kind = FORMAT_FULL if subscriber.fmt == FORMAT_FULL else "snapshot_delta"
                if kind not in frames:
                    frames[kind] = self._snapshot_frame(subscriber.fmt)
                if frames[kind] is None:
                    continue
                subscriber.needs_snapshot = False
                self._offer(subscriber, frames[kind])
            elif changed:
                kind = subscriber.fmt
                if kind not in frames:
                    frames[kind] = self._delta_frame(updates) if kind == FORMAT_DELTA else self._snapshot_frame(FORMAT_FULL)
                if frames[kind] is not None:
                    self._offer(subscriber, frames[kind])

        if changed and oldest is not None:
            self._metrics.record_recv_to_send(METRIC_STREAM, (self._clock() - oldest) * 1000.0)

    def _offer(self, subscriber: ChainSubscriber, frame: bytes) -> None:
        try:
            subscriber.queue.put_nowait(frame)
        except asyncio.QueueFull:
            subscriber.dropped += 1
            while not subscriber.queue.empty():
                subscriber.queue.get_nowait()
            if subscriber.fmt == FORMAT_DELTA:
                # Its deltas now have a gap: start it over from a snapshot.
                subscriber.needs_snapshot = True
                return
            subscriber.queue.put_nowait(frame)
        self._metrics.record_send(METRIC_STREAM, len(frame))

    def _srv_ts(self) -> int:
        return int(time.time() * 1000)

    def _snapshot_frame(self, fmt: str) -> bytes | None:
        cache = self._cache
        if fmt == FORMAT_DELTA:
            if cache.get() is None:
                return None
            return self._encoder.sse_data({
                "t": "s", "seq": self._seq, "sym": cache.underlying, "exch": cache.exchange,
                "exp": cache.expiry, "spot": cache.spot, "srv_ts": self._srv_ts(),
                "rows": cache.snapshot_rows(),
            })
        snapshot = cache.get()
        if snapshot is None:
            return None
        return self._encoder.sse_data({
            "symbol": cache.underlying,
            "exchange": cache.exchange,
            "expiry": cache.expiry,
            "spot": snapshot["spot"],
            "strikes": snapshot["strikes"],
            "errors": [],
            "last_updated": datetime.now(IST_OFFSET).isoformat(timespec="milliseconds"),
            "seq": self._seq,
            "srv_ts": self._srv_ts(),
        })

    def _delta_frame(self, updates: list) -> bytes:
        return self._encoder.sse_data({
            "t": "d", "seq": self._seq, "spot": self._cache.spot, "srv_ts": self._srv_ts(), "u": updates,
        })
