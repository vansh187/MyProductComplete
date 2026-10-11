"""
Internal diagnostics. Admin-only (same rule as /admin/shoonya: open to any
logged-in user while ADMIN_USER_IDS is empty - see require_admin).
"""

import asyncio
import logging
import threading

from fastapi import APIRouter, Depends, Request

from api.marketquotes import index_instrument_keys
from api.sectorPerformance import sector_instrument_keys
from utils.auth_dependency import require_admin
from utils.fastjson import json_encoder
from utils.streamMetrics import stream_metrics

router = APIRouter(prefix="/api/internal", tags=["Internal"], dependencies=[Depends(require_admin)])

logger = logging.getLogger(__name__)

RECORDER_STATUS_TIMEOUT_SECS = 3.0


class _StatusRunner:
    """Runs a component's status() (it touches the disk) on its own daemon
    thread - never on the event loop and never on the broker-io executor, so
    polling this endpoint can't take threads from order/quote calls. At most
    one call per component at a time: while one is still running (a slow
    disk), the last result is returned, flagged stale."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running: set[int] = set()
        self._last: dict[int, dict] = {}

    async def status(self, component) -> dict:
        if component is None:
            return {"enabled": False}
        key = id(component)
        try:
            with self._lock:
                if key in self._running:
                    return {**self._last.get(key, {}), "status_stale": "previous check still running"}
                self._running.add(key)
            loop = asyncio.get_running_loop()
            future = loop.create_future()

            def _deliver(result: dict) -> None:
                if not future.done():
                    future.set_result(result)

            def _work() -> None:
                try:
                    result = component.status()
                except Exception as exc:
                    result = {"status_error": repr(exc)[:300]}
                with self._lock:
                    self._running.discard(key)
                    self._last[key] = result
                try:
                    loop.call_soon_threadsafe(_deliver, result)
                except RuntimeError:
                    pass   # event loop closed

            threading.Thread(target=_work, name="diagnostics-status", daemon=True).start()
            return await asyncio.wait_for(asyncio.shield(future), timeout=RECORDER_STATUS_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            return {"status_error": "timed out"}
        except Exception as exc:
            logger.warning(f"[Internal] Recorder status failed: {exc!r}")
            with self._lock:
                self._running.discard(key)
            return {"status_error": repr(exc)[:300]}


_status_runner = _StatusRunner()


async def _recorder_status(component) -> dict:
    """status() of the market data recorder or its uploader, bounded; a
    failure is reported in the payload instead of failing the endpoint."""
    return await _status_runner.status(component)


@router.get("/latency")
async def get_stream_latency(request: Request):
    """
    Rolling (last 60 s) market-data stream metrics, per stream:
      subscribers, msgs_per_sec(_per_client), bytes_per_sec(_per_client),
      recv_to_send_ms {p50, p95, max}: broker tick received -> frame queued,
      exch_to_recv_ms {p50, p95, max}: exchange feed time -> tick received
        (1 s resolution from the exchange time stamp; includes clock skew).

    Targets: optionchain msgs_per_sec_per_client <= 4, bytes_per_sec_per_client
    < 20000 (format=delta), recv_to_send_ms.p95 < 260.

    pinned_ticks: every index and sector token the tiles read, with
    subscriptions (ref count on the broker socket), has_tick, age_secs,
    close_in_tick / known_close (previous close; without one the tile falls
    back to REST) and as_of. Healthy during market hours: subscriptions >= 1,
    age_secs a few seconds, and a close from either field.
    """
    feed = getattr(request.app.state, "option_feed", None)
    stock_feed = getattr(request.app.state, "stock_feed", None)
    tick_health = getattr(stock_feed, "tick_health", None)
    recorder_status, upload_status = await asyncio.gather(
        _recorder_status(getattr(request.app.state, "market_recorder", None)),
        _recorder_status(getattr(request.app.state, "recorder_uploader", None)),
    )
    return {
        "recorder": recorder_status,
        "recorder_upload": upload_status,
        "streams": stream_metrics.snapshot(),
        "json_backend": json_encoder.backend,
        "market_feed_connected": bool(getattr(feed, "is_connected", False)),
        # dict.fromkeys: one row per token (Bank Nifty is both an index and a sector).
        "pinned_ticks": tick_health(list(dict.fromkeys(index_instrument_keys() + sector_instrument_keys())))
        if callable(tick_health) else None,
    }
