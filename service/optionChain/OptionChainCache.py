"""
Per-(underlying, expiry) shared cache of live option-chain data.

Ticks are merged in O(1) and only record WHICH fields changed (the dirty
map); nothing is serialized or recomputed per tick. The chain's broadcaster
(service/optionChain/ChainBroadcaster.py) drains those changes on a fixed
flush interval via take_changes(), so a burst of ticks costs one message per
flush instead of one full-chain message per tick per client.

IV is the expensive part, so it is recomputed only at read/flush time
(_refresh_iv), only for legs whose price inputs changed, plus every leg at
most once per IV_FULL_REFRESH_SECS when spot moves. IV comes from the
bid/ask mid when the quote is two-sided and tight, otherwise from a price
that traded today, otherwise it is None - never from a stale LTP.

generation/wait_for_next() remain for one-shot readers and tests.
"""

import asyncio
import logging
import time
from datetime import datetime

from service.optionChain import blackScholes
from utils.market_hours import IST_OFFSET as IST, MARKET_CLOSE_TIME as MARKET_CLOSE
from utils.safe_numbers import safe_float

logger = logging.getLogger(__name__)

DEFAULT_TICK_SIZE = 0.05
# A quote whose spread is wider than this fraction of its mid is too loose to
# derive an IV from (illiquid strikes showed IV ~197%).
IV_MAX_SPREAD_RATIO = 0.20
# Spot moves on nearly every tick; re-deriving every leg's IV on each one is
# the costly part, so a spot move refreshes all legs at most this often.
IV_FULL_REFRESH_SECS = 1.0
# Fields whose change makes a leg's IV stale.
_IV_INPUTS = frozenset({"ltp", "bid", "ask", "volume"})
# Stored and sent along with a change, but never a change by themselves.
_METADATA_FIELDS = frozenset({"exch_ts"})


