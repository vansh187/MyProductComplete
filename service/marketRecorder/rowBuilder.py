"""
Turns raw Shoonya frames into complete `ticks` / `depth` rows.

Every frame that carries anything beyond its envelope (t/e/tk) becomes one
row - nothing the broker sends is dropped:
  - known fields go to their own columns, forward-filled: Shoonya sends only
    the fields that changed ('tf'/'df' updates), so the last known value of
    every field is kept per token and each row is complete. A field never
    received for a token stays None. Prices of 0 mean "none" (no trade /
    empty book level) and become None.
  - any other field of the frame goes, exactly as sent, into the `extra`
    JSON column of that row (not forward-filled).
The exchange feed time ('ft', epoch seconds) belongs to its frame only and
is never forward-filled.

Runs only on the recorder's worker thread - not thread-safe by design.
"""

import json
import logging

from utils.safe_numbers import safe_float, safe_int

logger = logging.getLogger(__name__)

_NS = 1_000_000_000

# Frame envelope: type, exchange, token. 'ts' (name) and 'ft' (feed time)
# have their own columns.
_ENVELOPE = frozenset({"t", "e", "tk"})
_OWN_COLUMNS = frozenset({"ts", "ft"})

# Fields both feeds can carry -> (column, kind)
_COMMON_FIELDS = {
    "pc": ("pct_change", "number"), "poi": ("prev_oi", "int"), "toi": ("total_oi", "int"),
    "ltt": ("last_trade_time", "text"), "uc": ("upper_circuit", "price"), "lc": ("lower_circuit", "price"),
    "52h": ("high_52w", "price"), "52l": ("low_52w", "price"),
    "tbq": ("total_buy_qty", "int"), "tsq": ("total_sell_qty", "int"),
}

# Shoonya touchline field -> (ticks column, kind)
_TICK_FIELDS = {
    "lp": ("ltp", "price"), "ltq": ("ltq", "int"), "v": ("volume", "int"), "oi": ("oi", "int"),
    "ap": ("atp", "price"),
    "bp1": ("bid", "price"), "bq1": ("bid_qty", "int"), "sp1": ("ask", "price"), "sq1": ("ask_qty", "int"),
    "o": ("day_open", "price"), "h": ("day_high", "price"), "l": ("day_low", "price"), "c": ("prev_close", "price"),
    **_COMMON_FIELDS,
    "bo1": ("bid_orders", "int"), "so1": ("ask_orders", "int"),
}
_TICK_COLUMNS = [column for column, _ in _TICK_FIELDS.values()]
_INDEX_NULL_COLUMNS = ("bid", "bid_qty", "ask", "ask_qty", "bid_orders", "ask_orders")

_DEPTH_FIELDS: dict[str, tuple[str, str]] = {"lp": ("ltp", "price"), "v": ("volume", "int")}
for _level in range(1, 6):
    _DEPTH_FIELDS[f"bp{_level}"] = (f"bid_px_{_level}", "price")
    _DEPTH_FIELDS[f"bq{_level}"] = (f"bid_qty_{_level}", "int")
    _DEPTH_FIELDS[f"bo{_level}"] = (f"bid_orders_{_level}", "int")
    _DEPTH_FIELDS[f"sp{_level}"] = (f"ask_px_{_level}", "price")
    _DEPTH_FIELDS[f"sq{_level}"] = (f"ask_qty_{_level}", "int")
    _DEPTH_FIELDS[f"so{_level}"] = (f"ask_orders_{_level}", "int")
_DEPTH_FIELDS.update({
    "ltq": ("ltq", "int"), "ap": ("atp", "price"), "oi": ("oi", "int"),
    "o": ("day_open", "price"), "h": ("day_high", "price"), "l": ("day_low", "price"), "c": ("prev_close", "price"),
    **_COMMON_FIELDS,
})
_DEPTH_COLUMNS = [column for column, _ in _DEPTH_FIELDS.values()]


def _parse(value, kind: str):
    try:
        if kind == "price":
            price = safe_float(value)
            return price if price is not None and price > 0 else None
        if kind == "number":
            return safe_float(value)          # may be 0 or negative (e.g. % change)
        if kind == "text":
            text = str(value).strip() if value is not None else ""
            return text or None
        return safe_int(value)
    except Exception:
        return None


def _extra(raw: dict, mapped: dict) -> str | None:
    """Every field of the frame that has no column of its own, as sent."""
    try:
        unmapped = {k: v for k, v in raw.items() if k not in _ENVELOPE and k not in _OWN_COLUMNS and k not in mapped}
        return json.dumps(unmapped, separators=(",", ":"), default=str) if unmapped else None
    except Exception:
        return None


