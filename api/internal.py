"""
Internal diagnostics. Admin-only (same rule as /admin/shoonya: open to any
logged-in user while ADMIN_USER_IDS is empty - see require_admin).
"""

from fastapi import APIRouter, Depends, Request

from utils.auth_dependency import require_admin
from utils.fastjson import json_encoder
from utils.streamMetrics import stream_metrics

router = APIRouter(prefix="/api/internal", tags=["Internal"], dependencies=[Depends(require_admin)])


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
    """
    feed = getattr(request.app.state, "option_feed", None)
    return {
        "streams": stream_metrics.snapshot(),
        "json_backend": json_encoder.backend,
        "market_feed_connected": bool(getattr(feed, "is_connected", False)),
    }
