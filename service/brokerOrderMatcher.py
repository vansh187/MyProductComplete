"""
Decides whether a row from the master account's broker order book (or a
broker order-update push) is the broker side of a given internal order.

client_order_id travels to the broker as `remarks`. Our orders table keeps
it globally unique (orders_client_order_id_key), but remarks is free text
the broker never validates, and the master account's order book holds every
user's orders plus anything placed on the account outside this app. A
remarks match alone could therefore pair an order with the wrong broker
order, so the contract (either tradingsymbol convention), side and quantity
must agree too, and an ambiguous match is never guessed at.

Deliberately instance-based (no static/class methods or class-level state).
"""

import logging
from typing import Any, Dict, List, Optional

from appconfig import OptionMaster

logger = logging.getLogger(__name__)


class BrokerOrderMatcher:

    def __init__(self):
        self._broker_sides = {"B": "BUY", "S": "SELL"}

    def _value(self, field) -> str:
        return str(field.value if hasattr(field, "value") else field or "").upper()

    def _symbols_for(self, symbol: str) -> set:
        symbols = {symbol.upper().strip()}
        symbols.update(alias.upper() for alias in OptionMaster.find_tsym_aliases(symbol))
        return symbols

    def matches(self, order_row: Dict[str, Any], broker_row: Dict[str, Any]) -> bool:
        try:
            broker_tsym = str(broker_row.get("tsym") or "").upper().strip()
            broker_side = self._broker_sides.get(str(broker_row.get("trantype") or "").upper())
            broker_qty = int(float(broker_row.get("qty")))
            return (
                bool(broker_tsym)
                and broker_tsym in self._symbols_for(str(order_row.get("symbol") or ""))
                and broker_side == self._value(order_row.get("side"))
                and broker_qty == int(order_row.get("quantity"))
            )
        except (TypeError, ValueError):
            return False

    def pick_order(self, order_rows: List[Dict[str, Any]], broker_row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The single internal order this broker row belongs to, or None when
        none or several match."""
        try:
            candidates = [row for row in order_rows if self.matches(row, broker_row)]
            if len(candidates) > 1:
                logger.error(
                    f"[BrokerOrderMatcher] {len(candidates)} internal orders match broker order "
                    f"{broker_row.get('norenordno')} (remarks={broker_row.get('remarks')!r}) - not linking any"
                )
            return candidates[0] if len(candidates) == 1 else None
        except Exception as exc:
            logger.error(f"[BrokerOrderMatcher] pick_order failed: {exc}")
            return None
