"""Smart Trailing TP · ALL POSITIONS — applies the trailing engine to every open Binance position
(incl. manual), takes over existing TP/SL, mirrors the moving stop as a native SL, and takes
PRIORITY over the fixed Auto TP/SL. Fully mocked — no real Binance calls."""
import asyncio
import time
import server
import hybrid_power as hybrid


def _reset():
    server.BOT["positions"].clear()
    server.BOT["autoTpSl"].clear()
    server.BOT["autoTrail"].clear()
    server.STATE["latest"].clear()
    server.BOT["config"].update({
        "dryRun": False, "exchange": "binance_futures", "autoTrailEnabled": True,
        "trailInitialSlPct": 0.30, "trailSecure": 0.40, "trailBE": 0.05, "trailStep": 0.5,
        "trailCallback": 0.30, "trailLock": 50, "hlStopSyncPct": 0.05,
    })


def _patch(monkeypatch, positions, placed, cancels, closes):
    async def fake_positions(http, sym=None):
        return positions

    async def fake_cancel(http, sym, closing, ps):
        cancels.append((sym, closing, ps))
        return 1

    async def fake_tpsl(http, sym, is_long, qty, tp, sl, reduce_only=True):
        placed.append({"sym": sym, "is_long": is_long, "qty": qty, "tp": tp, "sl": sl})
        return {"sl": {"algoId": "s" + str(len(placed))}}

    async def fake_close(http, sym, is_long, qty):
        closes.append((sym, is_long, qty))
        return {"fillPrice": 0.0, "orderId": "c1"}

    async def fake_last(http, sym):
        return 0.0

    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    monkeypatch.setattr(server, "_bin_cancel_tpsl", fake_cancel)
    monkeypatch.setattr(server, "_fapi_place_native_tpsl", fake_tpsl)
    monkeypatch.setattr(server, "_fapi_market_close", fake_close)
    monkeypatch.setattr(server, "_fapi_last_price", fake_last)


