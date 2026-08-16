import bisect

from dateutil.relativedelta import relativedelta

from productdto.mutualFundDto import NavPointDTO, ReturnsDTO

# (field name on ReturnsDTO, offset in months, whether to annualize as CAGR)
_TRAILING_PERIODS = [
    ("return_1m", 1, False),
    ("return_6m", 6, False),
    ("return_1y", 12, False),
    ("return_3y", 36, True),
    ("return_5y", 60, True),
]


class MFReturnsCalculator:
    """
    Computes trailing returns purely from a locally-held NAV series (no
    external calls) - point-to-point % for short periods, CAGR for periods
    long enough to compound. A scheme younger than a given period yields
    None for that field rather than a misleadingly-computed short series.
    """

    def compute_trailing_returns(self, nav_series: list[NavPointDTO]) -> ReturnsDTO:
        if not nav_series:
            return ReturnsDTO()

        ordered = sorted(nav_series, key=lambda point: point.nav_date)
        dates = [point.nav_date for point in ordered]
        latest = ordered[-1]

        returns = ReturnsDTO(latest_nav=latest.nav)
        for field_name, months_back, annualize in _TRAILING_PERIODS:
            target_date = latest.nav_date - relativedelta(months=months_back)
            reference = self._find_on_or_before(ordered, dates, target_date)
            if reference is None or reference.nav <= 0:
                continue
            growth = latest.nav / reference.nav
            if annualize:
                years = months_back / 12
                value = (growth ** (1 / years)) - 1
            else:
                value = growth - 1
            setattr(returns, field_name, round(value * 100, 2))

        returns.day_change_pct = self.compute_day_change(ordered)
        return returns

    def compute_day_change(self, nav_series: list[NavPointDTO]) -> float | None:
        ordered = sorted(nav_series, key=lambda point: point.nav_date)
        if len(ordered) < 2:
            return None
        latest, previous = ordered[-1], ordered[-2]
        if previous.nav <= 0:
            return None
        return round(((latest.nav / previous.nav) - 1) * 100, 2)

    def _find_on_or_before(self, ordered: list[NavPointDTO], dates: list, target_date) -> NavPointDTO | None:
        """Latest point with nav_date <= target_date, via binary search on the sorted date list."""
        idx = bisect.bisect_right(dates, target_date) - 1
        if idx < 0:
            return None
        return ordered[idx]
