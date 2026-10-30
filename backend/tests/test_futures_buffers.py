"""FUTURES IOC / slippage system (Binance USD-M). A buffer>0 does NOT fill at market: it
registers a slippage-priced pending entry (LONG -buf% below mark, SHORT +buf% above) that
fills when the mark reaches that price, or auto-cancels after 5 min. buffer==0 = plain MARKET
that fills instantly. SIM only, NO real orders."""
import asyncio
import time
import server


class FakeHTTP:
    async def get(self, *a, **k):
        raise AssertionError("no market-data fetch expected")


def _sim(**extra):
    server.BOT["positions"].clear()
    server.BOT["pendingFutures"].clear()
    server.BOT["straddles"].clear()
    server.BOT["stopped"] = False
    cfg = server.BOT["config"]
    cfg.update({"enabled": True, "dryRun": True, "exchange": "binance_futures",
                "futuresLeverage": 1, "maxPositionUsdt": 100, "straddleEnabled": False,
                "straddleDualSide": False, "maxLossPerTradeUsdt": 0.0, "dailyLossLimit": 0.0,
                "futLongBufferPct": 0.0, "futShortBufferPct": 0.0})
    cfg.update(extra)
    server.STATE["latest"]["BTCUSDT"] = {"price": 100.0, "quoteVol": 1e9, "base": "BTC"}


def _open(side, pos_key):
    asyncio.run(server._open_position(FakeHTTP(), "BTCUSDT", 100.0, time.time(), side=side,
                                      tp_price=None, sl_price=None, source="signal", pos_key=pos_key))


def _tick(price):
    server.STATE["latest"]["BTCUSDT"]["price"] = price
    asyncio.run(server.process_bot(FakeHTTP(), time.time()))


def test_zero_buffer_fills_at_market_now():
    _sim(futLongBufferPct=0.0)
    _open("long", "BTCUSDT")
    assert "BTCUSDT" in server.BOT["positions"], "buffer 0 = instant market fill"
    assert not server.BOT["pendingFutures"]
    assert abs(server.BOT["positions"]["BTCUSDT"]["entryPrice"] - 100.0) < 1e-9


def test_buffer_registers_pending_long_below_mark():
    _sim(futLongBufferPct=1.0)
    _open("long", "BTCUSDT")
    assert "BTCUSDT" not in server.BOT["positions"], "buffer>0 must NOT open immediately"
    pend = server.BOT["pendingFutures"]["BTCUSDT"]
    assert pend["side"] == "long"
    assert abs(pend["limitPrice"] - 99.0) < 1e-9, "long limit = 1% below mark"


def test_buffer_registers_pending_short_above_mark():
    _sim(futShortBufferPct=2.0)
    _open("short", "BTCUSDT")
    pend = server.BOT["pendingFutures"]["BTCUSDT"]
    assert pend["side"] == "short"
    assert abs(pend["limitPrice"] - 102.0) < 1e-9, "short limit = 2% above mark"


def test_pending_fills_when_price_reaches_limit():
    _sim(futLongBufferPct=1.0)
    _open("long", "BTCUSDT")               # pending long, triggers at 99
    _tick(99.5)                            # not yet reached
    assert "BTCUSDT" not in server.BOT["positions"]
    assert "BTCUSDT" in server.BOT["pendingFutures"]
    _tick(99.0)                            # trigger hit -> MARKET fill at mark (99.0)
    assert "BTCUSDT" in server.BOT["positions"], "fills once mark <= trigger"
    assert "BTCUSDT" not in server.BOT["pendingFutures"]
    assert abs(server.BOT["positions"]["BTCUSDT"]["entryPrice"] - 99.0) < 1e-9


def test_pending_auto_cancels_after_5min():
    _sim(futLongBufferPct=1.0)
    _open("long", "BTCUSDT")
    server.BOT["pendingFutures"]["BTCUSDT"]["createdTs"] = time.time() - 301  # age it past 5 min
    _tick(100.0)                           # price never reached the limit
    assert "BTCUSDT" not in server.BOT["pendingFutures"], "expired entry is cancelled"
    assert "BTCUSDT" not in server.BOT["positions"], "nothing opened"


def test_pending_respects_configurable_timeout():
    _sim(futLongBufferPct=1.0, futPendingTimeoutSec=2)
    _open("long", "BTCUSDT")
    server.BOT["pendingFutures"]["BTCUSDT"]["createdTs"] = time.time() - 3  # older than 2s ttl
    _tick(100.0)
    assert "BTCUSDT" not in server.BOT["pendingFutures"], "cancels after the configured 2s"
    # a longer ttl keeps it alive at the same age
    _sim(futLongBufferPct=1.0, futPendingTimeoutSec=60)
    _open("long", "BTCUSDT")
    server.BOT["pendingFutures"]["BTCUSDT"]["createdTs"] = time.time() - 3
    _tick(100.0)
    assert "BTCUSDT" in server.BOT["pendingFutures"], "still pending within the 60s ttl"


def test_tiny_buffer_floor():
    _sim()
    c = server.BOT["config"]
    # emulate the clamp applied by the config endpoint
    import asyncio as _a
    res = _a.run(server.bot_config(server.BotConfigUpdate(futLongBufferPct=0.000000001)))
    assert res["config"]["futLongBufferPct"] == 1e-08, "floor is 0.00000001%"
    c.update({"futLongBufferPct": 0.0})


def test_no_duplicate_pending():
    _sim(futLongBufferPct=1.0)
    _open("long", "BTCUSDT")
    _open("long", "BTCUSDT")               # second signal must not stack a duplicate
    assert len(server.BOT["pendingFutures"]) == 1


def test_cleanup():
    server.BOT["positions"].clear()
    server.BOT["pendingFutures"].clear()
    server.BOT["config"].update({"enabled": False, "dryRun": True, "exchange": "hyperliquid",
                                  "futLongBufferPct": 0.0, "futShortBufferPct": 0.0})
