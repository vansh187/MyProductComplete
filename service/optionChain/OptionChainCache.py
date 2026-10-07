"""
Per-(underlying, expiry) shared cache of live option-chain data.

Mirrors service/topMovers/TopMoversService.py's TopMoversCache: an
asyncio.Condition + generation counter so SSE clients wake up exactly when
new ticks land instead of polling on a fixed timer, plus a _last_valid_data
fallback for stale/closed-market display.
"""

import asyncio
import logging
from datetime import datetime

from service.optionChain import blackScholes
from utils.market_hours import IST_OFFSET as IST, MARKET_CLOSE_TIME as MARKET_CLOSE
from utils.safe_numbers import safe_float

logger = logging.getLogger(__name__)

DEFAULT_TICK_SIZE = 0.05


class OptionChainCache:

    def __init__(self, underlying: str, expiry: str, strike_chain: dict[str, dict], exchange: str = "NFO", rate: float = 0.065):
        self.underlying = underlying
        self.expiry = expiry
        self._rate = rate
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

    def _recompute_iv(self, key: str, strike: str, leg: str) -> None:
        leg_data = self._legs.get(key)
        if leg_data is None or self._spot is None or leg_data.get("ltp") is None:
            return
        # Called from inside apply_tick()'s critical section, before the
        # generation bump/notify_all() - a failure here (e.g. a malformed
        # expiry string) must never abort the tick update, or every field
        # this tick legitimately carried (ltp/bid/ask/oi) would be silently
        # dropped along with it, since apply_tick is invoked from a detached
        # asyncio Task with nothing checking its result (see
        # ShoonyaOptionFeed._on_tick).
        try:
            t_years = self._time_to_expiry_years()
            leg_data["iv"] = blackScholes.implied_volatility(
                leg_data["ltp"], self._spot, float(strike), t_years, leg.upper(), self._rate
            )
        except Exception as exc:
            logger.debug(f"[OptionChainCache] IV not computed for {self.underlying} {self.expiry} {key}: {exc}")
            leg_data["iv"] = None

    def set_spot(self, spot: float) -> None:
        self._spot = spot

    def _merge(self, strike: str, leg: str, fields: dict) -> None:
        """Merges one REST seed or WS tick into the leg: fields, last-update
        time, OI change against the reference OI, IV."""
        key = f"{strike}:{leg}"
        leg_data = self._leg_for_update(key)
        leg_data.update(fields)
        leg_data["ts"] = self._now_iso()

        oi = fields.get("oi")
        if oi is not None:
            if key not in self._oi_baseline:
                # Previous-day OI when the broker sends one. Shoonya sends an
                # empty "poi" for some contracts - that must not become a
                # None baseline, or every later oi_change would fail.
                previous_oi = fields.get("poi")
                self._oi_baseline[key] = previous_oi if previous_oi is not None else oi
            leg_data["oi_change"] = oi - self._oi_baseline[key]

        self._recompute_iv(key, strike, leg)

    def _merge_logged(self, strike: str, leg: str, fields: dict) -> None:
        """_merge for the feed/seed paths: one bad frame must not stop the
        chain, but the failure is logged (once per cache with traceback,
        then at debug so a systematic problem cannot flood the log)."""
        try:
            self._merge(strike, leg, fields)
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
        """Initial REST snapshot fill at cache-creation time (before ticks start arriving)."""
        self._merge_logged(strike, leg, fields)

    async def apply_tick(self, instrument_key: str, tick_fields: dict) -> None:
        strike_leg = self._token_to_strike_leg.get(instrument_key)
        if strike_leg is None or not tick_fields:
            return
        strike, leg = strike_leg

        async with self._condition:
            self._merge_logged(strike, leg, tick_fields)
            # Always wake waiters, even after a malformed tick, so an SSE
            # stream never stalls on one bad frame.
            self._generation += 1
            self._condition.notify_all()

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
