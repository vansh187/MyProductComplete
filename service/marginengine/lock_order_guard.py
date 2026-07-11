"""
LockOrderGuard - enforces the mandated lock-acquisition order across the
margin engine: the Redis position lock (PositionCache.lock) must always be
acquired before the Postgres wallet row lock (FOR UPDATE via
MarginWalletPersistence). Violating this order is exactly the shape of bug
that causes a cross-process deadlock once a second call site acquires the
same two locks in the opposite order under load.

Usage is a plain instance per call site (this is deliberately NOT a
process-wide singleton/static tracker - each `with` block owns its own
guard instance and only tracks its own lock acquisitions):

    guard = LockOrderGuard()
    with position_cache.lock(user_id, tsym):
        guard.record_position_lock_acquired()
        ...
        guard.record_wallet_lock_acquired()  # raises if position lock wasn't recorded first
"""

import logging

from service.marginengine.exceptions import LockOrderViolationError

logger = logging.getLogger(__name__)


class LockOrderGuard:
    """Tracks and asserts the position-lock-before-wallet-lock ordering rule for one call site."""

    def __init__(self):
        self._position_lock_acquired = False
        self._wallet_lock_acquired = False
        self.logger = logger

    def record_position_lock_acquired(self) -> None:
        self._position_lock_acquired = True

    def record_wallet_lock_acquired(self) -> None:
        if not self._position_lock_acquired:
            message = "Wallet lock acquired before position lock - lock-ordering rule violated"
            self.logger.error(message)
            raise LockOrderViolationError(message)
        self._wallet_lock_acquired = True
