"""
Unit tests for service/optionChain/blackScholes.py.
"""

from service.optionChain import blackScholes as bs

SPOT, STRIKE, T_YEARS, RATE = 24270.0, 24300.0, 30 / 365, 0.065


class TestRoundTrip:

    def test_ce_round_trip_across_vol_range(self):
        for true_sigma in (0.10, 0.15, 0.337, 0.8, 1.5):
            price = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, true_sigma, "CE")
            iv = bs.implied_volatility(price, SPOT, STRIKE, T_YEARS, "CE", RATE)
            assert iv is not None
            assert abs(iv - true_sigma) < 1e-3

    def test_pe_round_trip_across_vol_range(self):
        for true_sigma in (0.10, 0.15, 0.337, 0.8, 1.5):
            price = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, true_sigma, "PE")
            iv = bs.implied_volatility(price, SPOT, STRIKE, T_YEARS, "PE", RATE)
            assert iv is not None
            assert abs(iv - true_sigma) < 1e-3

    def test_near_expiry_still_converges_via_bisection_fallback(self):
        """Near-expiry options have tiny vega, which can stall Newton-Raphson -
        the bisection fallback must still find a root."""
        t_near = 1 / 365
        price = bs.bs_price(SPOT, STRIKE, t_near, RATE, 0.4, "CE")
        iv = bs.implied_volatility(price, SPOT, STRIKE, t_near, "CE", RATE)
        assert iv is not None
        assert abs(iv - 0.4) < 1e-2


class TestEdgeCases:

    def test_zero_time_to_expiry_returns_none(self):
        assert bs.implied_volatility(50, SPOT, STRIKE, 0, "CE") is None

    def test_negative_time_to_expiry_returns_none(self):
        assert bs.implied_volatility(50, SPOT, STRIKE, -1, "CE") is None

    def test_ltp_below_intrinsic_returns_none(self):
        """Deep ITM CE priced below its own intrinsic value is an arbitrage-
        violating/stale quote - must not fabricate an IV."""
        spot, strike = 25000.0, 24300.0  # intrinsic = 700
        assert bs.implied_volatility(500, spot, strike, T_YEARS, "CE") is None

    def test_negative_spot_returns_none(self):
        assert bs.implied_volatility(50, -1, STRIKE, T_YEARS, "CE") is None

    def test_zero_or_none_ltp_returns_none(self):
        assert bs.implied_volatility(0, SPOT, STRIKE, T_YEARS, "CE") is None
        assert bs.implied_volatility(None, SPOT, STRIKE, T_YEARS, "CE") is None

    def test_price_above_max_sigma_range_returns_none(self):
        """A quote implying vol above SIGMA_MAX (500%) has no solution in-range."""
        absurd_price = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, bs.SIGMA_MAX, "CE") * 5
        assert bs.implied_volatility(absurd_price, SPOT, STRIKE, T_YEARS, "CE") is None

    def test_never_raises_on_garbage_input(self):
        # Should return None, not throw, for pathological inputs
        assert bs.implied_volatility(float("nan"), SPOT, STRIKE, T_YEARS, "CE") is None
