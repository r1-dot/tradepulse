"""
Hybrid Power System — short-window "power" signal engine.

Fed once per second per coin from the scanner's per-second trade deltas:
  amount_usd = estimated USD volume of that second's trades (trades * avgTradeSize)
  side       = dominant aggressor side that second ('buy'/'sell')
  count      = number of trades that second

get_signal() derives multi-window power/flow metrics and emits LONG / SHORT / None.
"""
import time
from collections import deque, defaultdict

# average trade size ($) baseline per coin
NORMAL_AVG = {"BTC": 800, "ETH": 600, "HYPE": 200, "PURR": 80, "JEFF": 60}
DEFAULT_AVG = 100
BUFFER_SEC = 300

# coin -> deque[(ts, amount_usd, side, count)]
_buffers = defaultdict(deque)


def update(coin, amount_usd, side, timestamp, count=1):
    """Append one aggregated trade sample and prune anything older than 300s."""
    b = _buffers[coin]
    b.append((timestamp, float(amount_usd), side, int(count)))
    cutoff = timestamp - BUFFER_SEC
    while b and b[0][0] < cutoff:
        b.popleft()


def _window(coin, now, secs):
    """Return (sum_amount, total_count, buy_count) over the last `secs` seconds."""
    b = _buffers.get(coin)
    if not b:
        return (0.0, 0, 0)
    lo = now - secs
    sa = 0.0
    tc = 0
    bc = 0
    for ts, amt, side, cnt in reversed(b):
        if ts < lo:
            break
        sa += amt
        tc += cnt
        if side == "buy":
            bc += cnt
    return (sa, tc, bc)


def normal_avg(coin):
    return NORMAL_AVG.get(coin, DEFAULT_AVG)


def trail_stop_offset(peak_pct, secure, be, step):
    """Locked SL offset (% from entry) for a trailing stop given the peak profit %.
    None until peak reaches `secure`. Matches: +0.40->BE+ (be), +0.90->+0.45, +1.40->+0.95
    with secure=0.40, be=0.05, step=0.5."""
    if peak_pct < secure:
        return None
    return max(be, peak_pct - step + be)


def get_signal(coin, vol_24h, params, now=None):
    """Compute power metrics + LONG/SHORT/None for a coin."""
    now = now or time.time()
    navg = normal_avg(coin)
    v = float(vol_24h or 0)

    sa60, tc60, bc60 = _window(coin, now, 60)
    sa5, _, _ = _window(coin, now, 5)
    sa1, tc1, bc1 = _window(coin, now, 1)

    power_1m = (sa60 / v * 100) if v else 0.0
    avg_1m = (sa60 / tc60) if tc60 else 0.0
    buy_1m = (bc60 / tc60 * 100) if tc60 else 0.0
    power_1s = (sa1 / v * 100) if v else 0.0
    buy_1s = (bc1 / tc1 * 100) if tc1 else 0.0
    power_5s = (sa5 / v * 100) if v else 0.0

    min_p = float(params.get("min_power_1m", 0.6))
    max_p = float(params.get("max_power_1m", 1.8))
    burst = float(params.get("burst_power", 0.06))

    base = (min_p <= power_1m <= max_p) and (avg_1m > navg * 2.2) and (power_1s > burst) and (power_5s < 0.8)
    signal = None
    if base and buy_1m > 60 and buy_1s > 75:
        signal = "LONG"
    elif base and buy_1m < 40 and buy_1s < 25:
        signal = "SHORT"

    return {
        "coin": coin,
        "power_1m": round(power_1m, 4),
        "power_1s": round(power_1s, 4),
        "power_5s": round(power_5s, 4),
        "avg": round(avg_1m, 2),
        "buy": round(buy_1m, 1),
        "buy_1s": round(buy_1s, 1),
        "normalAvg": navg,
        "trades_1m": tc60,
        "signal": signal,
    }
