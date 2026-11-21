"""CHECKPOINT — holds each bot signal before the Native Straddle, averages Buy/Sell pressure over
the hold window, then arms ONLY the confirmed side (Buy->long, Sell->short) or skips/auto-cancels.
Fully mocked (hybrid.get_signal + _arm_bin_straddle patched) — no real Binance calls."""
import asyncio
import time
import server
import hybrid_power as hybrid


def _reset():
    server.BOT["positions"].clear()
    server.BOT["binStraddles"].clear()
    server.BOT["straddles"].clear()
    server.BOT["checkpoints"].clear()
    server.BOT["lastTradeTs"].clear()
    server.BOT["stopped"] = False
    server.STATE["latest"].clear()
    server.STATE["latest"]["BTCUSDT"] = {"price": 100.0, "quoteVol": 5_000_000, "count24h": 1000}
    server.BOT["config"].update({
        "enabled": True, "dryRun": True, "exchange": "binance_futures", "binStradEnabled": True,
        "checkpointEnabled": True, "checkpointHoldSec": 20, "checkpointBuyPct": 70,
        "checkpointSellPct": 70, "cooldownSec": 0, "maxOpenPositions": 10,
        "minVolumeUsd": 0, "maxVolumeUsd": 0, "tradeAmtEnabled": False,
    })


def _patch_arm(monkeypatch):
    armed = []
    async def fake_arm(http, sym, now, sides):
        armed.append((sym, tuple(sides)))
    monkeypatch.setattr(server, "_arm_bin_straddle", fake_arm)
    return armed


def _patch_pressure(monkeypatch, buy_pct):
    def fake_sig(base, qv, params, now):
        return {"buy": buy_pct, "signal": None, "power_1m": 0.0}
    monkeypatch.setattr(hybrid, "get_signal", fake_sig)
    monkeypatch.setattr(hybrid, "update", lambda *a, **k: None)
    monkeypatch.setattr(server, "compute_trades", lambda n: ({"BTCUSDT": {"trades": 5, "side": "buy"}}, False))


def test_signal_creates_hold_not_immediate_arm(monkeypatch):
    _reset()
    armed = _patch_arm(monkeypatch)
    asyncio.run(server.handle_signal(None, "BTCUSDT", "buy", 100.0, time.time(), 0.0, 0.0))
    assert "BTCUSDT" in server.BOT["checkpoints"], "signal held, not armed immediately"
    assert armed == []
    cp = server.BOT["checkpoints"]["BTCUSDT"]
    assert cp["holdSec"] == 20 and cp["samples"] == []
    _reset()


def test_checkpoint_off_arms_both_sides_immediately(monkeypatch):
    _reset()
    server.BOT["config"]["checkpointEnabled"] = False
    armed = _patch_arm(monkeypatch)
    asyncio.run(server.handle_signal(None, "BTCUSDT", "buy", 100.0, time.time(), 0.0, 0.0))
    assert server.BOT["checkpoints"] == {}
    assert armed == [("BTCUSDT", ("long", "short"))]
    _reset()


def test_buy_dominant_arms_long_only(monkeypatch):
    _reset()
    armed = _patch_arm(monkeypatch)
    _patch_pressure(monkeypatch, buy_pct=80)  # 80% buy >= 70% -> BUY/long
    now = time.time()
    server.BOT["checkpoints"]["BTCUSDT"] = {"base": "BTC", "start": now - 25, "deadline": now - 1,
        "holdSec": 20, "samples": [], "lastSampleTs": 0.0, "lastBuy": None, "price": 100.0}
    asyncio.run(server._process_checkpoints(None, now))
    assert armed == [("BTCUSDT", ("long",))]
    assert "BTCUSDT" not in server.BOT["checkpoints"]
    _reset()


