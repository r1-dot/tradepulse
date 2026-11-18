"""Exact decimal formatting for Binance USD-M Futures order quantities and prices.

Binance rejects any quantity that is not an exact multiple of the symbol's stepSize, and any
price/trigger that is not an exact multiple of tickSize (error -1111 "Precision is over the
maximum defined for this asset"). Python floats introduce binary artifacts (e.g. 0.018*1000 =
17.999999...), so all rounding is done with Decimal and serialized to a fixed-point string.
"""
from decimal import Decimal, ROUND_DOWN, ROUND_UP

D = Decimal


def _decimals(step: str) -> int:
    return max(0, -D(str(step)).normalize().as_tuple().exponent)


def _fixed(value: Decimal, step: str) -> str:
    return format(value, f".{_decimals(step)}f")


def floor_to_step(value, step: str) -> str:
    """Round value DOWN to the nearest multiple of step, as a fixed-point string."""
    v, s = D(str(value)), D(str(step))
    n = (v / s).to_integral_value(rounding=ROUND_DOWN)
    return _fixed(n * s, step)


def ceil_to_step(value, step: str) -> str:
    """Round value UP to the nearest multiple of step, as a fixed-point string."""
    v, s = D(str(value)), D(str(step))
    n = (v / s).to_integral_value(rounding=ROUND_UP)
    return _fixed(n * s, step)


def round_price(value, tick: str, direction: str = "down") -> str:
    """Round a price/trigger to the symbol's tickSize. direction 'up' ceils, 'down' floors."""
    return ceil_to_step(value, tick) if direction == "up" else floor_to_step(value, tick)
