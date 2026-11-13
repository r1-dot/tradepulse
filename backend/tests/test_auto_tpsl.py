"""Auto TP/SL: attach a reduce-only TP/SL to EVERY open Binance position that doesn't already have
one, reusing the straddle TP/SL levels. Leaves protected positions alone; never cancels/overrides;
skips the bot's own straddle positions. Fully mocked — no real Binance calls."""
import asyncio
import time
import server


def _reset():
    server.BOT["positions"].clear()
    server.BOT["autoTpSl"].clear()
    server.BOT["config"].update({
        "dryRun": False, "exchange": "binance_futures",
        "autoTpSlEnabled": True, "binStradTpPct": 0.2, "binStradSlPct": 0.08,
    })


def test_attaches_tpsl_to_unprotected_position(monkeypatch):
    _reset()
    placed = []

    async def fake_positions(http, sym=None):
        return [{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100.0",
                 "unRealizedProfit": "0", "positionSide": "BOTH"}]

    async def fake_protected(http, sym, closing, ps):
        return False

    async def fake_tpsl(http, sym, is_long, qty, tp, sl):
        placed.append((sym, is_long, qty, tp, sl))
        return {"tp": {"algoId": "t1"}, "sl": {"algoId": "s1"}}

    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    monkeypatch.setattr(server, "_bin_position_protected", fake_protected)
    monkeypatch.setattr(server, "_fapi_place_native_tpsl", fake_tpsl)
    asyncio.run(server.auto_tpsl_scan(None))
    assert len(placed) == 1
    sym, is_long, qty, tp, sl = placed[0]
    assert sym == "BTCUSDT" and is_long is True and abs(qty - 0.5) < 1e-9
    assert abs(tp - 100.0 * 1.002) < 1e-6   # +0.2% TP (long)
    assert abs(sl - 100.0 * 0.9992) < 1e-6  # -0.08% SL (long)
    rec = server.BOT["autoTpSl"]["BTCUSDT:BOTH:long"]
    assert rec["placed"] and rec["tpOid"] == "t1" and rec["slOid"] == "s1"
    _reset()


def test_short_direction_levels(monkeypatch):
    _reset()
    placed = []

    async def fake_positions(http, sym=None):
        return [{"symbol": "ETHUSDT", "positionAmt": "-2.0", "entryPrice": "200.0",
                 "unRealizedProfit": "0", "positionSide": "BOTH"}]

    async def fake_protected(http, sym, closing, ps):
        assert closing == "BUY"  # short closes with BUY
        return False

    async def fake_tpsl(http, sym, is_long, qty, tp, sl):
        placed.append((is_long, tp, sl))
        return {"tp": {"algoId": "t"}, "sl": {"algoId": "s"}}

    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    monkeypatch.setattr(server, "_bin_position_protected", fake_protected)
    monkeypatch.setattr(server, "_fapi_place_native_tpsl", fake_tpsl)
    asyncio.run(server.auto_tpsl_scan(None))
    assert placed and placed[0][0] is False
    assert abs(placed[0][1] - 200.0 * 0.998) < 1e-6   # SHORT TP below entry
    assert abs(placed[0][2] - 200.0 * 1.0008) < 1e-6  # SHORT SL above entry
    _reset()


def test_skips_already_protected_position(monkeypatch):
    _reset()
    placed = []

    async def fake_positions(http, sym=None):
        return [{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100.0",
                 "unRealizedProfit": "0", "positionSide": "BOTH"}]

    async def fake_protected(http, sym, closing, ps):
        return True  # already has a TP/SL -> leave alone

    async def fake_tpsl(http, *a, **k):
        placed.append(1)
        return {}

    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    monkeypatch.setattr(server, "_bin_position_protected", fake_protected)
    monkeypatch.setattr(server, "_fapi_place_native_tpsl", fake_tpsl)
    asyncio.run(server.auto_tpsl_scan(None))
    assert placed == [], "must NOT place TP/SL when protection already exists"
    assert server.BOT["autoTpSl"]["BTCUSDT:BOTH:long"].get("external") is True
    _reset()


