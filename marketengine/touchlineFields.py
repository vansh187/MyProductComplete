"""
Shared field parsing for Shoonya equity touchline data (bid/ask depth,
upper/lower circuit limits) - used by BOTH the REST quote snapshot
(marketengine/ShoonyaConnection.get_stock_quote) and the WebSocket tick
cache (marketengine/ShoonyaStockFeed) so the two paths can never drift out
of sync on field names or defaulting rules (e.g. a future circuit-limit
field name Shoonya adds only having to be taught to one of the two copies).

REST snapshots and WS frames need different depth semantics though:
  - REST (full_depth): always exactly 5 zero-filled levels per side - a
    single snapshot is expected to be complete.
  - WS (depth_delta): only the levels/fields actually present in that one
    frame - per Shoonya's own docs a 'tf' update only carries fields that
    changed, so a partial frame must never be treated as "the other levels
    are now zero"; the caller merges this delta into previously-cached
    state instead of replacing it (see ShoonyaStockFeed.ingest_raw_tick).

Deliberately instance-based (no static/class methods).
"""


class TouchlineFieldParser:

    def __init__(self, level_count: int = 5):
        self._level_count = level_count

    def safe_float(self, value, default=None):
        try:
            return float(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def safe_int(self, value, default=None):
        try:
            return int(float(value)) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def circuit_limits(self, raw: dict) -> dict:
        """Returns only the keys actually present in this frame (checking
        both known field-name variants) - callers decide their own default
        for an absent key (a REST snapshot defaults to 0.0, a WS delta
        simply omits the key so a merge leaves the previous value alone)."""
        result = {}
        for key in ("uc", "ucl"):
            if raw.get(key) not in (None, ""):
                result["upper_circuit"] = self.safe_float(raw.get(key))
                break
        for key in ("lc", "lcl"):
            if raw.get(key) not in (None, ""):
                result["lower_circuit"] = self.safe_float(raw.get(key))
                break
        return result

    def full_depth(self, raw: dict) -> dict:
        """REST snapshot: always self._level_count zero-filled levels each side."""
        bids = []
        asks = []
        for level in range(1, self._level_count + 1):
            bids.append({
                "price": self.safe_float(raw.get(f"bp{level}"), 0.0),
                "qty": self.safe_int(raw.get(f"bq{level}"), 0),
                "orders": self.safe_int(raw.get(f"bo{level}"), 0),
            })
            asks.append({
                "price": self.safe_float(raw.get(f"sp{level}"), 0.0),
                "qty": self.safe_int(raw.get(f"sq{level}"), 0),
                "orders": self.safe_int(raw.get(f"so{level}"), 0),
            })
        return {"bids": bids, "asks": asks}

    def depth_delta(self, raw: dict) -> dict | None:
        """WS partial frame: only levels/fields actually present, keyed by
        1-based level index -> {field: value}, for the caller to merge into
        previously-cached depth. Returns None if this frame carries no depth
        fields at all (most 'tf' frames don't - only lp/v/etc change)."""
        bids: dict[int, dict] = {}
        asks: dict[int, dict] = {}
        for level in range(1, self._level_count + 1):
            bid_fields = {}
            if f"bp{level}" in raw:
                bid_fields["price"] = self.safe_float(raw.get(f"bp{level}"), 0.0)
            if f"bq{level}" in raw:
                bid_fields["qty"] = self.safe_int(raw.get(f"bq{level}"), 0)
            if f"bo{level}" in raw:
                bid_fields["orders"] = self.safe_int(raw.get(f"bo{level}"), 0)
            if bid_fields:
                bids[level] = bid_fields

            ask_fields = {}
            if f"sp{level}" in raw:
                ask_fields["price"] = self.safe_float(raw.get(f"sp{level}"), 0.0)
            if f"sq{level}" in raw:
                ask_fields["qty"] = self.safe_int(raw.get(f"sq{level}"), 0)
            if f"so{level}" in raw:
                ask_fields["orders"] = self.safe_int(raw.get(f"so{level}"), 0)
            if ask_fields:
                asks[level] = ask_fields

        if not bids and not asks:
            return None
        return {"bids": bids, "asks": asks}

    def empty_depth(self) -> dict:
        """A fresh zero-filled depth structure, used to seed the cache the
        first time a token's depth_delta arrives with no prior state."""
        return {
            "bids": [{"price": 0.0, "qty": 0, "orders": 0} for _ in range(self._level_count)],
            "asks": [{"price": 0.0, "qty": 0, "orders": 0} for _ in range(self._level_count)],
        }

    def apply_depth_delta(self, depth: dict, delta: dict) -> dict:
        """Merges a depth_delta() result into a full depth dict (as returned
        by empty_depth()/full_depth()) in place, only overwriting the
        specific fields present in the delta, and returns it."""
        for level, fields in delta.get("bids", {}).items():
            if 1 <= level <= len(depth["bids"]):
                depth["bids"][level - 1].update(fields)
        for level, fields in delta.get("asks", {}).items():
            if 1 <= level <= len(depth["asks"]):
                depth["asks"][level - 1].update(fields)
        return depth
