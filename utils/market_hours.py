from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_OPEN_MINUTES = 9 * 60 + 15
_CLOSE_MINUTES = 15 * 60 + 30


def is_market_open(now: datetime | None = None) -> bool:
    """NSE cash/F&O continuous session, weekdays 09:15-15:30 IST (no holiday calendar)."""
    now = now.astimezone(IST) if now is not None else datetime.now(IST)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return _OPEN_MINUTES <= minutes <= _CLOSE_MINUTES