def _has_payload(raw: dict) -> bool:
    return any(k not in _ENVELOPE for k in raw)


def _exch_ns(raw: dict) -> int | None:
    try:
        feed_time = safe_int(raw.get("ft"))
        return feed_time * _NS if feed_time and feed_time > 0 else None
    except Exception:
        return None


class RowBuilder:

    def __init__(self, index_tokens: set[str], tsym_lookup=None):
        """index_tokens: 'EXCH|TOKEN' keys of indices (bid/ask always null).
        tsym_lookup(key) -> tsym | None: names tokens whose first ('tk')
        frame - the only one carrying 'ts' - was missed."""
        self._index_tokens = set(index_tokens)
        self._tsym_lookup = tsym_lookup or (lambda key: None)
        self._ticks: dict[str, dict] = {}
        self._tick_recv_ns: dict[str, int] = {}
        self._depth: dict[str, dict] = {}
        self._tsym: dict[str, str] = {}

    def _name(self, key: str, token: str, raw: dict) -> str:
        try:
            if raw.get("ts"):
                self._tsym[key] = str(raw["ts"])
            name = self._tsym.get(key)
            if name is None:
                name = self._tsym_lookup(key) or token
                self._tsym[key] = name
            return name
        except Exception:
            return token

    def tick_row(self, recv_ns: int, raw: dict) -> dict | None:
        """Full `ticks` row for a 'tk'/'tf' frame; None only for a frame with
        no content beyond its envelope (or a malformed one)."""
        try:
            exchange, token = raw.get("e"), raw.get("tk")
            if not exchange or not token or not _has_payload(raw):
                return None
            key = f"{exchange}|{token}"
            changed = {column: _parse(raw[field], kind) for field, (column, kind) in _TICK_FIELDS.items() if field in raw}
            state = self._ticks.setdefault(key, dict.fromkeys(_TICK_COLUMNS))
            state.update(changed)
            if key in self._index_tokens:
                for column in _INDEX_NULL_COLUMNS:
                    state[column] = None
            self._tick_recv_ns[key] = recv_ns
            return {
                "ts_recv_ns": recv_ns, "ts_exch_ns": _exch_ns(raw),
                "exchange": str(exchange), "token": str(token), "tsym": self._name(key, str(token), raw),
                **state,
                "extra": _extra(raw, _TICK_FIELDS),
            }
        except Exception as exc:
            logger.warning(f"[Recorder] Bad touchline frame skipped ({exc!r}): {str(raw)[:200]}")
            return None

    def depth_row(self, recv_ns: int, raw: dict) -> dict | None:
        """Full `depth` row for a 'dk'/'df' frame; None only for a frame with
        no content beyond its envelope (or a malformed one)."""
        try:
            exchange, token = raw.get("e"), raw.get("tk")
            if not exchange or not token or not _has_payload(raw):
                return None
            key = f"{exchange}|{token}"
            changed = {column: _parse(raw[field], kind) for field, (column, kind) in _DEPTH_FIELDS.items() if field in raw}
            state = self._depth.setdefault(key, dict.fromkeys(_DEPTH_COLUMNS))
            state.update(changed)
            return {
                "ts_recv_ns": recv_ns, "ts_exch_ns": _exch_ns(raw),
                "exchange": str(exchange), "token": str(token), "tsym": self._name(key, str(token), raw),
                **state,
                "extra": _extra(raw, _DEPTH_FIELDS),
            }
        except Exception as exc:
            logger.warning(f"[Recorder] Bad depth frame skipped ({exc!r}): {str(raw)[:200]}")
            return None

    def latest(self, key: str) -> tuple[dict, int] | None:
        """(last known tick fields, receive time ns) for a token, or None."""
        try:
            state = self._ticks.get(key)
            if state is None:
                return None
            return state, self._tick_recv_ns[key]
        except Exception:
            return None

    def ltp(self, key: str) -> float | None:
        try:
            state = self._ticks.get(key)
            return state.get("ltp") if state else None
        except Exception:
            return None

    def reset_day(self) -> None:
        """Day change: prices and names start from scratch."""
        try:
            self._ticks.clear()
            self._tick_recv_ns.clear()
            self._depth.clear()
            self._tsym.clear()
        except Exception as exc:
            logger.warning(f"[Recorder] reset_day failed: {exc!r}")
