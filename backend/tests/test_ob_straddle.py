"""Order-book smart straddle: continuously arms straddles on the busiest tracked symbols using
live order-book walls (floored by straddleEntryPct), biasing the heavier side's trigger. The
existing straddle fill loop then OCO-fills whichever side price crosses. Binance/SIM, in-memory."""
import asyncio
import time
import server


class FakeHTTP:
    """Serves a fixed depth book for any symbol via .get()."""
    def __init__(self, bids, asks):
        self._bids, self._asks = bids, asks

    async def get(self, url, params=None, timeout=None):
        class R:
            def __init__(s, b, a):
                s._b, s._a = b, a
            def json(s):
                return {"bids": [[str(p), str(q)] for p, q in s._b],
                        "asks": [[str(p), str(q)] for p, q in s._a]}
        return R(self._bids, self._asks)


def _sim(**extra):
    server.BOT["positions"].clear()
    server.BOT["straddles"].clear()
    server.BOT["pendingFutures"].clear()
    server.BOT["stopped"] = False
    server.BOT["obArmTs"] = 0.0
    cfg = server.BOT["config"]
    cfg.update({"enabled": True, "dryRun": True, "exchange": "binance_futures",
                "obStraddleEnabled": True, "obStraddleMaxSymbols": 5, "straddleEntryPct": 0.1,
                "minVolumeUsd": 0.0, "maxVolumeUsd": 0.0, "maxOpenPositions": 10,
                "straddleEnabled": False, "straddleDualSide": False})
    cfg.update(extra)
    server.STATE["latest"] = {"XRPUSDT": {"price": 100.0, "quoteVol": 5e8, "base": "XRP"}}


def test_arms_at_walls_floored_by_pct():
    _sim()
    # perfectly balanced book (bid_usd == ask_usd -> no bias): ask wall 101, bid wall 99
    http = FakeHTTP(bids=[(99.0, 101)], asks=[(101.0, 99)])  # 99*101 == 101*99
    asyncio.run(server.process_bot(http, time.time()))
    s = server.BOT["straddles"]["XRPUSDT"]
    assert s["source"] == "ob"
    assert abs(s["longEntry"] - 101.0) < 1e-6
    assert abs(s["shortEntry"] - 99.0) < 1e-6


def test_floor_applies_when_walls_too_close():
    _sim(straddleEntryPct=0.5)
    # balanced, walls 100.1/99.9 INSIDE the 0.5% floor (100.5 / 99.5) -> floor wins
    http = FakeHTTP(bids=[(99.9, 1001)], asks=[(100.1, 999)])  # 99.9*1001 == 100.1*999
    asyncio.run(server.process_bot(http, time.time()))
    s = server.BOT["straddles"]["XRPUSDT"]
    assert abs(s["longEntry"] - 100.5) < 1e-6
    assert abs(s["shortEntry"] - 99.5) < 1e-6


def test_imbalance_biases_heavier_side():
    _sim()
    # bids dominate strongly -> LONG trigger pulled toward mark (below the raw 101 wall)
    http = FakeHTTP(bids=[(99.0, 100000)], asks=[(101.0, 100)])
    asyncio.run(server.process_bot(http, time.time()))
    s = server.BOT["straddles"]["XRPUSDT"]
    assert s["longEntry"] < 101.0, "heavier bids tighten the long trigger toward mark"
    assert s["longEntry"] > 100.0


def test_disabled_does_not_arm():
    _sim(obStraddleEnabled=False)
    http = FakeHTTP(bids=[(99.0, 1000)], asks=[(101.0, 1000)])
    asyncio.run(server.process_bot(http, time.time()))
    assert "XRPUSDT" not in server.BOT["straddles"]


def test_ignored_on_hyperliquid():
    _sim(exchange="hyperliquid")
    http = FakeHTTP(bids=[(99.0, 1000)], asks=[(101.0, 1000)])
    asyncio.run(server.process_bot(http, time.time()))
    assert "XRPUSDT" not in server.BOT["straddles"]


def test_cleanup():
    server.BOT["straddles"].clear()
    server.BOT["config"].update({"enabled": False, "obStraddleEnabled": False,
                                 "exchange": "hyperliquid"})
