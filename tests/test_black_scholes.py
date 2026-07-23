"""
Unit tests for service/optionChain/blackScholes.py.

Covers:
- bs_price(): known reference values, put-call parity, monotonicity in vol
- Greeks-adjacent internals (_vega, _norm_cdf, _norm_pdf) via their
  contribution to bs_price / implied_volatility behavior
- implied_volatility(): round trips (recovering the vol used to price),
  edge cases (zero/negative time, degenerate inputs, arbitrage-violating
  quotes), and the bisection fallback near expiry
"""

import math

import pytest

from service.optionChain import blackScholes as bs

SPOT, STRIKE, T_YEARS, RATE = 24270.0, 24300.0, 30 / 365, 0.065


class TestBsPriceKnownValues:

    def test_atm_call_matches_reference_value(self):
        # Reference value computed independently via the standard BS formula
        # for spot=100, strike=100, t=1y, r=0.05, sigma=0.2 -> ~10.4506
        price = bs.bs_price(100.0, 100.0, 1.0, 0.05, 0.2, "CE")
        assert price == pytest.approx(10.4506, abs=1e-3)

    def test_atm_put_matches_reference_value(self):
        # Same inputs, put -> ~5.5735
        price = bs.bs_price(100.0, 100.0, 1.0, 0.05, 0.2, "PE")
        assert price == pytest.approx(5.5735, abs=1e-3)

    def test_put_call_parity_holds(self):
        # C - P = S - K*e^(-rT) for any consistent set of inputs
        call = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, 0.25, "CE")
        put = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, 0.25, "PE")
        expected = SPOT - STRIKE * math.exp(-RATE * T_YEARS)
        assert (call - put) == pytest.approx(expected, abs=1e-6)

    def test_call_price_increases_with_volatility(self):
        low = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, 0.10, "CE")
        high = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, 0.50, "CE")
        assert high > low

    def test_put_price_increases_with_volatility(self):
        low = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, 0.10, "PE")
        high = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, 0.50, "PE")
        assert high > low

    def test_deep_itm_call_approaches_intrinsic_plus_carry(self):
        # Deep ITM call with tiny vol should price close to spot - PV(strike)
        price = bs.bs_price(30000.0, 20000.0, T_YEARS, RATE, 0.01, "CE")
        expected = 30000.0 - 20000.0 * math.exp(-RATE * T_YEARS)
        assert price == pytest.approx(expected, abs=1.0)

    def test_deep_otm_call_is_near_zero(self):
        price = bs.bs_price(20000.0, 30000.0, T_YEARS, RATE, 0.15, "CE")
        assert price == pytest.approx(0.0, abs=1.0)

    def test_call_and_put_are_positive_for_reasonable_inputs(self):
        call = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, 0.2, "CE")
        put = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, 0.2, "PE")
        assert call > 0
        assert put > 0

    def test_option_type_is_case_insensitive(self):
        upper = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, 0.2, "ce")
        lower = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, 0.2, "CE")
        assert upper == pytest.approx(lower)


class TestNormHelpers:

    def test_norm_cdf_of_zero_is_half(self):
        assert bs._norm_cdf(0.0) == pytest.approx(0.5)

    def test_norm_cdf_is_monotonic(self):
        assert bs._norm_cdf(-1.0) < bs._norm_cdf(0.0) < bs._norm_cdf(1.0)

    def test_norm_pdf_peak_at_zero(self):
        assert bs._norm_pdf(0.0) == pytest.approx(1.0 / math.sqrt(2.0 * math.pi))

    def test_norm_pdf_symmetric(self):
        assert bs._norm_pdf(1.5) == pytest.approx(bs._norm_pdf(-1.5))


class TestVega:

    def test_vega_is_positive_for_valid_inputs(self):
        vega = bs._vega(SPOT, STRIKE, T_YEARS, RATE, 0.2)
        assert vega > 0

    def test_vega_shrinks_as_expiry_approaches(self):
        far = bs._vega(SPOT, STRIKE, 30 / 365, RATE, 0.2)
        near = bs._vega(SPOT, STRIKE, 1 / 365, RATE, 0.2)
        assert near < far


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

    def test_zero_or_negative_strike_returns_none(self):
        assert bs.implied_volatility(50, SPOT, 0, T_YEARS, "CE") is None
        assert bs.implied_volatility(50, SPOT, -100, T_YEARS, "CE") is None

    def test_price_above_max_sigma_range_returns_none(self):
        """A quote implying vol above SIGMA_MAX (500%) has no solution in-range."""
        absurd_price = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, bs.SIGMA_MAX, "CE") * 5
        assert bs.implied_volatility(absurd_price, SPOT, STRIKE, T_YEARS, "CE") is None

    def test_never_raises_on_garbage_input(self):
        # Should return None, not throw, for pathological inputs
        assert bs.implied_volatility(float("nan"), SPOT, STRIKE, T_YEARS, "CE") is None

    def test_nan_time_returns_none(self):
        assert bs.implied_volatility(50, SPOT, STRIKE, float("nan"), "CE") is None

    def test_implied_volatility_result_within_bounds(self):
        price = bs.bs_price(SPOT, STRIKE, T_YEARS, RATE, 0.3, "CE")
        iv = bs.implied_volatility(price, SPOT, STRIKE, T_YEARS, "CE", RATE)
        assert iv is not None
        assert bs.SIGMA_MIN <= iv <= bs.SIGMA_MAX