def test_adopts_and_takes_over_unprotected_or_protected(monkeypatch):
    _reset()
    placed, cancels, closes = [], [], []
    pos = [{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100.0", "positionSide": "BOTH"}]
    _patch(monkeypatch, pos, placed, cancels, closes)
    server.STATE["latest"]["BTCUSDT"] = {"price": 100.0}  # flat -> no profit, stays on init SL
    asyncio.run(server.auto_trail_scan(None))
    # TOOK OVER: existing TP/SL cancelled, then ONE init SL placed (no TP)
    assert cancels and cancels[0][0] == "BTCUSDT"
    assert len(placed) == 1 and placed[0]["tp"] is None
    assert abs(placed[0]["sl"] - 100.0 * (1 - 0.003)) < 1e-6   # -0.30% initial stop (long)
    rec = server.BOT["autoTrail"]["BTCUSDT:BOTH:long"]
    assert rec["slOffPct"] is None and abs(rec["nativeSlPrice"] - 99.7) < 1e-6
    _reset()


def test_trails_stop_up_and_syncs_native(monkeypatch):
    _reset()
    placed, cancels, closes = [], [], []
    pos = [{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100.0", "positionSide": "BOTH"}]
    _patch(monkeypatch, pos, placed, cancels, closes)
    # pre-seed an already-adopted record sitting on the initial stop
    server.BOT["autoTrail"]["BTCUSDT:BOTH:long"] = {
        "entry": 100.0, "qty": 0.5, "side": "long", "ps": "BOTH", "base": "BTC",
        "peakPct": 0.0, "slOffPct": None, "nativeSlPrice": 99.7, "slOid": "s0", "ts": time.time()}
    server.STATE["latest"]["BTCUSDT"] = {"price": 101.5}  # +1.5% -> should secure + trail up
    asyncio.run(server.auto_trail_scan(None))
    rec = server.BOT["autoTrail"]["BTCUSDT:BOTH:long"]
    assert rec["slOffPct"] is not None, "should have secured to BE+"
    assert rec["nativeSlPrice"] > 99.7, "native SL must trail UP into profit"
    # a native cancel-replace happened (one cancel + one new SL place during sync)
    assert cancels and placed and placed[-1]["tp"] is None
    assert abs(placed[-1]["sl"] - rec["nativeSlPrice"]) < 1e-6
    _reset()


def test_trail_exit_closes_position(monkeypatch):
    _reset()
    placed, cancels, closes = [], [], []
    pos = [{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100.0", "positionSide": "BOTH"}]
    _patch(monkeypatch, pos, placed, cancels, closes)
    server.BOT["autoTrail"]["BTCUSDT:BOTH:long"] = {
        "entry": 100.0, "qty": 0.5, "side": "long", "ps": "BOTH", "base": "BTC",
        "peakPct": 2.0, "slOffPct": 1.0, "nativeSlPrice": 101.0, "slOid": "s0", "ts": time.time()}
    server.STATE["latest"]["BTCUSDT"] = {"price": 100.0}  # fell back below the trailed stop
    asyncio.run(server.auto_trail_scan(None))
    assert closes == [("BTCUSDT", True, 0.5)], "trailed stop hit -> market close"
    assert "BTCUSDT:BOTH:long" not in server.BOT["autoTrail"], "closed position forgotten"
    _reset()


def test_short_initial_stop_is_above_entry(monkeypatch):
    _reset()
    placed, cancels, closes = [], [], []
    pos = [{"symbol": "ETHUSDT", "positionAmt": "-2.0", "entryPrice": "200.0", "positionSide": "BOTH"}]
    _patch(monkeypatch, pos, placed, cancels, closes)
    server.STATE["latest"]["ETHUSDT"] = {"price": 200.0}
    asyncio.run(server.auto_trail_scan(None))
    assert placed and placed[0]["is_long"] is False
    assert abs(placed[0]["sl"] - 200.0 * (1 + 0.003)) < 1e-6   # +0.30% initial stop (short)
    _reset()


def test_skips_bot_owned_positions(monkeypatch):
    _reset()
    placed, cancels, closes = [], [], []
    pos = [{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100.0", "positionSide": "BOTH"}]
    _patch(monkeypatch, pos, placed, cancels, closes)
    server.BOT["positions"]["BTCUSDT"] = {"source": "bin_straddle", "side": "long"}
    server.STATE["latest"]["BTCUSDT"] = {"price": 100.0}
    asyncio.run(server.auto_trail_scan(None))
    assert placed == [] and "BTCUSDT:BOTH:long" not in server.BOT["autoTrail"]
    _reset()


def test_cleanup_forgets_closed_positions(monkeypatch):
    _reset()
    placed, cancels, closes = [], [], []
    _patch(monkeypatch, [], placed, cancels, closes)  # nothing open
    server.BOT["autoTrail"]["OLDUSDT:BOTH:long"] = {"entry": 1.0, "qty": 1, "side": "long"}
    asyncio.run(server.auto_trail_scan(None))
    assert "OLDUSDT:BOTH:long" not in server.BOT["autoTrail"]
    _reset()


def test_trailing_takes_priority_over_fixed_autotpsl(monkeypatch):
    _reset()
    server.BOT["config"]["autoTpSlEnabled"] = True
    server.BOT["config"]["binStradTpPct"] = 0.2
    server.BOT["config"]["binStradSlPct"] = 0.08
    placed = []

    async def fake_positions(http, sym=None):
        return [{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100.0", "positionSide": "BOTH"}]

    async def fake_protected(http, sym, closing, ps):
        return False

    async def fake_tpsl(http, *a, **k):
        placed.append(1)
        return {"tp": {"algoId": "t"}, "sl": {"algoId": "s"}}

    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    monkeypatch.setattr(server, "_bin_position_protected", fake_protected)
    monkeypatch.setattr(server, "_fapi_place_native_tpsl", fake_tpsl)
    # trailing already owns this position -> fixed Auto TP/SL must skip it
    server.BOT["autoTrail"]["BTCUSDT:BOTH:long"] = {"entry": 100.0, "qty": 0.5, "side": "long"}
    asyncio.run(server.auto_tpsl_scan(None))
    assert placed == [], "fixed Auto TP/SL must not touch a position owned by Smart Trailing"
    assert "BTCUSDT:BOTH:long" not in server.BOT["autoTpSl"]
    _reset()


def test_config_toggle_auto_trail(monkeypatch):
    async def _noop():
        return None
    monkeypatch.setattr(server, "save_bot_config", _noop)
    asyncio.run(server.bot_config(server.BotConfigUpdate(autoTrailEnabled=True)))
    assert server.BOT["config"]["autoTrailEnabled"] is True
    asyncio.run(server.bot_config(server.BotConfigUpdate(autoTrailEnabled=False)))
    assert server.BOT["config"]["autoTrailEnabled"] is False
