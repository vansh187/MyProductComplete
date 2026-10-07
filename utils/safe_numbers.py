"""
The one place broker-supplied numbers are parsed. Shoonya sends numbers as
strings, omits fields, sends "" for "no value", and occasionally garbage;
none of that may raise on a hot path. NaN/inf are treated as "no value" too,
so they can never leak into prices, quantities or JSON responses.
"""

import math


def safe_float(value, default=None):
    """float(value), or `default` for None/""/unparseable/NaN/inf."""
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def safe_int(value, default=None):
    """int(float(value)) - broker quantities may arrive as "75.0" - or
    `default` for None/""/unparseable/NaN/inf."""
    number = safe_float(value)
    return int(number) if number is not None else default
