"""
Which tokens the recorder subscribes to (spec section 9):
  - Nifty 50, Nifty Bank, India VIX index ticks
  - NIFTY / BANKNIFTY futures, current + next month (ticks); front month also depth
  - NIFTY / BANKNIFTY options, 2 nearest expiries, ATM +/- N strikes, CE + PE

Expiries, strikes, tokens and lot sizes come from the daily-refreshed scrip
masters (appconfig/OptionMaster.py, appconfig/FutureMaster.py), never
hardcoded. The option window re-centres only when the ATM strike has moved
by >= recenter_after_strikes from the current centre (no churn on noise).

A broken master entry costs only that contract / expiry / underlying: each
is loaded in its own try, so the rest of the day's plan still works.
"""

import logging
from datetime import date, datetime

from appconfig import FutureMaster, OptionMaster
from appconfig.ScripMasterRefresher import upcoming_expiry_dates
from utils.safe_numbers import safe_float

logger = logging.getLogger(__name__)

DEFAULT_TICK_SIZE = 0.05


def _fno_exchange(underlying: str) -> str:
    """Sensex F&O trades on BFO; NSE index F&O on NFO."""
    return "BFO" if underlying.upper() in ("SENSEX", "BANKEX") else "NFO"


class SubscriptionPlanner:

    def __init__(self, config, option_master=OptionMaster, future_master=FutureMaster):
        self._config = config
        self._options = option_master
        self._futures = future_master
        # "EXCH|TOKEN" -> instrument description (instruments snapshot / chain rows)
        self.instruments: dict[str, dict] = {}
        self.front_futures: dict[str, str] = {}               # underlying -> future key
        self._ladders: dict[tuple[str, str], list[tuple[float, dict]]] = {}   # (underlying, expiry) -> sorted strikes
        self._centres: dict[tuple[str, str], int] = {}
        self.index_key_by_underlying = {name: key for key, name in config.index_tokens.items()}
        self.load_errors: list[str] = []

    # -- daily setup ---------------------------------------------------------

    def start_day(self, now: datetime) -> None:
        """Loads today's contracts from the masters. Call once per day,
        before the first plan(). Never raises: what fails to load is listed
        in load_errors and left out of the plan."""
        self.instruments.clear()
        self.front_futures.clear()
        self._ladders.clear()
        self._centres.clear()
        self.load_errors = []

        for key, name in self._config.index_tokens.items():
            try:
                exchange, token = key.split("|", 1)
                self.instruments[key] = {
                    "exchange": exchange, "token": token, "tsym": name, "underlying": name,
                    "instrument_type": "INDEX", "expiry": None, "strike": None, "opt_type": None,
                    "lot_size": None, "tick_size": None,
                }
            except Exception as exc:
                self._load_error(f"index {key}", exc)

        for underlying in self._config.underlyings:
            self._load_futures(underlying, now)
            self._load_options(underlying, now)

        logger.info(
            f"[Recorder] Day plan: {len(self.instruments)} instruments, futures {sorted(self.front_futures)}, "
            f"option chains {sorted(self._ladders)}" + (f", load errors {self.load_errors}" if self.load_errors else "")
        )

    def _load_error(self, what: str, exc: Exception) -> None:
        message = f"{what}: {exc!r}"
        self.load_errors.append(message[:300])
        logger.error(f"[Recorder] Could not load {message}")

    def _load_futures(self, underlying: str, now: datetime) -> None:
        exchange = _fno_exchange(underlying)
        try:
            future_expiries = upcoming_expiry_dates(self._futures.get_expiries(underlying), now=now)
        except Exception as exc:
            self._load_error(f"{underlying} futures expiries", exc)
            return
        for position, expiry in enumerate(future_expiries[: self._config.future_months]):
            try:
                contract = self._futures.get_contract(underlying, expiry)
                if not contract or not contract.get("token"):
                    continue
                key = f"{exchange}|{contract['token']}"
                self.instruments[key] = {
                    "exchange": exchange, "token": str(contract["token"]), "tsym": contract.get("tsym") or "",
                    "underlying": underlying, "instrument_type": "FUT", "expiry": date.fromisoformat(expiry),
                    "strike": None, "opt_type": None, "lot_size": contract.get("lot_size"),
                    "tick_size": DEFAULT_TICK_SIZE,
                }
                if position == 0:
                    self.front_futures[underlying] = key
            except Exception as exc:
                self._load_error(f"{underlying} future {expiry}", exc)

    def _load_options(self, underlying: str, now: datetime) -> None:
        exchange = _fno_exchange(underlying)
        try:
            option_expiries = self._options.upcoming_expiries(underlying, now=now)[: self._config.option_expiries]
        except Exception as exc:
            self._load_error(f"{underlying} option expiries", exc)
            return
        for expiry in option_expiries:
            try:
                expiry_date = date.fromisoformat(expiry)
                ladder = []
                for strike, info in self._options.get_strike_chain(underlying, expiry).items():
                    strike_value = safe_float(strike)
                    if strike_value is None or not isinstance(info, dict):
                        continue
                    ladder.append((strike_value, info))
                    for opt_type in ("CE", "PE"):
                        token = info.get(f"{opt_type.lower()}_token")
                        if not token:
                            continue
                        self.instruments[f"{exchange}|{token}"] = {
                            "exchange": exchange, "token": str(token), "tsym": info.get(f"{opt_type.lower()}_tsym") or "",
                            "underlying": underlying, "instrument_type": "OPT", "expiry": expiry_date,
                            "strike": strike_value, "opt_type": opt_type, "lot_size": info.get("lot_size"),
                            "tick_size": info.get("tick_size") or DEFAULT_TICK_SIZE,
                        }
                ladder.sort(key=lambda item: item[0])
                if ladder:
                    self._ladders[(underlying, expiry)] = ladder
            except Exception as exc:
                self._load_error(f"{underlying} options {expiry}", exc)

    def tsym(self, key: str) -> str | None:
        try:
            info = self.instruments.get(key)
            return info["tsym"] if info and info.get("tsym") else None
        except Exception:
            return None

    def option_keys(self) -> set[str]:
        try:
            return {key for key, info in self.instruments.items() if info["instrument_type"] == "OPT"}
        except Exception:
            return set()

    # -- what to subscribe now -------------------------------------------------

    def plan(self, spot_for) -> tuple[set[str], set[str]]:
        """(touchline keys, depth keys) wanted right now. spot_for(underlying)
        -> latest underlying price or None; an option chain is left out
        until its underlying has a price. Never raises."""
        touchline = set(self._config.index_tokens)
        depth: set[str] = set()
        try:
            touchline |= {key for key, info in self.instruments.items() if info["instrument_type"] == "FUT"}
            depth = set(self.front_futures.values())
        except Exception as exc:
            logger.warning(f"[Recorder] Futures plan failed: {exc!r}")

        for (underlying, expiry), ladder in list(self._ladders.items()):
            try:
                spot = spot_for(underlying)
                if spot is None:
                    continue
                atm = min(range(len(ladder)), key=lambda i: abs(ladder[i][0] - spot))
                centre = self._centres.get((underlying, expiry))
                if centre is None or abs(atm - centre) >= self._config.recenter_after_strikes:
                    if centre is not None:
                        logger.info(f"[Recorder] Re-centring {underlying} {expiry}: {ladder[centre][0]} -> {ladder[atm][0]}")
                    centre = atm
                    self._centres[(underlying, expiry)] = centre
                low = max(0, centre - self._config.strikes_each_side)
                high = min(len(ladder), centre + self._config.strikes_each_side + 1)
                exchange = _fno_exchange(underlying)
                for _, info in ladder[low:high]:
                    for opt_type in ("ce", "pe"):
                        token = info.get(f"{opt_type}_token")
                        if token:
                            touchline.add(f"{exchange}|{token}")
            except Exception as exc:
                logger.warning(f"[Recorder] Option plan for {underlying} {expiry} failed: {exc!r}")
        return touchline, depth
