"""Verify simulated (dryRun) Binance entries work for BOTH long and short, and that the
Leverage box now scales the simulated position size. No real orders."""
import asyncio
import server


def _setup(monkeypatch, lev=1):
    cfg = server.BOT["config"]
    cfg["dryRun"] = True
    cfg["exchange"] = "binance"
    cfg["maxPositionUsdt"] = 10.0
    cfg["hlLeverage"] = lev
    cfg["maxOpenPositions"] = 50
    cfg["dailyLossLimit"] = 1e9
    server.BOT["positions"].clear()
    server.STATE["latest"] = {"BTCUSDT": {"base": "BTC", "price": 100.0, "quote": "USDT"}}
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(server, "send_webhook", _noop)


def test_sim_binance_long_entry(monkeypatch):
    _setup(monkeypatch, lev=1)
    asyncio.run(server._open_position(None, "BTCUSDT", 100.0, 1000.0, side="long", source="signal"))
    p = server.BOT["positions"].get("BTCUSDT")
    assert p and p["side"] == "long"
    assert abs(p["qty"] - (10.0 / 100.0)) < 1e-9  # 1x -> notional/price


def test_sim_binance_short_entry_allowed(monkeypatch):
    _setup(monkeypatch, lev=1)
    asyncio.run(server._open_position(None, "BTCUSDT", 100.0, 1000.0, side="short", source="signal"))
    p = server.BOT["positions"].get("BTCUSDT")
    assert p and p["side"] == "short", "SHORT must open in SIM on Binance (not rejected)"


def test_sim_leverage_scales_size(monkeypatch):
    _setup(monkeypatch, lev=5)
    asyncio.run(server._open_position(None, "BTCUSDT", 100.0, 1000.0, side="short", source="signal"))
    p = server.BOT["positions"]["BTCUSDT"]
    # 5x leverage -> 5 * (10/100) = 0.5
    assert abs(p["qty"] - 0.5) < 1e-9, f"leverage should scale sim size, got {p['qty']}"
    assert abs(p["quoteSpent"] - 50.0) < 1e-9
