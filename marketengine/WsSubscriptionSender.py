"""
Sends WebSocket subscribe/unsubscribe frames on one dedicated daemon thread.

NorenApi's subscribe()/unsubscribe() write to the socket under a mutex and,
in the stock library, busy-wait (`while not connected: sleep(0.05)`) with no
bound while the socket is down. Callers such as ShoonyaStockFeed.touch() run
on the asyncio event loop, so a direct call during a feed outage froze the
whole app. submit() only enqueues and returns immediately; the frame is
written here, in FIFO order (so a subscribe followed by an unsubscribe of
the same token is applied in that order).

Duck-typed contract (anything with submit(label, send) -> bool and close()
can replace this, e.g. an inline sender in tests).
"""

import logging
import queue
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_MAX_PENDING = 1000
SLOW_SEND_MS = 500.0
CLOSE_JOIN_TIMEOUT_SECS = 1.0


class WsSubscriptionSender:

    def __init__(self, max_pending: int = DEFAULT_MAX_PENDING, thread_name: str = "ws-subscribe",
                 slow_send_ms: float = SLOW_SEND_MS):
        self._queue: queue.Queue = queue.Queue(maxsize=max_pending)
        self._thread_name = thread_name
        self._slow_send_ms = slow_send_ms
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._stop_marker = object()
        self._closed = False

    def submit(self, label: str, send: Callable[[], object]) -> bool:
        """Queues send() for the sender thread. Never blocks. Returns False if
        the sender is closed or the queue is full (the caller's ref-counted
        state is still correct - the feed resubscribes on the next connect)."""
        if self._closed:
            return False
        try:
            self._ensure_thread()
            self._queue.put_nowait((label, send))
            return True
        except queue.Full:
            logger.warning("[WsSender] Queue full (%d pending) - dropped: %s", self._queue.qsize(), label)
            return False
        except Exception as exc:
            logger.warning("[WsSender] Could not queue %s: %s", label, exc)
            return False

    def flush(self, timeout: float = 2.0) -> bool:
        """Blocks until every frame queued before this call has been processed.
        For tests and shutdown only - never call from the event loop."""
        done = threading.Event()
        if not self.submit("flush", done.set):
            return False
        return done.wait(timeout)

    def close(self) -> None:
        self._closed = True
        try:
            self._queue.put_nowait((None, self._stop_marker))
        except queue.Full:
            pass  # daemon thread; it dies with the process
        except Exception as exc:
            logger.warning("[WsSender] Stop signal failed: %s", exc)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=CLOSE_JOIN_TIMEOUT_SECS)

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name=self._thread_name, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                label, send = self._queue.get()
            except Exception as exc:
                logger.warning("[WsSender] Queue read failed: %s", exc)
                continue
            if send is self._stop_marker:
                return
            self._send_one(label, send)

    def _send_one(self, label: str, send: Callable[[], object]) -> None:
        start = time.perf_counter()
        try:
            send()
        except Exception as exc:
            logger.warning("[WsSender] %s failed: %s", label, exc)
            return
        elapsed_ms = (time.perf_counter() - start) * 1000
        if elapsed_ms >= self._slow_send_ms:
            logger.warning("[WsSender] SLOW %s %.0fms", label, elapsed_ms)
        elif logger.isEnabledFor(logging.DEBUG):
            logger.debug("[WsSender] %s %.1fms", label, elapsed_ms)
