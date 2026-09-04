"""
Hyperliquid coin filtering + size/price rounding.

Fixes two live-trading failures:
  1. ("float_to_wire causes rounding", <raw float>)  -> order size had more precision
     than the coin allows. Hyperliquid requires size rounded to the coin's szDecimals.
  2. KeyError: 'ARKM' / 'DIA' / ...                  -> coin is not listed on Hyperliquid.
     Such Binance symbols must be skipped (never sent to Hyperliquid).

Hyperliquid rounding rules (perps):
  - size  : round(size, szDecimals)
  - price : max 5 significant figures AND at most (MAX_DECIMALS - szDecimals) decimals,
            where MAX_DECIMALS = 6 for perps, 8 for spot. Integer prices are always allowed.
"""

MAX_DECIMALS_PERP = 6
MAX_DECIMALS_SPOT = 8

# name -> szDecimals, populated from Info.meta()
_META = {"universe": {}, "maxlev": {}, "loaded": False}


def load_hl_meta(info, force=False):
    """Build {coin: szDecimals} + {coin: maxLeverage} from a Hyperliquid Info instance."""
    if _META["loaded"] and not force:
        return _META["universe"]
    meta = info.meta()
    uni = {}
    lev = {}
    for a in meta.get("universe", []):
        try:
            name = a["name"].upper()
            uni[name] = int(a.get("szDecimals", 0))
            lev[name] = int(a.get("maxLeverage", 1) or 1)
        except (KeyError, TypeError, ValueError):
            continue
    _META["universe"] = uni
    _META["maxlev"] = lev
    _META["loaded"] = bool(uni)
    return uni


def max_leverage(coin):
    """Max leverage Hyperliquid allows for a coin (defaults to 1 if unknown)."""
    return _META["maxlev"].get(coin, 1)


def meta_loaded():
    return _META["loaded"]


def hl_coins():
    return set(_META["universe"].keys())


def convert_binance_to_hyperliquid(base):
    """
    Map a Binance base asset (e.g. 'BTC', 'ARKM', '1000PEPE') to its Hyperliquid coin
    name, or return None if the coin is not tradable on Hyperliquid perps.
    """
    if not base:
        return None
    b = str(base).upper()
    uni = _META["universe"]
    if not uni:
        return None
    if b in uni:
        return b
    # Binance "1000X" leverage tokens map to Hyperliquid "kX"
    if b.startswith("1000") and ("K" + b[4:]) in uni:
        return "K" + b[4:]
    if b.startswith("K") and b in uni:
        return b
    return None


def round_size(coin, size):
    """Round an order size to the coin's szDecimals. Returns None if coin unknown."""
    szd = _META["universe"].get(coin)
    if szd is None:
        return None
    return round(float(size), szd)


def round_price(coin, price, is_spot=False):
    """Round a price to Hyperliquid's 5-sig-fig / max-decimals rule. None if coin unknown."""
    szd = _META["universe"].get(coin)
    if szd is None or price is None:
        return None
    p = float(price)
    if p <= 0:
        return None
    max_dec = (MAX_DECIMALS_SPOT if is_spot else MAX_DECIMALS_PERP) - szd
    max_dec = max(0, max_dec)
    # 5 significant figures, then clamp decimal places
    p = float(f"{p:.5g}")
    return round(p, max_dec)


def get_rounded_size_and_price(base, size, price=None, is_spot=False):
    """
    Filter + round in one call. Returns (hl_coin, rounded_size, rounded_price) or None
    when the coin is not on Hyperliquid or the size rounds to <= 0.
    """
    hl = convert_binance_to_hyperliquid(base)
    if hl is None:
        return None
    rs = round_size(hl, size)
    if rs is None or rs <= 0:
        return None
    rp = round_price(hl, price, is_spot) if price else None
    return (hl, rs, rp)
