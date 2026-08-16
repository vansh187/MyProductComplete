from dateutil.relativedelta import relativedelta

from productdto.mutualFundDto import NavPointDTO

# Longest period any return figure needs (5Y CAGR) plus a buffer so the
# calculator's own relativedelta(months=60) lookback always finds a point
# on/before its target date - see MFReturnsCalculator._find_on_or_before.
DEFAULT_CAP_YEARS = 5
_BOUNDARY_BUFFER_DAYS = 15


class MFNavHistoryCapper:
    """
    Single source of truth for how far back NAV history is kept - used by
    both MFNavBackfillService (one-time backfill) and MutualFundService
    (live-first write-through), so the two paths can never again drift
    apart on the exact cutoff math the way the original 365*years timedelta
    vs. relativedelta(months=60) mismatch did.
    """

    def cap(self, points: list[NavPointDTO], years: int = DEFAULT_CAP_YEARS) -> list[NavPointDTO]:
        if not points:
            return points
        cutoff = points[-1].nav_date - relativedelta(years=years) - relativedelta(days=_BOUNDARY_BUFFER_DAYS)
        return [point for point in points if point.nav_date >= cutoff]