def test_sell_dominant_arms_short_only(monkeypatch):
    _reset()
    armed = _patch_arm(monkeypatch)
    _patch_pressure(monkeypatch, buy_pct=25)  # 25% buy -> 75% sell >= 70% -> SELL/short
    now = time.time()
    server.BOT["checkpoints"]["BTCUSDT"] = {"base": "BTC", "start": now - 25, "deadline": now - 1,
        "holdSec": 20, "samples": [], "lastSampleTs": 0.0, "lastBuy": None, "price": 100.0}
    asyncio.run(server._process_checkpoints(None, now))
    assert armed == [("BTCUSDT", ("short",))]
    _reset()


def test_weak_pressure_skips_and_autocancels(monkeypatch):
    _reset()
    armed = _patch_arm(monkeypatch)
    _patch_pressure(monkeypatch, buy_pct=55)  # 55% buy / 45% sell -> neither >= 70% -> SKIP
    now = time.time()
    server.BOT["checkpoints"]["BTCUSDT"] = {"base": "BTC", "start": now - 25, "deadline": now - 1,
        "holdSec": 20, "samples": [], "lastSampleTs": 0.0, "lastBuy": None, "price": 100.0}
    asyncio.run(server._process_checkpoints(None, now))
    assert armed == [], "weak pressure must not arm"
    assert "BTCUSDT" not in server.BOT["checkpoints"], "auto-cancelled, never left pending"
    jmsgs = [j.get("message", "") for j in server.BOT["journal"]]
    assert any("CHECKPOINT SKIP BTC" in m for m in jmsgs)
    _reset()


def test_samples_averaged_over_window(monkeypatch):
    _reset()
    armed = _patch_arm(monkeypatch)
    now = time.time()
    # pre-seeded samples average to 72% -> passes the 70% buy threshold even though last reading dips
    server.BOT["checkpoints"]["BTCUSDT"] = {"base": "BTC", "start": now - 25, "deadline": now - 0.5,
        "holdSec": 20, "samples": [75.0, 73.0, 68.0], "lastSampleTs": now, "lastBuy": 68.0, "price": 100.0}
    # no new sample added (deadline passed); avg = 72.0
    monkeypatch.setattr(server, "compute_trades", lambda n: ({}, False))
    asyncio.run(server._process_checkpoints(None, now))
    assert armed == [("BTCUSDT", ("long",))]
    _reset()


def test_held_symbol_not_rearmed_by_new_signal(monkeypatch):
    _reset()
    armed = _patch_arm(monkeypatch)
    now = time.time()
    server.BOT["checkpoints"]["BTCUSDT"] = {"base": "BTC", "start": now, "deadline": now + 20,
        "holdSec": 20, "samples": [], "lastSampleTs": 0.0, "lastBuy": None, "price": 100.0}
    asyncio.run(server.handle_signal(None, "BTCUSDT", "buy", 100.0, now, 0.0, 0.0))
    assert armed == [] and len(server.BOT["checkpoints"]) == 1  # unchanged, no duplicate hold/arm
    _reset()


def test_system_off_clears_holds(monkeypatch):
    _reset()
    server.BOT["checkpoints"]["BTCUSDT"] = {"base": "BTC", "start": 0, "deadline": 0,
        "holdSec": 20, "samples": [], "lastSampleTs": 0.0, "lastBuy": None, "price": 100.0}
    server.BOT["config"]["checkpointEnabled"] = False
    asyncio.run(server._process_checkpoints(None, time.time()))
    assert server.BOT["checkpoints"] == {}
    _reset()


def test_config_toggle_and_clamps(monkeypatch):
    async def _noop():
        return None
    monkeypatch.setattr(server, "save_bot_config", _noop)
    asyncio.run(server.bot_config(server.BotConfigUpdate(
        checkpointEnabled=True, checkpointHoldSec=60, checkpointBuyPct=90, checkpointSellPct=60)))
    c = server.BOT["config"]
    assert c["checkpointEnabled"] is True and c["checkpointHoldSec"] == 60
    assert c["checkpointBuyPct"] == 90 and c["checkpointSellPct"] == 60
    # clamps
    asyncio.run(server.bot_config(server.BotConfigUpdate(checkpointBuyPct=150, checkpointHoldSec=99999)))
    assert server.BOT["config"]["checkpointBuyPct"] == 99.0
    assert server.BOT["config"]["checkpointHoldSec"] == 600
