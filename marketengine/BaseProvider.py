from abc import ABC, abstractmethod
from typing import List, Callable, Dict, Any

class BaseMarketProvider(ABC):
    """Abstract base class to guarantee structure across market feeds"""
    
    @abstractmethod
    async def connect(self):
        """Initialize the connection client"""
        pass

    @abstractmethod
    async def subscribe(self, symbols: List[str]):
        """Subscribe to specific watchlists"""
        pass

    @abstractmethod
    def on_tick(self, callback: Callable[[Dict[str, Any]], None]):
        """Register the method that processes incoming data"""
        pass