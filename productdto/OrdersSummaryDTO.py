from dataclasses import dataclass

@dataclass
class OrdersSummary:
    total: int
    pending: int
    executed: int
    partially_executed: int
    cancelled: int
    failed: int = 0   # optional default