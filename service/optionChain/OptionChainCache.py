"""
Per-(underlying, expiry) shared cache of live option-chain data.

Mirrors service/topMovers/TopMoversService.py's TopMoversCache: an
asyncio.Condition + generation counter so SSE clients wake up exactly when
new ticks land instead of polling on a fixed timer, plus a _last_valid_data
fallback for stale/closed-market display.
"""

import asyncio
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from service.optionChain import blackScholes

IST = ZoneInfo("Asia/Kolkata")
MARKET_CLOSE = dtime(15, 30)


class OptionChainCache:

    def __init__(self, underlying: str, expiry: str, strike_chain: dict[str, dict], exchange: str = "NFO", rate: float = 0.065):
        self.underlying = underlying
        self.expiry = expiry
        self._rate = rate
        self._lot_size_by_strike: dict[str, int] = {
            strike: info["lot_size"] for strike, info in strike_chain.items()
        }
        self._token_to_strike_leg: dict[str, tuple[str, str]] = {}
        for strike, info in strike_chain.items():
            if info.get("ce_token"):
                self._token_to_strike_leg[f"{exchange}|{info['ce_token']}"] = (strike, "ce")
            if info.get("pe_token"):
                self._token_to_strike_leg[f"{exchange}|{info['pe_token']}"] = (strike, "pe")

        self._legs: dict[str, dict] = {}       # "strike:leg" -> {ltp, bid, ask, oi, oi_change, volume, iv}
        self._oi_baseline: dict[str, int] = {}  # "strike:leg" -> reference OI for oi_change
        self._spot: float | None = None
        self._generation = 0
        self._condition = asyncio.Condition()
        self._last_valid_data: dict | None = None

    def tokens(self) -> set[str]:
        """All 'EXCH|TOKEN' instrument keys this cache needs subscribed."""
        return set(self._token_to_strike_leg.keys())

    def _time_to_expiry_years(self) -> float:
        expiry_date = datetime.strptime(self.expiry, "%Y-%m-%d").date()
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
        except Exception:
            leg_data["iv"] = None

    def set_spot(self, spot: float) -> None:
        self._spot = spot

    def seed_leg(self, strike: str, leg: str, fields: dict) -> None:
        """Initial REST snapshot fill at cache-creation time (before ticks start arriving)."""
        key = f"{strike}:{leg}"
        leg_data = self._legs.setdefault(key, {})
        leg_data.update(fields)
        if fields.get("oi") is not None and key not in self._oi_baseline:
            self._oi_baseline[key] = fields.get("poi", fields["oi"])
        self._recompute_iv(key, strike, leg)

    async def apply_tick(self, instrument_key: str, tick_fields: dict) -> None:
        strike_leg = self._token_to_strike_leg.get(instrument_key)
        if strike_leg is None or not tick_fields:
            return
        strike, leg = strike_leg
        key = f"{strike}:{leg}"

        async with self._condition:
            leg_data = self._legs.setdefault(key, {})
            leg_data.update(tick_fields)

            if tick_fields.get("oi") is not None:
                if key not in self._oi_baseline:
                    self._oi_baseline[key] = tick_fields.get("poi", tick_fields["oi"])
                leg_data["oi_change"] = tick_fields["oi"] - self._oi_baseline[key]

            self._recompute_iv(key, strike, leg)

            self._generation += 1
            self._condition.notify_all()

    def _build_snapshot(self) -> dict:
        strikes = []
        for strike in sorted(self._lot_size_by_strike.keys(), key=float):
            ce = self._legs.get(f"{strike}:ce")
            pe = self._legs.get(f"{strike}:pe")
            if not ce and not pe:
                continue
            strikes.append({
                "strike": float(strike),
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
