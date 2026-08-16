import asyncio
import time

from productdto.mutualFundDto import NavPointDTO

_DEFAULT_TTL_SECONDS = 600  # 10 minutes - short enough that "live-first" stays meaningfully live,
                            # long enough to dedupe a user flipping between 1M/6M/1Y tabs.


class _CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value, ttl_seconds: float):
        self.value = value
        self.expires_at = time.monotonic() + ttl_seconds


class MFInMemoryCache:
    """
    Self-contained, in-process, short-TTL read-through cache fronting the
    live-first NAV chart path (mutualfunds/service.py). Deliberately not
    Redis - this is a pure latency shim over an already-durable Postgres
    fallback, not a source of correctness, so per-process memory is enough.
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._entries: dict[int, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get_nav_series(self, scheme_code: int) -> list[NavPointDTO] | None:
        async with self._lock:
            entry = self._entries.get(scheme_code)
            if entry is None:
                return None
            if entry.expires_at < time.monotonic():
                # Self-clean on read so a scheme that's viewed once and never
                # again doesn't sit in memory forever after expiry - the
                # periodic evict_expired() sweep (see scheduler.py) still
                # catches entries that are set but never read again.
                del self._entries[scheme_code]
                return None
            return entry.value

    async def set_nav_series(self, scheme_code: int, points: list[NavPointDTO]) -> None:
        async with self._lock:
            self._entries[scheme_code] = _CacheEntry(points, self._ttl_seconds)

    async def evict_expired(self) -> None:
        async with self._lock:
            now = time.monotonic()
            expired = [code for code, entry in self._entries.items() if entry.expires_at < now]
            for code in expired:
                del self._entries[code]
