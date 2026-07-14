"""
NullBrokerMarginProvider - today's only BrokerMarginProvider implementation.
Always defers to the internal MarginCalculator. This exists purely as the
seam a real Shoonya/Zerodha margin API integration slots into later
(implement get_broker_margin() to return a real MarginResult instead of
None) with zero changes to MarginEngine or any call site.
"""

import logging
from typing import Optional

from service.marginengine.interfaces import BrokerMarginProvider
from service.marginengine.models import MarginContext, MarginResult

logger = logging.getLogger(__name__)


class NullBrokerMarginProvider(BrokerMarginProvider):
    """Stub broker margin provider - always defers to the internal calculator."""

    def __init__(self):
        self.logger = logger

    def get_broker_margin(self, context: MarginContext) -> Optional[MarginResult]:
        try:
            return None
        except Exception as ex:
            self.logger.error(f"NullBrokerMarginProvider unexpected failure: {str(ex)}")
            return None
