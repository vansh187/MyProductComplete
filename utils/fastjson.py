"""
JSON encoding for hot market-data paths (SSE frames serialized once per
flush and shared by every subscriber).

Uses orjson when installed (several times faster than the stdlib and emits
bytes directly); falls back to the stdlib json so a server that has not yet
installed the new requirement keeps working. Both produce compact output
and accept the same inputs used here (dicts/lists of str, int, float, bool,
None).
"""

import json
import math

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - depends on the environment
    _orjson = None


def _finite_or_none(value):
    """NaN/inf are not valid JSON; orjson writes them as null, so the
    stdlib fallback must do the same."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite_or_none(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_or_none(item) for item in value]
    return value


class JsonEncoder:

    def __init__(self, use_orjson: bool | None = None):
        self._orjson = _orjson if (use_orjson is None or use_orjson) else None

    @property
    def backend(self) -> str:
        return "orjson" if self._orjson is not None else "json"

    def dumps(self, payload) -> bytes:
        if self._orjson is not None:
            return self._orjson.dumps(payload)
        return json.dumps(_finite_or_none(payload), separators=(",", ":")).encode("utf-8")

    def sse_data(self, payload) -> bytes:
        """One complete SSE 'data:' frame."""
        return b"data: " + self.dumps(payload) + b"\n\n"


json_encoder = JsonEncoder()
