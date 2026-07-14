"""
Position Cache - Redis-backed live view of open positions.

Design:
  - Postgres is written twice in a position's life: the fill that first
    opens it (full or partial - status=OPEN) and the fill that closes it
    (status=CLOSED, final sell-side columns). Every fill in between, and
    every live-tick price update, touches ONLY this Redis cache - that's
    what saves the database call on the hot path (see
    service/positionService.py for the open/continue/close branching).
  - Per-user open positions are stored in a Redis hash `positions:{user_id}`,
    field = tsym, value = JSON-encoded position dict. This lets the frontend
    read a user's whole position book with a single HGETALL.
  - Every open position is also indexed into a per-instrument watch set
    `position_watch:{exchange}|{token}` (member = "user_id:tsym") so a
    single incoming tick can fan out to every user holding that instrument
    without scanning all positions in the system.
  - Fill-path methods (get/save/remove/lock) are synchronous, because
    apply_fill() runs inside a plain psycopg2 transaction on FastAPI's sync
    threadpool (see PostgresConnectionFactory / executionEngine.py) - no
    asyncio event loop is available there. apply_tick() is async because it
    is invoked from the live WS feed, which already runs on the app's
    asyncio loop (see marketengine/ShoonyaOptionFeed.py).
"""

import json
import logging
import math
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from database.redisConnection import RedisConnection

logger = logging.getLogger(__name__)

POSITION_LOCK_TIMEOUT_SECS = 15  # TTL: comfortably above the critical section's worst case (one DB upsert on close)
TICK_LOCK_WAIT_SECS = 0.5  # best-effort: skip this tick for a position rather than block the shared event loop

CACHE_SCHEMA_VERSION = 1


