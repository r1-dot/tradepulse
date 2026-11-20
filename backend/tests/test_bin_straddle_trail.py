"""Smart Trailing TP INSIDE the Binance native straddle. When binStradTrailEnabled is ON, a filled
leg is managed as trailing TP/SL (fixed TP cancelled, native SL trails up); when OFF it stays a
fixed pre-placed TP/SL. Fully mocked — no real Binance calls."""
import asyncio
import server


def _reset():
    server.BOT["positions"].clear()
    server.STATE["latest"].clear()
    server.BOT["config"].update({
        "dryRun": False, "exchange": "binance_futures", "autoExit": True, "trailEnabled": True,
        "binStradTpPct": 0.5, "binStradSlPct": 0.3, "binStradTrailEnabled": False,
        "trailSecure": 0.40, "trailBE": 0.05, "trailStep": 0.5, "trailCallback": 0.30,
        "trailLock": 50, "hlStopSyncPct": 0.05,
    })


def _straddle(longTp=101.5, longSl=99.7):
    return {"symbol": "BTCUSDT", "base": "BTC", "longTp": longTp, "longSl": longSl,
            "longTpOid": "tp1", "longSlOid": "sl1", "shortTp": 98.5, "shortSl": 100.3,
            "shortTpOid": "tp2", "shortSlOid": "sl2"}


def test_fill_off_keeps_fixed_tpsl(monkeypatch):
    _reset()
    server.BOT["config"]["binStradTrailEnabled"] = False
    cancelled = []
    async def fake_cancel(http, sym, oid):
        cancelled.append(oid)
    monkeypatch.setattr(server, "_fapi_algo_cancel", fake_cancel)
    asyncio.run(server._register_bin_straddle_fill(None, _straddle(), "long", 100.0, 0.5, 1.0, True))
    pos = server.BOT["positions"]["BTCUSDT"]
    assert pos["tpPrice"] == 101.5 and pos["slPrice"] == 99.7  # fixed TP/SL retained
    assert not pos.get("trailManaged")
    assert cancelled == []  # nothing cancelled
    _reset()


def test_fill_on_cancels_tp_and_arms_trailing(monkeypatch):
    _reset()
    server.BOT["config"]["binStradTrailEnabled"] = True
    cancelled = []
    async def fake_cancel(http, sym, oid):
        cancelled.append(oid)
    monkeypatch.setattr(server, "_fapi_algo_cancel", fake_cancel)
    asyncio.run(server._register_bin_straddle_fill(None, _straddle(), "long", 100.0, 0.5, 1.0, True))
    pos = server.BOT["positions"]["BTCUSDT"]
    assert pos["tpPrice"] is None, "fixed TP removed so profit runs under the trail"
    assert pos["trailManaged"] is True
    assert pos["nativeSlPrice"] == 99.7 and pos["slOffPct"] is None and pos["peakPct"] == 0.0
    assert cancelled == ["tp1"], "pre-placed fixed TP cancelled on Binance"
    _reset()


def test_sim_fill_on_arms_trailing_without_cancel(monkeypatch):
    _reset()
    server.BOT["config"]["binStradTrailEnabled"] = True
    cancelled = []
    async def fake_cancel(http, sym, oid):
        cancelled.append(oid)
    monkeypatch.setattr(server, "_fapi_algo_cancel", fake_cancel)
    # live=False (SIM) -> no exchange cancel, but still trail-managed in software
    asyncio.run(server._register_bin_straddle_fill(None, _straddle(), "long", 100.0, 0.5, 1.0, False))
    pos = server.BOT["positions"]["BTCUSDT"]
    assert pos["trailManaged"] is True and pos["tpPrice"] is None
    assert cancelled == []  # SIM never touches the exchange
    _reset()


def test_live_straddle_trails_native_sl_up_without_software_close(monkeypatch):
    _reset()
    server.BOT["config"]["binStradTrailEnabled"] = True
    syncs, closes = [], []
    async def fake_sync(http, rec, sym, stop):
        syncs.append(stop)
        rec["nativeSlPrice"] = stop
    async def fake_close(http, pkey, price, reason):
        closes.append(reason)
    monkeypatch.setattr(server, "_bin_trail_sync_stop", fake_sync)
    monkeypatch.setattr(server, "_close_position", fake_close)
    pos = {"symbol": "BTCUSDT", "base": "BTC", "side": "long", "entryPrice": 100.0, "qty": 0.5,
           "source": "bin_straddle", "dryRun": False, "tpPrice": None, "slPrice": 99.7,
           "trailManaged": True, "peakPct": 0.0, "slOffPct": None, "nativeSlPrice": 99.7}
    server.BOT["positions"]["BTCUSDT"] = pos
    asyncio.run(server._update_trailing(None, "BTCUSDT", pos, 101.5))  # +1.5%
    assert pos["slOffPct"] is not None, "secured to BE+"
    assert syncs and syncs[-1] > 99.7, "native SL trailed UP on Binance"
    assert closes == [], "LIVE native straddle is never software-closed (exchange SL executes)"
    _reset()


def test_live_straddle_stop_hit_lets_exchange_close(monkeypatch):
    _reset()
    server.BOT["config"]["binStradTrailEnabled"] = True
    closes = []
    async def fake_sync(http, rec, sym, stop):
        rec["nativeSlPrice"] = stop
    async def fake_close(http, pkey, price, reason):
        closes.append(reason)
    monkeypatch.setattr(server, "_bin_trail_sync_stop", fake_sync)
    monkeypatch.setattr(server, "_close_position", fake_close)
    # secured, price falls back below the trailed stop -> still NO software close (native SL does it)
    pos = {"symbol": "BTCUSDT", "base": "BTC", "side": "long", "entryPrice": 100.0, "qty": 0.5,
           "source": "bin_straddle", "dryRun": False, "tpPrice": None, "slPrice": 100.8,
           "trailManaged": True, "peakPct": 2.0, "slOffPct": 1.0, "nativeSlPrice": 100.8}
    server.BOT["positions"]["BTCUSDT"] = pos
    asyncio.run(server._update_trailing(None, "BTCUSDT", pos, 100.0))
    assert closes == [], "exchange-native: software never closes a LIVE straddle"
    _reset()


def test_config_toggle_bin_strad_trail(monkeypatch):
    async def _noop():
        return None
    monkeypatch.setattr(server, "save_bot_config", _noop)
    asyncio.run(server.bot_config(server.BotConfigUpdate(binStradTrailEnabled=True)))
    assert server.BOT["config"]["binStradTrailEnabled"] is True
    asyncio.run(server.bot_config(server.BotConfigUpdate(binStradTrailEnabled=False)))
    assert server.BOT["config"]["binStradTrailEnabled"] is False