class OptionChainCache:

    def __init__(self, underlying: str, expiry: str, strike_chain: dict[str, dict], exchange: str = "NFO",
                 rate: float = 0.065, clock=time.monotonic):
        self.underlying = underlying
        self.expiry = expiry
        self.exchange = exchange
        self._rate = rate
        self._clock = clock
        self._expiry_date = self._parse_expiry(expiry)
        self._lot_size_by_strike: dict[str, int] = {}
        self._token_to_strike_leg: dict[str, tuple[str, str]] = {}
        # "strike:leg" -> static contract fields, copied into the leg's live
        # dict once when it is first created, so every snapshot carries them
        # with no per-snapshot merge cost.
        self._leg_meta: dict[str, dict] = {}
        for strike, info in strike_chain.items():
            if not isinstance(info, dict) or safe_float(strike) is None:
                continue  # one malformed strike row must not break the whole chain
            lot_size = info.get("lot_size")
            tick_size = info.get("tick_size") or DEFAULT_TICK_SIZE
            self._lot_size_by_strike[strike] = lot_size
            for leg in ("ce", "pe"):
                token = info.get(f"{leg}_token")
                if not token:
                    continue
                self._token_to_strike_leg[f"{exchange}|{token}"] = (strike, leg)
                self._leg_meta[f"{strike}:{leg}"] = {
                    "tsym": info.get(f"{leg}_tsym"),
                    "token": str(token),
                    "lot_size": lot_size,
                    "tick_size": tick_size,
                }
        # Sorted once here - the snapshot is rebuilt on every tick.
        self._strike_order = self._ordered_strike_keys()

        self._legs: dict[str, dict] = {}       # "strike:leg" -> {tsym, token, lot_size, tick_size, ltp, bid, ask, oi, oi_change, volume, iv, ts}
        self._oi_baseline: dict[str, int] = {}  # "strike:leg" -> reference OI for oi_change
        self._spot: float | None = None
        self._generation = 0
        self._condition = asyncio.Condition()
        self._last_valid_data: dict | None = None
        # A systematic per-tick failure would otherwise log on every tick.
        self._merge_failure_logged = False

        # Changes since the last take_changes(): "strike:leg" -> field names.
        self._dirty: dict[str, set[str]] = {}
        self._spot_changed = False
        # Monotonic time the oldest not-yet-flushed change arrived (for the
        # recv->send latency metric).
        self._oldest_pending_at: float | None = None
        self._iv_dirty: set[str] = set()
        self._iv_spot_stale = False
        self._last_full_iv_at = float("-inf")

    def tokens(self) -> set[str]:
        """All 'EXCH|TOKEN' instrument keys this cache needs subscribed."""
        return set(self._token_to_strike_leg.keys())

    def _parse_expiry(self, expiry: str):
        """Parsed once here instead of on every tick's IV recompute."""
        try:
            return datetime.strptime(expiry, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    def _now_iso(self) -> str:
        """Last-update time, e.g. '2026-10-06T10:15:30.123+05:30'."""
        return datetime.now(IST).isoformat(timespec="milliseconds")

    def _leg_for_update(self, key: str) -> dict:
        leg_data = self._legs.get(key)
        if leg_data is None:
            leg_data = dict(self._leg_meta.get(key, ()))
            self._legs[key] = leg_data
        return leg_data

    def _time_to_expiry_years(self) -> float:
        expiry_date = self._expiry_date
        if expiry_date is None:
            raise ValueError(f"Invalid expiry: {self.expiry}")
        now = datetime.now(IST)
        if expiry_date > now.date():
            return (expiry_date - now.date()).days / 365.0
        if expiry_date == now.date():
            close = datetime.combine(expiry_date, MARKET_CLOSE, tzinfo=IST)
            remaining_seconds = max((close - now).total_seconds(), 0.0)
            return remaining_seconds / (365.0 * 86400.0)
        return 0.0  # already expired

    def _iv_price(self, leg_data: dict) -> float | None:
        """The price an IV may be derived from: the bid/ask mid of a tight
        two-sided quote; with no two-sided quote (e.g. after the close) the
        LTP only if it traded today; otherwise None."""
        bid = leg_data.get("bid")
        ask = leg_data.get("ask")
        if bid and ask and bid > 0 and ask >= bid:
            mid = (bid + ask) / 2.0
            return mid if (ask - bid) / mid <= IV_MAX_SPREAD_RATIO else None
        ltp = leg_data.get("ltp")
        volume = leg_data.get("volume")
        if ltp and ltp > 0 and volume and volume > 0:
            return ltp
        return None

    def _refresh_iv(self) -> None:
        """Recomputes IV for legs whose inputs changed, and for every leg (at
        most once per IV_FULL_REFRESH_SECS) after spot moved. Never raises:
        a failure (e.g. a malformed expiry) leaves IV as None."""
        keys = self._iv_dirty
        if self._iv_spot_stale and (self._clock() - self._last_full_iv_at) >= IV_FULL_REFRESH_SECS:
            keys = keys | set(self._legs)
            self._iv_spot_stale = False
            self._last_full_iv_at = self._clock()
        if not keys:
            return
        self._iv_dirty = set()

        try:
            t_years = self._time_to_expiry_years()
        except Exception as exc:
            logger.debug(f"[OptionChainCache] IV not computed for {self.underlying} {self.expiry}: {exc}")
            t_years = None

        for key in keys:
            leg_data = self._legs.get(key)
            if leg_data is None:
                continue
            iv = None
            price = self._iv_price(leg_data)
            if t_years is not None and price is not None and self._spot is not None:
                strike, leg = key.split(":")
                try:
                    iv = blackScholes.implied_volatility(
                        price, self._spot, float(strike), t_years, leg.upper(), self._rate
                    )
                except Exception as exc:
                    logger.debug(f"[OptionChainCache] IV not computed for {self.underlying} {self.expiry} {key}: {exc}")
            if "iv" not in leg_data or leg_data["iv"] != iv:
                leg_data["iv"] = iv
                self._dirty.setdefault(key, set()).add("iv")

    def set_spot(self, spot: float | None) -> None:
        if spot is None or spot <= 0 or spot == self._spot:
            return
        self._spot = spot
        self._spot_changed = True
        self._iv_spot_stale = True
        self._mark_pending()

    @property
    def spot(self) -> float | None:
        return self._spot

    def _mark_pending(self) -> None:
        if self._oldest_pending_at is None:
            self._oldest_pending_at = self._clock()

    def _merge(self, strike: str, leg: str, fields: dict, only_missing: bool = False) -> None:
        """Merges one REST seed or WS tick into the leg and records which
        fields actually changed. only_missing (REST seeds): never overwrite
        a value a live tick already set - the seed is the older data."""
        key = f"{strike}:{leg}"
        if only_missing:
            existing = self._legs.get(key) or {}
            fields = {name: value for name, value in fields.items() if existing.get(name) is None}
        if not fields:
            return
        leg_data = self._leg_for_update(key)

        changed = set()
        for name, value in fields.items():
            if name not in leg_data or leg_data[name] != value:
                leg_data[name] = value
                changed.add(name)

        oi = fields.get("oi")
        if oi is not None:
            if key not in self._oi_baseline:
                # Previous-day OI when the broker sends one. Shoonya sends an
                # empty "poi" for some contracts - that must not become a
                # None baseline, or every later oi_change would fail.
                previous_oi = fields.get("poi")
                self._oi_baseline[key] = previous_oi if previous_oi is not None else oi
            oi_change = oi - self._oi_baseline[key]
            if leg_data.get("oi_change") != oi_change:
                leg_data["oi_change"] = oi_change
                changed.add("oi_change")

        # No trade today (day volume 0): the LTP is a previous session's.
        volume = leg_data.get("volume")
        ltp_stale = volume == 0 and leg_data.get("ltp") is not None
        if leg_data.get("ltp_stale") != ltp_stale:
            leg_data["ltp_stale"] = ltp_stale
            changed.add("ltp_stale")

        # The exchange time stamp travels with a real change but is not one.
        changed -= _METADATA_FIELDS
        if not changed:
            return
        leg_data["ts"] = self._now_iso()
        changed.add("ts")
        changed |= _METADATA_FIELDS & fields.keys()
        self._dirty.setdefault(key, set()).update(changed)
        if changed & _IV_INPUTS:
            self._iv_dirty.add(key)
        self._mark_pending()

    def _merge_logged(self, strike: str, leg: str, fields: dict, only_missing: bool = False) -> None:
        """_merge for the feed/seed paths: one bad frame must not stop the
        chain, but the failure is logged (once per cache with traceback,
        then at debug so a systematic problem cannot flood the log)."""
        try:
            self._merge(strike, leg, fields, only_missing)
        except Exception as exc:
            if not self._merge_failure_logged:
                self._merge_failure_logged = True
                logger.warning(
                    f"[OptionChainCache] {self.underlying} {self.expiry} {strike}:{leg} update failed "
                    f"(further failures on this chain log at debug): {exc!r} fields={fields}",
                    exc_info=True,
                )
            else:
                logger.debug(f"[OptionChainCache] {self.underlying} {self.expiry} {strike}:{leg} update failed: {exc!r}")

    def seed_leg(self, strike: str, leg: str, fields: dict) -> None:
        """REST snapshot fill. Seeding may finish after live ticks started
        arriving, so it only fills fields no tick has set yet."""
        self._merge_logged(strike, leg, fields, only_missing=True)

    async def apply_tick(self, instrument_key: str, tick_fields: dict) -> None:
        strike_leg = self._token_to_strike_leg.get(instrument_key)
        if strike_leg is None or not tick_fields:
            return
        strike, leg = strike_leg

        async with self._condition:
            self._merge_logged(strike, leg, tick_fields)
            # Always wake waiters, even after a malformed tick, so a reader
            # never stalls on one bad frame.
            self._generation += 1
            self._condition.notify_all()

    def has_pending_changes(self) -> bool:
        return bool(self._dirty) or self._spot_changed or bool(self._iv_dirty) or self._iv_spot_stale

    def take_changes(self) -> tuple[list[list], bool, float | None]:
        """Drains everything changed since the previous call:
        ([[token, {changed fields}], ...], spot_changed, oldest_change_at).
        IV is brought up to date first so it travels in the same batch."""
        self._refresh_iv()
        updates = []
        for key, names in self._dirty.items():
            leg_data = self._legs.get(key)
            if not names or leg_data is None:
                continue
            updates.append([leg_data.get("token"), {name: leg_data.get(name) for name in names}])
        spot_changed = self._spot_changed
        oldest = self._oldest_pending_at
        self._dirty = {}
        self._spot_changed = False
        self._oldest_pending_at = None
        return updates, spot_changed, oldest

    def snapshot_rows(self) -> list[list]:
        """Compact full state for the delta protocol, every strike in the
        window in order: [[strike, ce_token, pe_token, ce|None, pe|None], ...]."""
        self._refresh_iv()
        rows = []
        for strike_value, ce_key, pe_key in self._strike_order:
            rows.append([
                strike_value,
                self._leg_meta.get(ce_key, {}).get("token"),
                self._leg_meta.get(pe_key, {}).get("token"),
                self._legs.get(ce_key) or None,
                self._legs.get(pe_key) or None,
            ])
        return rows

    def _ordered_strike_keys(self) -> list[tuple[float, str, str]]:
        ordered = [
            (safe_float(strike), f"{strike}:ce", f"{strike}:pe")
            for strike in self._lot_size_by_strike
        ]
        ordered.sort()
        return ordered

    def _build_snapshot(self) -> dict:
        legs = self._legs
        strikes = []
        for strike_value, ce_key, pe_key in self._strike_order:
            ce = legs.get(ce_key)
            pe = legs.get(pe_key)
            if not ce and not pe:
                continue
            strikes.append({
                "strike": strike_value,
                "ce": ce or None,
                "pe": pe or None,
            })
        return {"spot": self._spot, "strikes": strikes}

    def get(self) -> dict | None:
        self._refresh_iv()
        snapshot = self._build_snapshot()
        if snapshot["strikes"]:
            self._last_valid_data = snapshot
            return snapshot
        return self._last_valid_data

    @property
    def generation(self) -> int:
        return self._generation

    async def wait_for_next(self, after_generation: int) -> dict | None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._generation > after_generation)
            return self.get()
