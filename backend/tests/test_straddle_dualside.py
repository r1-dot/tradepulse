"""Dual-side straddle (Binance + SIM only): when armed, BOTH the long and the short leg
open independently as price touches each level (no OCO cancel), each tracked under its own
position key and managed by Smart Trailing TP. Direct, fully in-memory, NO real orders."""
import asyncio
import time
import server


class FakeHTTP:
    async def request(self, *a, **k):
        raise AssertionError("dry-run must not hit the network")

    async def get(self, *a, **k):
        raise AssertionError("dry-run must not hit the network")


def _setup(dual=True, exchange="binance_futures"):
    server.BOT["positions"].clear()
    server.BOT["straddles"].clear()
    server.BOT["lastTradeTs"].clear()
    server.BOT["stopped"] = False
    server.BOT["dailyPnl"] = 0.0
    cfg = server.BOT["config"]
    cfg.update({
        "enabled": True, "dryRun": True, "exchange": exchange,
        "straddleEnabled": True, "straddleDualSide": dual,
        "maxOpenPositions": 10, "maxPositionUsdt": 100.0,
        "maxLossPerTradeUsdt": 0.0, "dailyLossLimit": 0.0, "cooldownSec": 0,
        "trailEnabled": True, "trailInitialSlPct": 5.0,
        "minVolumeUsd": 0.0, "maxVolumeUsd": 0.0,
    })
    # ensure the symbol is tracked so _open_position can price it
    server.STATE["latest"]["BTCUSDT"] = {
        "price": 100.0, "quoteVol": 10_000_000_000.0, "base": "BTC",
    }


def _arm(mark=100.0, entry_pct=1.0):
    now = time.time()
    off = mark * entry_pct / 100.0
    server.BOT["straddles"]["BTCUSDT"] = {
        "symbol": "BTCUSDT", "base": "BTC", "mark": mark,
        "longEntry": mark + off, "shortEntry": mark - off,
        "createdTs": now,
    }


def _tick(price):
    server.STATE["latest"]["BTCUSDT"]["price"] = price
    asyncio.run(server.process_bot(FakeHTTP(), time.time()))


def test_dualside_opens_both_legs():
    _setup(dual=True)
    _arm(mark=100.0, entry_pct=1.0)  # long>=101, short<=99
    _tick(101.0)   # touch long level (flat entry -> not yet secured)
    assert "BTCUSDT#L" in server.BOT["positions"], "long leg should open"
    assert "BTCUSDT" in server.BOT["straddles"], "straddle stays armed until both fill"
    _tick(99.0)    # touch short level; wide initial stop keeps the long alive
    assert "BTCUSDT#S" in server.BOT["positions"], "short leg should also open"
    assert "BTCUSDT#L" in server.BOT["positions"], "long leg still open (both legs coexist)"
    assert "BTCUSDT" not in server.BOT["straddles"], "straddle cleared once both legs filled"
    longp = server.BOT["positions"]["BTCUSDT#L"]
    shortp = server.BOT["positions"]["BTCUSDT#S"]
    assert longp["side"] == "long" and shortp["side"] == "short"
    assert longp["symbol"] == "BTCUSDT" and shortp["symbol"] == "BTCUSDT"
    assert longp["tpPrice"] is None and shortp["tpPrice"] is None  # smart-trailing (no fixed TP)


def test_oco_cancels_opposite_when_dual_off():
    _setup(dual=False)
    _arm(mark=100.0, entry_pct=1.0)
    _tick(101.0)   # long fills -> short cancelled, straddle gone
    assert "BTCUSDT" in server.BOT["positions"]      # OCO uses the plain symbol key
    assert "BTCUSDT#L" not in server.BOT["positions"]
    assert "BTCUSDT" not in server.BOT["straddles"]
    _tick(99.0)    # no armed straddle left -> no short leg appears
    assert "BTCUSDT#S" not in server.BOT["positions"]


def test_dualside_ignored_on_hyperliquid():
    _setup(dual=True, exchange="hyperliquid")
    _arm(mark=100.0, entry_pct=1.0)
    _tick(101.0)   # dual requested but HL -> behaves as OCO (single leg, plain key)
    assert "BTCUSDT" in server.BOT["positions"]
    assert "BTCUSDT#L" not in server.BOT["positions"]


def test_status_reports_unique_keys_for_both_legs():
    _setup(dual=True)
    _arm(mark=100.0, entry_pct=1.0)
    _tick(101.0)
    _tick(99.0)
    st = server._bot_status()
    keys = sorted(p["posKey"] for p in st["openPositions"])
    syms = [p["symbol"] for p in st["openPositions"]]
    assert keys == ["BTCUSDT#L", "BTCUSDT#S"]
    assert syms == ["BTCUSDT", "BTCUSDT"]  # real market symbol, not the composite key


def test_cleanup():
    server.BOT["positions"].clear()
    server.BOT["straddles"].clear()
    server.BOT["config"].update({"enabled": False, "straddleEnabled": False,
                                  "straddleDualSide": False, "dryRun": True})