def test_skips_bot_straddle_positions(monkeypatch):
    _reset()
    placed = []

    async def fake_positions(http, sym=None):
        return [{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100.0",
                 "unRealizedProfit": "0", "positionSide": "BOTH"}]

    async def fake_protected(http, sym, closing, ps):
        return False

    async def fake_tpsl(http, *a, **k):
        placed.append(1)
        return {}

    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    monkeypatch.setattr(server, "_bin_position_protected", fake_protected)
    monkeypatch.setattr(server, "_fapi_place_native_tpsl", fake_tpsl)
    server.BOT["positions"]["BTCUSDT"] = {"source": "bin_straddle", "side": "long"}
    asyncio.run(server.auto_tpsl_scan(None))
    assert placed == [], "straddle engine owns its own positions' TP/SL"
    assert "BTCUSDT:BOTH:long" not in server.BOT["autoTpSl"]
    _reset()


def test_does_not_replace_on_repeat_scan(monkeypatch):
    _reset()
    calls = {"n": 0}

    async def fake_positions(http, sym=None):
        return [{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100.0",
                 "unRealizedProfit": "0", "positionSide": "BOTH"}]

    async def fake_protected(http, sym, closing, ps):
        return False

    async def fake_tpsl(http, *a, **k):
        calls["n"] += 1
        return {"tp": {"algoId": "t"}, "sl": {"algoId": "s"}}

    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    monkeypatch.setattr(server, "_bin_position_protected", fake_protected)
    monkeypatch.setattr(server, "_fapi_place_native_tpsl", fake_tpsl)
    asyncio.run(server.auto_tpsl_scan(None))
    asyncio.run(server.auto_tpsl_scan(None))
    assert calls["n"] == 1, "already-handled position must not be re-placed on the next scan"
    _reset()


def test_cleanup_removes_closed_positions(monkeypatch):
    _reset()
    server.BOT["autoTpSl"]["OLDUSDT:BOTH:long"] = {"placed": True, "ts": time.time()}

    async def fake_positions(http, sym=None):
        return []  # nothing open anymore

    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    asyncio.run(server.auto_tpsl_scan(None))
    assert "OLDUSDT:BOTH:long" not in server.BOT["autoTpSl"], "closed positions must be forgotten"
    _reset()


def test_protected_detection_algo_reduceonly():
    _reset()

    class FR:
        status_code = 200

        def __init__(self, d):
            self._d = d

        def json(self):
            return self._d

    algo_rows = [{"orderType": "TAKE_PROFIT_MARKET", "reduceOnly": True, "algoStatus": "NEW",
                  "symbol": "BTCUSDT", "side": "SELL", "positionSide": "BOTH"}]

    async def fake_algo(http, sym):
        return algo_rows

    async def fake_open(http, sym):
        return []

    import types
    # patch module-level helpers used inside _bin_position_protected
    server._fapi_open_algo_orders = fake_algo
    server._fapi_open_orders = fake_open
    ok = asyncio.run(server._bin_position_protected(None, "BTCUSDT", "SELL", "BOTH"))
    assert ok is True

    # a non-protective reduce-only LIMIT must NOT count, and a non-reduceOnly stop (entry) must not
    algo_rows[:] = [{"orderType": "LIMIT", "reduceOnly": True, "algoStatus": "NEW", "symbol": "BTCUSDT", "side": "SELL"},
                    {"orderType": "STOP_MARKET", "reduceOnly": False, "algoStatus": "NEW", "symbol": "BTCUSDT", "side": "BUY"}]
    ok2 = asyncio.run(server._bin_position_protected(None, "BTCUSDT", "SELL", "BOTH"))
    assert ok2 is False
    _reset()


def test_config_toggle_auto_tpsl(monkeypatch):
    async def _noop():
        return None
    monkeypatch.setattr(server, "save_bot_config", _noop)
    asyncio.run(server.bot_config(server.BotConfigUpdate(autoTpSlEnabled=True)))
    assert server.BOT["config"]["autoTpSlEnabled"] is True
    asyncio.run(server.bot_config(server.BotConfigUpdate(autoTpSlEnabled=False)))
    assert server.BOT["config"]["autoTpSlEnabled"] is False
