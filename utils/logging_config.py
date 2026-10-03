"""
Single logging setup for the whole app (and CLI scripts). Output goes to
stdout, which systemd/journald captures:  journalctl -u backend.service

Levels:
  ERROR    a real failure that needs attention
  WARNING  degraded but still serving (fallback used, timeout, retry)
  INFO     lifecycle: startup, login, feed start, scheduled refreshes, per-request latency
  DEBUG    per-call/per-tick detail (broker call timings, payloads)

LOG_LEVEL env var picks the root level (default INFO). Per-logger overrides
(logger names are module paths, case-sensitive; a parent covers its children):
LOG_LEVELS="latency=WARNING,marketengine.ShoonyaConnection=DEBUG"
  - marketengine.ShoonyaConnection         Shoonya login/quotes + call timings
  - marketengine.ShoonyaConnection.timing  only the per-call timings
"""

import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_NOISY_LIBRARIES = ("urllib3", "websocket", "httpx", "httpcore", "hpack", "selenium", "WDM", "asyncio", "multipart")

_configured = False


def setup_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    root_level = _parse_level(level or os.getenv("LOG_LEVEL", "INFO"), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(root_level)

    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(max(root_level, logging.WARNING))
    # Our latency middleware logs every request with timing and client IP,
    # so uvicorn's own per-request access line is redundant.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    for item in os.getenv("LOG_LEVELS", "").split(","):
        name, _, value = item.partition("=")
        if name.strip() and value.strip():
            logging.getLogger(name.strip()).setLevel(_parse_level(value, root_level))


def _parse_level(value: str, default: int) -> int:
    resolved = logging.getLevelName(str(value).strip().upper())
    return resolved if isinstance(resolved, int) else default