def _to_jsonable(value: Any) -> Any:
    """Numeric/date types that don't round-trip through json natively -
    Decimal (Postgres NUMERIC columns, and this module's own PnL math) and
    date (contract expiry) - are normalized to plain JSON types so cached
    positions carry real numbers (e.g. "strike": 24700) instead of stringified
    ones."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _serialize_position(position: Dict[str, Any]) -> str:
    return json.dumps({k: _to_jsonable(v) for k, v in position.items()})


class PositionCache:
    """Redis-backed cache for open positions."""

    def __init__(self):
        self.client = RedisConnection.get_sync_connection()
        self.logger = logger

    # -- key helpers -----------------------------------------------------

    @staticmethod
    def _positions_key(user_id: int) -> str:
        return f"positions:{user_id}"

    @staticmethod
    def _watch_key(exchange: Optional[str], token: Optional[str]) -> Optional[str]:
        if not exchange or not token:
            return None
        # Upper-cased so this always matches apply_tick()'s own normalization
        # of the feed's instrument_key below - a case mismatch between the
        # two would silently split one instrument's tick fan-out into two
        # untouched watch-sets instead of raising anything.
        return f"position_watch:{str(exchange).strip().upper()}|{str(token).strip()}"

    # -- concurrency -------------------------------------------------------

    def lock(self, user_id: int, tsym: str):
        """
        Distributed lock guarding the read-modify-write of one user's
        position. Fills used to serialize on Postgres's `SELECT ... FOR
        UPDATE` row lock (a plain, non-NOWAIT lock that just blocks the
        second transaction until the first commits); now that open positions
        live only in Redis between fills, this replaces that role so two
        near-simultaneous fills on the same (user_id, tsym) can't clobber
        each other - with the same "just wait" semantics:

          - blocking_timeout=None: keep waiting for the lock rather than
            raising on contention, matching the old FOR UPDATE's behavior.
            This is still bounded in practice by `timeout` below (once the
            holder's TTL expires, the lock frees up and a waiter acquires it).
          - raise_on_release_error=False: if the critical section runs long
            enough that this lock's own TTL expires before release(), the
            work has already completed by then - a release failure at that
            point is Redis-lock housekeeping, not a failed fill, and must
            never bubble up and roll back an otherwise-successful trade.
        """
        return self.client.lock(
            f"lock:position:{user_id}:{tsym}",
            timeout=POSITION_LOCK_TIMEOUT_SECS,
            blocking_timeout=None,
            raise_on_release_error=False,
        )

    # -- fill-path (sync) --------------------------------------------------

    def get_position(self, user_id: int, tsym: str) -> Optional[Dict[str, Any]]:
        raw = self.client.hget(self._positions_key(user_id), tsym)
        return json.loads(raw) if raw else None

    def get_all_positions(self, user_id: int) -> List[Dict[str, Any]]:
        raw_map = self.client.hgetall(self._positions_key(user_id)) or {}
        return [json.loads(raw) for raw in raw_map.values()]

    def save_open_position(self, position: Dict[str, Any]) -> None:
        """Writes/refreshes one user's open position into the cache and
        (re)indexes it under its instrument's watch set for tick fan-out."""
        user_id = position.get("user_id")
        tsym = position.get("tsym")
        if not user_id or not tsym:
            raise ValueError("Position must include user_id and tsym")

        payload = _serialize_position(position)
        watch_key = self._watch_key(position.get("exchange"), position.get("token"))

        pipe = self.client.pipeline()
        pipe.hset(self._positions_key(user_id), tsym, payload)
        if watch_key:
            pipe.sadd(watch_key, f"{user_id}:{tsym}")
        pipe.execute()

    def remove_position(self, user_id: int, tsym: str,
                         exchange: Optional[str] = None, token: Optional[str] = None) -> None:
        """Evicts a closed position from the cache and its watch set."""
        watch_key = self._watch_key(exchange, token)

        pipe = self.client.pipeline()
        pipe.hdel(self._positions_key(user_id), tsym)
        if watch_key:
            pipe.srem(watch_key, f"{user_id}:{tsym}")
        pipe.execute()

    # -- tick-path (async) --------------------------------------------------

    async def apply_tick(self, instrument_key: str, ltp: float) -> int:
        """
        Refreshes lp/unrealized_pnl/total_pnl for every open position on this
        instrument (across all users), in real time as ticks arrive. Every
        other field (qty/avg-price/realized_pnl/etc.) is untouched - only a
        fill changes those. Returns the number of positions updated.

        Never raises: a tick is inherently best-effort and near-continuous -
        if this one fails for any reason (a transient Redis hiccup, a burst
        of concurrent ticks briefly exhausting the connection pool), the next
        tick arrives well under a second later and simply catches up. Callers
        (e.g. service/positionTickService.py) also guard their own call for
        defense in depth, but apply_tick's own contract must hold regardless
        of who's calling it.
        """
        if not isinstance(ltp, (int, float)) or not math.isfinite(ltp):
            # A malformed/glitched feed message (NaN, +-Infinity) would
            # otherwise poison the cached JSON: Python's json.dumps happily
            # encodes NaN/Infinity as bare (non-standard) tokens, which a
            # strict frontend JSON.parse then throws on for every subsequent
            # read of that position, cache-wide - reject at the source.
            logger.warning(f"[PositionCache] Rejecting non-finite tick ltp={ltp!r} for {instrument_key}")
            return 0

        # Same normalization _watch_key() applies when a fill indexes a
        # position - built from the same "EXCH|TOKEN" shape ShoonyaOptionFeed
        # constructs (see marketengine/ShoonyaOptionFeed.py), so a tick's
        # watch-set lookup always lands on the exact key a fill wrote to.
        exchange, _, token = instrument_key.partition("|")
        watch_key = self._watch_key(exchange, token)
        if watch_key is None:
            return 0

        try:
            redis_client = await RedisConnection.createRedisConnection()
            members = await redis_client.smembers(watch_key)
        except Exception as e:
            logger.warning(f"[PositionCache] apply_tick lookup failed for {instrument_key}: {e}")
            return 0

        if not members:
            return 0

        ltp_decimal = Decimal(str(ltp))
        now_ts = int(time.time())
        updated = 0

        for member in members:
            user_id, _, tsym = member.partition(":")
            if not tsym:
                continue

            positions_key = f"positions:{user_id}"

            # Same per-(user_id, tsym) lock apply_fill() uses. Without this, a
            # tick's read-modify-write can interleave with a concurrent
            # fill's: the tick reads the pre-fill qty/avg-price, the fill
            # commits its new state, and the tick's write (a full HSET of the
            # whole JSON blob) then silently reverts it back to stale values.
            # In a live market this isn't a rare edge case - ticks arrive
            # near-continuously while fills land in between them. Best-effort
            # (short wait, skip on contention) rather than blocking: if a
            # fill is actively holding the lock, the very next tick (typically
            # well under a second later) will pick up the post-fill state.
            lock = redis_client.lock(
                f"lock:position:{user_id}:{tsym}",
                timeout=POSITION_LOCK_TIMEOUT_SECS,
                blocking_timeout=TICK_LOCK_WAIT_SECS,
                raise_on_release_error=False,
            )
            acquired = False
            try:
                acquired = await lock.acquire(blocking=True)
                if not acquired:
                    continue  # a fill is in flight for this position - next tick will catch up

                raw = await redis_client.hget(positions_key, tsym)
                if raw is None:
                    # Stale watch-set entry (position closed/evicted without
                    # this index being cleaned up) - drop it so future ticks
                    # stop paying the lookup cost for it.
                    await redis_client.srem(watch_key, member)
                    continue

                position = json.loads(raw)
                netqty = Decimal(str(position.get("netqty", 0)))
                netavgprc = Decimal(str(position.get("netavgprc", 0)))
                realized_pnl = Decimal(str(position.get("realized_pnl", 0)))

                unrealized_pnl = (ltp_decimal - netavgprc) * netqty

                position["lp"] = float(ltp_decimal)
                position["unrealized_pnl"] = float(unrealized_pnl)
                position["total_pnl"] = float(realized_pnl + unrealized_pnl)
                position["last_tick_ts"] = now_ts
                position["updated_at"] = now_ts

                await redis_client.hset(positions_key, tsym, json.dumps(position))
                updated += 1
            except Exception as e:
                logger.warning(f"[PositionCache] Failed to apply tick to {member}: {e}")
            finally:
                if acquired:
                    try:
                        await lock.release()
                    except Exception:
                        pass

        return updated
