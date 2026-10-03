"""
Pooled, keep-alive HTTP client for the Shoonya REST API.

NorenRestApiPy calls the bare module-level `requests.post(url, data=...)`
for every REST call: no timeout (a hung broker socket pins an executor
thread forever - asyncio.wait_for only abandons the await, not the thread)
and no Session (every call pays a fresh TCP + TLS handshake, ~50-150ms).

NorenApi resolves the name `requests` from its own module globals at call
time, so install() swaps that one module attribute for an instance of this
class. Every NorenApi REST call then goes through one shared Session with a
connection pool sized to the broker executor and a default timeout. Only
`post`/`get` are overridden; any other attribute (requests.exceptions etc.)
is delegated to the real requests module, so library code that touches
anything else keeps working unchanged.

Retries are deliberately disabled: NorenApi also places/modifies/cancels
orders through this client, and a POST must never be silently re-sent.
"""

import logging

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECS = 3.05
DEFAULT_READ_TIMEOUT_SECS = 10.0
DEFAULT_POOL_SIZE = 32


class BrokerHttpClient:

    def __init__(self, connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECS,
                 read_timeout: float = DEFAULT_READ_TIMEOUT_SECS,
                 pool_size: int = DEFAULT_POOL_SIZE,
                 session_factory=requests.Session,
                 requests_module=requests):
        self._timeout = (connect_timeout, read_timeout)
        self._requests_module = requests_module
        self._session = session_factory()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=pool_size, max_retries=0, pool_block=False)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    @property
    def timeout(self) -> tuple[float, float]:
        return self._timeout

    def post(self, url, data=None, json=None, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return self._session.post(url, data=data, json=json, **kwargs)

    def get(self, url, params=None, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return self._session.get(url, params=params, **kwargs)

    def install(self, module) -> bool:
        """Points `module.requests` at this client. Returns False (and leaves
        the module untouched) if it has no `requests` attribute to replace,
        e.g. a future library version that changed its HTTP layer."""
        try:
            if module is None or not hasattr(module, "requests"):
                logger.warning("[BrokerHttp] %r has no 'requests' attribute - pooled client not installed", module)
                return False
            if getattr(module, "requests") is not self:
                setattr(module, "requests", self)
                logger.info("[BrokerHttp] Pooled session installed (timeout=%s, retries=0)", self._timeout)
            return True
        except Exception as exc:
            logger.warning("[BrokerHttp] Failed to install pooled client: %s", exc)
            return False

    def close(self) -> None:
        try:
            self._session.close()
        except Exception as exc:
            logger.warning("[BrokerHttp] Session close failed: %s", exc)

    def __getattr__(self, name):
        # Only reached for attributes not defined on this instance/class:
        # requests.exceptions, requests.codes, etc.
        if name.startswith("__") or name in ("_requests_module", "_session", "_timeout"):
            raise AttributeError(name)
        return getattr(self._requests_module, name)
