from abc import ABC, abstractmethod
from datetime import date

from productdto.mutualFundDto import NavPointDTO


class NotSupportedError(Exception):
    """Raised by a provider for data it fundamentally cannot supply (e.g. holdings)."""


class MFDataProvider(ABC):
    """
    Abstraction over an external mutual-fund data source. Every method is an
    instance method so multiple providers (mfapi.in today, a paid vendor
    later) can be swapped in via constructor injection without touching any
    caller - repositories, services and routers only ever depend on this
    interface, never a concrete provider.
    """

    @abstractmethod
    async def get_all_schemes(self) -> list[dict]:
        """Returns [{scheme_code, scheme_name}, ...] for the full catalog."""
        raise NotImplementedError

    @abstractmethod
    async def fetch_scheme_with_history(self, scheme_code: int) -> tuple[dict, list[NavPointDTO]]:
        """
        Returns (meta, nav_points) in a single round trip - mfapi.in's
        per-scheme endpoint returns both together, so meta is never fetched
        with a separate call. meta keys: fund_house, scheme_type,
        scheme_category, isin_growth, isin_div_reinvestment, scheme_name.
        nav_points is ordered oldest-to-newest.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_latest_nav(self, scheme_code: int) -> tuple[date, float] | None:
        """Returns (nav_date, nav) for the most recent NAV, or None if unavailable."""
        raise NotImplementedError

    async def get_holdings(self, scheme_code: int) -> list[dict]:
        """Portfolio holdings/sector allocation. Not every provider has this."""
        raise NotSupportedError(f"{type(self).__name__} does not provide holdings data")
