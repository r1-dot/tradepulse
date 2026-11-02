"""Fixed TP/SL system: when Smart Trailing TP is OFF, every filled position (incl. straddle/
hybrid legs that would otherwise trail) gets a fixed take-profit & stop-loss from tpPct/slPct
and exits at those levels. When trailing is ON, straddle/hybrid still trail. In-memory, SIM."""
import asyncio
import time
import server


class FakeHTTP:
    async def get(self, *a, **k):
        raise AssertionError("no network in SIM")


def _sim(trail, **extra):
    server.BOT["positions"].clear()
    server.BOT["pendingFutures"].clear()
    server.BOT["straddles"].clear()
    server.BOT["stopped"] = False
    cfg = server.BOT["config"]
    cfg.update({"enabled": True, "dryRun": True, "exchange": "hyperliquid",
                "trailEnabled": trail, "tpPct": 2.0, "slPct": 1.0, "autoExit": True,
                "maxLossPerTradeUsdt": 0.0, "dailyLossLimit": 0.0, "futuresLeverage": 1,
                "maxPositionUsdt": 100})
    cfg.update(extra)
    server.STATE["latest"]["ETHUSDT"] = {"price": 100.0, "quoteVol": 1e9, "base": "ETH"}


def _open_straddle_long():
    # straddle legs pass tp_price=None + a computed trailing init stop
    asyncio.run(server._open_position(FakeHTTP(), "ETHUSDT", 100.0, time.time(), side="long",
                                      tp_price=None, sl_price=99.7, source="straddle", pos_key="ETHUSDT"))


def _tick(price):
    server.STATE["latest"]["ETHUSDT"]["price"] = price
    asyncio.run(server.process_bot(FakeHTTP(), time.time()))


def test_trail_off_sets_fixed_tpsl():
    _sim(trail=False)
    _open_straddle_long()
    pos = server.BOT["positions"]["ETHUSDT"]
    assert abs(pos["tpPrice"] - 102.0) < 1e-9, "fixed TP = +2% from entry"
    assert abs(pos["slPrice"] - 99.0) < 1e-9, "fixed SL = -1% from entry (overrides trailing stop)"


def test_trail_on_keeps_trailing():
    _sim(trail=True)
    _open_straddle_long()
    pos = server.BOT["positions"]["ETHUSDT"]
    assert pos["tpPrice"] is None, "trailing on -> no fixed TP"
    assert abs(pos["slPrice"] - 99.7) < 1e-9, "trailing keeps the tight init stop passed in"


def test_fixed_tp_exit():
    _sim(trail=False)
    _open_straddle_long()
    _tick(101.0)                       # below TP, stays open
    assert "ETHUSDT" in server.BOT["positions"]
    _tick(102.5)                       # hits fixed TP -> closes
    assert "ETHUSDT" not in server.BOT["positions"], "closes at fixed take-profit"


def test_fixed_sl_exit():
    _sim(trail=False)
    _open_straddle_long()
    _tick(98.5)                        # hits fixed SL -> closes
    assert "ETHUSDT" not in server.BOT["positions"], "closes at fixed stop-loss"


def test_cleanup():
    server.BOT["positions"].clear()
    server.BOT["config"].update({"enabled": False, "trailEnabled": True})
