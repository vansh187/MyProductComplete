"""
PositionServicePositionReader - the only PositionReader implementation
today. Mirrors PositionService.apply_fill()'s own cache-first, DB-fallback
lookup strategy so a pre-trade closing-trade check sees the same view of
"current position" that the fill path itself would.
"""

import logging
from decimal import Decimal
from typing import Optional

from database.positionCache import PositionCache
from database.positionPersistence import PositionPersistence
from service.marginengine.interfaces import PositionReader

logger = logging.getLogger(__name__)

_POSITION_TUPLE_COLUMNS = (
    "user_id", "tsym", "netqty", "netavgprc", "buyqty", "sellqty",
    "buyavgprc", "sellavgprc", "realized_pnl", "status",
)


class PositionServicePositionReader(PositionReader):
    """Read-only net-position lookup used to detect opening vs closing trades."""

    def __init__(self):
        self.position_cache = PositionCache()
        self.position_persistence = PositionPersistence()
        self.logger = logger

    def get_net_position(self, user_id: int, tsym: str, cursor=None) -> Optional[dict]:
        if user_id is None or user_id <= 0:
            raise ValueError("User ID must be a positive integer")
        if not tsym or not tsym.strip():
            raise ValueError("tsym cannot be empty")

        tsym = tsym.strip().upper()
        try:
            cached = self.position_cache.get_position(user_id, tsym)
            if cached is not None:
                return {"netqty": Decimal(str(cached.get("netqty", 0))), "status": cached.get("status")}

            if cursor is None:
                return None

            row = self.position_persistence.get_position(user_id, tsym, cursor)
            if row is None:
                return None
            position = dict(zip(_POSITION_TUPLE_COLUMNS, row))
            return {"netqty": Decimal(str(position["netqty"])), "status": position["status"]}
        except Exception as ex:
            self.logger.error(f"Error reading net position for user {user_id}, tsym {tsym}: {str(ex)}")
            raise Exception(f"Error reading net position for user {user_id}, tsym {tsym}: {str(ex)}") from ex
