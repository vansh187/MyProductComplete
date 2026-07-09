"""
Pure Black-Scholes pricing + implied-volatility solver for index options.

Shoonya provides no IV field anywhere in its API (option-chain endpoint
returns only contract definitions; get_quotes has no IV; get_option_greek
computes greeks FROM a supplied volatility rather than backing one out), so
IV is computed here from each leg's live LTP.

No I/O, no external dependencies (numpy/scipy not required - math.erf covers
the normal CDF). Never raises: bad/degenerate inputs return None so callers
can simply omit `iv` for that leg instead of failing the whole chain.
"""

import math

SIGMA_MIN = 0.01
SIGMA_MAX = 5.0
_INITIAL_GUESS = 0.3


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(spot: float, strike: float, t_years: float, rate: float, sigma: float) -> tuple[float, float]:
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def bs_price(spot: float, strike: float, t_years: float, rate: float, sigma: float, option_type: str) -> float:
    """Black-Scholes price for a European CE/PE. Caller must ensure t_years > 0 and sigma > 0."""
    d1, d2 = _d1_d2(spot, strike, t_years, rate, sigma)
    discounted_strike = strike * math.exp(-rate * t_years)

    if option_type.upper() == "CE":
        return spot * _norm_cdf(d1) - discounted_strike * _norm_cdf(d2)
    else:
        return discounted_strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _vega(spot: float, strike: float, t_years: float, rate: float, sigma: float) -> float:
    d1, _ = _d1_d2(spot, strike, t_years, rate, sigma)
    return spot * math.sqrt(t_years) * _norm_pdf(d1)


def _intrinsic_value(spot: float, strike: float, option_type: str) -> float:
    if option_type.upper() == "CE":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def _bisection_iv(ltp: float, spot: float, strike: float, t_years: float, rate: float,
                   option_type: str, tol: float, max_iter: int) -> float | None:
    lo, hi = SIGMA_MIN, SIGMA_MAX
    price_lo = bs_price(spot, strike, t_years, rate, lo, option_type) - ltp
    price_hi = bs_price(spot, strike, t_years, rate, hi, option_type) - ltp

    if price_lo > 0 or price_hi < 0:
        # ltp is outside the price range achievable within [SIGMA_MIN, SIGMA_MAX]
        return None

    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        price_mid = bs_price(spot, strike, t_years, rate, mid, option_type) - ltp
        if abs(price_mid) < tol:
            return mid
        if price_mid > 0:
            hi = mid
        else:
            lo = mid

    return (lo + hi) / 2.0


def implied_volatility(
    ltp: float,
    spot: float,
    strike: float,
    t_years: float,
    option_type: str,
    rate: float = 0.065,
    tol: float = 1e-4,
    max_iter: int = 50,
) -> float | None:
    """
    Returns implied volatility as a fraction (e.g. 0.337 for 33.7%), or None
    if inputs are degenerate (expired/negative time) or the price is below
    intrinsic value (arbitrage-violating quote, e.g. a stale/crossed LTP).
    """
    if t_years is None or math.isnan(t_years) or t_years <= 0:
        return None
    if ltp is None or math.isnan(ltp) or ltp <= 0:
        return None
    if spot is None or math.isnan(spot) or spot <= 0:
        return None
    if strike is None or math.isnan(strike) or strike <= 0:
        return None

    intrinsic = _intrinsic_value(spot, strike, option_type)
    if ltp <= intrinsic:
        return None

    sigma = _INITIAL_GUESS
    for _ in range(max_iter):
        try:
            price = bs_price(spot, strike, t_years, rate, sigma, option_type)
            diff = price - ltp
        except (ValueError, OverflowError, ZeroDivisionError):
            break

        if abs(diff) < tol:
            return max(min(sigma, SIGMA_MAX), SIGMA_MIN)

        vega = _vega(spot, strike, t_years, rate, sigma)
        if vega < 1e-8:
            break

        sigma -= diff / vega
        if not (SIGMA_MIN <= sigma <= SIGMA_MAX) or math.isnan(sigma):
            break
    else:
        return max(min(sigma, SIGMA_MAX), SIGMA_MIN)

    # Newton-Raphson diverged/stalled (near expiry, deep ITM/OTM, etc.) - fall
    # back to bisection, which is slower but guaranteed to converge if a root
    # exists within [SIGMA_MIN, SIGMA_MAX].
    return _bisection_iv(ltp, spot, strike, t_years, rate, option_type, tol, max_iter)
