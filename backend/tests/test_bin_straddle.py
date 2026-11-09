"""Binance NATIVE straddle engine (Dec-2025 Algo Service migration): on a bot signal, place real
Binance USD-M Futures CONDITIONAL algo stop-entry orders (/fapi/v1/algoOrder, algoType=CONDITIONAL,
triggerPrice, workingType=MARK_PRICE) — a LONG stop above + SHORT stop below. Whichever triggers
first keeps running; the opposite leg is cancelled via DELETE /fapi/v1/algoOrder (OCO). The filled
leg is closed by native reduce-only TP/SL algo orders (quantity + reduceOnly, NOT closePosition).
Fully mocked with a fake httpx client — NO real Binance calls."""
import asyncio
import re
import time
import server


class FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = str(data)
        self.headers = {}

    def json(self):
        if isinstance(self._data, str):
            raise ValueError("not json")
        return self._data


class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _match(self, url):
        for sub, resp in self.routes:
            if sub in url:
                return FakeResp(resp)
        return FakeResp({"code": -1, "msg": "unmatched"}, status=400)

    async def request(self, method, url, headers=None, timeout=None, follow_redirects=False):
        self.calls.append((method, url))
        return self._match(url)

    async def get(self, url, headers=None, timeout=None, follow_redirects=False):
        self.calls.append(("GET", url))
        return self._match(url)


def _reset():
    server._FAPI["mode"] = None
    server._FAPI["modeTs"] = 0.0
    server._FAPI["lots"] = {}
    server._FAPI["lotsTs"] = 0.0
    server._FAPI["activeHost"] = None
    server.BOT["positions"].clear()
    server.BOT["binStraddles"].clear()
    server.BOT["straddles"].clear()
    server.BOT["lastTradeTs"].clear()


def _cfg(**over):
    cfg = server.BOT["config"]
    cfg.update({
        "enabled": True, "dryRun": False, "exchange": "binance_futures",
        "binStradEnabled": True, "binStradEntryPct": 0.08,
        "binStradTpPct": 0.5, "binStradSlPct": 0.3,
        "straddleEnabled": False, "cooldownSec": 0, "maxOpenPositions": 50,
        "minVolumeUsd": 0.0, "maxVolumeUsd": 0.0, "tradeAmtEnabled": False,
        "maxPositionUsdt": 100.0, "futuresLeverage": 3, "dailyLossLimit": 1e9,
    })
    cfg.update(over)
    return cfg


def _sp(u):
    m = re.search(r"triggerPrice=([0-9.]+)", u)
    return float(m.group(1)) if m else None


def test_arm_places_two_conditional_algo_stop_entries(monkeypatch):
    _reset()
    _cfg()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    server.STATE["latest"] = {"BTCUSDT": {"base": "BTC", "price": 100.0, "quoteVol": 1e6, "quote": "USDT"}}
    http = FakeHTTP([
        ("/fapi/v1/positionSide/dual", {"dualSidePosition": False}),
        ("/fapi/v1/leverage", {"leverage": 3}),
        ("/fapi/v1/exchangeInfo", {"symbols": []}),
        ("/fapi/v1/algoOrder", {"algoId": 111, "algoStatus": "NEW"}),
    ])
    asyncio.run(server.handle_signal(http, "BTCUSDT", "buy", 100.0, time.time(), volume=1e6))
    s = server.BOT["binStraddles"].get("BTCUSDT")
    assert s is not None and s["live"] is True
    assert s["longOid"] == 111 and s["shortOid"] == 111
    order_urls = [u for _, u in http.calls if "/fapi/v1/algoOrder" in u]
    assert len(order_urls) == 2, order_urls
    joined = " ".join(order_urls)
    # new Algo Service contract
    assert "algoType=CONDITIONAL" in joined
    assert "type=STOP_MARKET" in joined
    assert "triggerPrice=" in joined and "stopPrice=" not in joined
    assert "workingType=MARK_PRICE" in joined and "triggerType=" not in joined
    assert "timeInForce" not in joined
    # placed on the algo endpoint, never the legacy order endpoint
    assert not any(u.endswith("/fapi/v1/order") or "/fapi/v1/order?" in u for _, u in http.calls)
    buy = [u for u in order_urls if "side=BUY" in u]
    sell = [u for u in order_urls if "side=SELL" in u]
    assert buy and sell
    assert abs(_sp(buy[0]) - 100.08) < 1e-3 and _sp(buy[0]) > 100.0   # LONG above
    assert abs(_sp(sell[0]) - 99.92) < 1e-3 and _sp(sell[0]) < 100.0   # SHORT below
    _cfg(dryRun=True, exchange="binance", binStradEnabled=False)


def test_native_tpsl_uses_reduceonly_quantity_not_closeposition(monkeypatch):
    _reset()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    http = FakeHTTP([
        ("/fapi/v1/positionSide/dual", {"dualSidePosition": False}),
        ("/fapi/v1/exchangeInfo", {"symbols": []}),
        ("/fapi/v1/algoOrder", {"algoId": 7}),
    ])
    out = asyncio.run(server._fapi_place_native_tpsl(http, "BTCUSDT", True, 0.5, 105.0, 98.0))
    order_urls = [u for _, u in http.calls if "/fapi/v1/algoOrder" in u]
    assert len(order_urls) == 2
    joined = " ".join(order_urls)
    assert "type=TAKE_PROFIT_MARKET" in joined and "type=STOP_MARKET" in joined
    assert "algoType=CONDITIONAL" in joined and "triggerPrice=" in joined
    # closing a LONG -> SELL triggers, reduce-only + quantity (Dec-2025: closePosition dropped)
    assert all("side=SELL" in u for u in order_urls)
    assert "closePosition" not in joined
    assert "reduceOnly=true" in joined and "quantity=0.5" in joined
    assert out["tp"]["algoId"] == 7 and out["sl"]["algoId"] == 7


def test_live_trigger_cancels_opposite_and_places_native_tpsl(monkeypatch):
    _reset()
    _cfg()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    cancels = []
    tpsl_calls = []

    async def fake_status(http, sym, oid):
        return {"algoStatus": "FINISHED", "actualPrice": "100.08", "actualQty": "1.0"} if oid == "L1" \
            else {"algoStatus": "NEW"}

    async def fake_cancel(http, sym, oid):
        cancels.append(oid)

    async def fake_positions(http, sym=None):
        return []  # no refinement -> keep algo fill price/qty

    async def fake_tpsl(http, sym, is_long, qty, tp, sl):
        tpsl_calls.append((is_long, qty, tp, sl))
        return {"tp": {"algoId": "t1"}, "sl": {"algoId": "s1"}}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(server, "_fapi_algo_status", fake_status)
    monkeypatch.setattr(server, "_fapi_algo_cancel", fake_cancel)
    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    monkeypatch.setattr(server, "_fapi_place_native_tpsl", fake_tpsl)
    monkeypatch.setattr(server, "send_webhook", noop)

    now = time.time()
    server.BOT["binStraddles"]["BTCUSDT"] = {
        "symbol": "BTCUSDT", "base": "BTC", "mark": 100.0, "qty": 1.0, "lev": 3,
        "longStop": 100.08, "shortStop": 99.92, "longOid": "L1", "shortOid": "S1",
        "createdTs": now, "live": True, "lastPollTs": 0.0,
    }
    asyncio.run(server._reconcile_bin_arms(None, now + 3))
    assert "S1" in cancels, "opposite (SHORT) leg must be cancelled on LONG trigger (OCO)"
    assert "BTCUSDT" not in server.BOT["binStraddles"]
    pos = server.BOT["positions"]["BTCUSDT"]
    assert pos["source"] == "bin_straddle" and pos["side"] == "long"
    assert abs(pos["entryPrice"] - 100.08) < 1e-9
    # native TP/SL placed with the real fill qty + dedicated levels (LONG: tp above, sl below)
    assert tpsl_calls and tpsl_calls[0][0] is True and abs(tpsl_calls[0][1] - 1.0) < 1e-9
    assert abs(pos["tpPrice"] - 100.08 * 1.005) < 1e-6
    assert abs(pos["slPrice"] - 100.08 * 0.997) < 1e-6
    assert pos["binTpOid"] == "t1" and pos["binSlOid"] == "s1"
    _reset()
    _cfg(dryRun=True, exchange="binance", binStradEnabled=False)


def test_live_fill_price_refined_from_position_risk(monkeypatch):
    _reset()
    _cfg()

    async def fake_status(http, sym, oid):
        return {"algoStatus": "TRIGGERED", "actualPrice": "0", "actualQty": "0"} if oid == "S9" \
            else {"algoStatus": "NEW"}

    async def fake_cancel(http, sym, oid):
        return None

    async def fake_positions(http, sym=None):
        return [{"positionAmt": "-0.37", "entryPrice": "99.915"}]

    async def fake_tpsl(http, sym, is_long, qty, tp, sl):
        return {"tp": {"algoId": "t"}, "sl": {"algoId": "s"}}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(server, "_fapi_algo_status", fake_status)
    monkeypatch.setattr(server, "_fapi_algo_cancel", fake_cancel)
    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    monkeypatch.setattr(server, "_fapi_place_native_tpsl", fake_tpsl)
    monkeypatch.setattr(server, "send_webhook", noop)

    now = time.time()
    server.BOT["binStraddles"]["SOLUSDT"] = {
        "symbol": "SOLUSDT", "base": "SOL", "mark": 100.0, "qty": 0.4, "lev": 3,
        "longStop": 100.08, "shortStop": 99.92, "longOid": "L9", "shortOid": "S9",
        "createdTs": now, "live": True, "lastPollTs": 0.0,
    }
    asyncio.run(server._reconcile_bin_arms(None, now + 3))
    pos = server.BOT["positions"]["SOLUSDT"]
    assert pos["side"] == "short"
    assert abs(pos["entryPrice"] - 99.915) < 1e-9   # real avg entry from positionRisk
    assert abs(pos["qty"] - 0.37) < 1e-9             # real size from positionRisk
    _reset()
    _cfg(dryRun=True, exchange="binance", binStradEnabled=False)


def test_sim_arm_and_fill_no_real_orders(monkeypatch):
    _reset()
    _cfg(dryRun=True)

    async def boom(*a, **k):
        raise AssertionError("real futures API must NOT be called in SIM")

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(server, "_fapi_place_stop_entry", boom)
    monkeypatch.setattr(server, "_fapi_place_native_tpsl", boom)
    monkeypatch.setattr(server, "send_webhook", noop)
    server.STATE["latest"] = {"ETHUSDT": {"base": "ETH", "price": 200.0, "quoteVol": 1e6, "quote": "USDT"}}

    now = time.time()
    asyncio.run(server.handle_signal(None, "ETHUSDT", "buy", 200.0, now, volume=1e6))
    s = server.BOT["binStraddles"]["ETHUSDT"]
    assert s["live"] is False and abs(s["longStop"] - 200.16) < 1e-6
    server.STATE["latest"]["ETHUSDT"]["price"] = 201.0
    asyncio.run(server._process_bin_straddles(None, now + 1))
    assert "ETHUSDT" not in server.BOT["binStraddles"]
    pos = server.BOT["positions"]["ETHUSDT"]
    assert pos["side"] == "long" and pos["dryRun"] is True and pos["source"] == "bin_straddle"
    assert abs(pos["qty"] - (100.0 / 200.16) * 3) < 1e-6   # SIM qty scaled by leverage
    _reset()
    _cfg(dryRun=True, exchange="binance", binStradEnabled=False)


def test_short_fill_tp_below_sl_above(monkeypatch):
    _reset()
    _cfg(dryRun=True)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(server, "send_webhook", noop)
    now = time.time()
    s = {"symbol": "SOLUSDT", "base": "SOL", "mark": 100.0, "qty": 1.0, "lev": 1,
         "longStop": 100.08, "shortStop": 99.92, "longOid": None, "shortOid": None,
         "createdTs": now, "live": False, "lastPollTs": 0.0}
    asyncio.run(server._register_bin_straddle_fill(None, s, "short", 99.92, 1.0, now, live=False))
    pos = server.BOT["positions"]["SOLUSDT"]
    assert pos["side"] == "short"
    assert abs(pos["tpPrice"] - 99.92 * 0.995) < 1e-6  # SHORT TP below entry
    assert abs(pos["slPrice"] - 99.92 * 1.003) < 1e-6  # SHORT SL above entry
    _reset()


def test_live_arm_expiry_cancels_both_algo_legs(monkeypatch):
    _reset()
    _cfg()
    cancels = []

    async def fake_cancel(http, sym, oid):
        cancels.append(oid)

    monkeypatch.setattr(server, "_fapi_algo_cancel", fake_cancel)
    now = time.time()
    server.BOT["binStraddles"]["BTCUSDT"] = {
        "symbol": "BTCUSDT", "base": "BTC", "mark": 100.0, "qty": 1.0, "lev": 3,
        "longStop": 100.08, "shortStop": 99.92, "longOid": "L1", "shortOid": "S1",
        "createdTs": now - (server.STRADDLE_EXPIRY + 5), "live": True, "lastPollTs": 0.0,
    }
    asyncio.run(server._reconcile_bin_arms(None, now))
    assert "BTCUSDT" not in server.BOT["binStraddles"]
    assert set(cancels) == {"L1", "S1"}, "both resting algo legs must be cancelled on expiry"
    _reset()
    _cfg(dryRun=True, exchange="binance", binStradEnabled=False)


def test_reconcile_detects_exchange_close_by_tp(monkeypatch):
    _reset()
    server.BOT["wins"] = 0
    server.BOT["losses"] = 0
    server.BOT["dailyPnl"] = 0.0
    cancels = []

    async def fake_positions(http, sym=None):
        return []  # FLAT on exchange -> closed

    async def fake_cancel(http, sym, oid):
        cancels.append(oid)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    monkeypatch.setattr(server, "_fapi_algo_cancel", fake_cancel)
    monkeypatch.setattr(server, "send_webhook", noop)
    server.STATE["latest"] = {"BTCUSDT": {"base": "BTC", "price": 100.6, "quote": "USDT"}}
    pos = {"symbol": "BTCUSDT", "base": "BTC", "side": "long", "entryPrice": 100.0, "qty": 1.0,
           "quoteSpent": 100.0, "tpPrice": 100.5, "slPrice": 99.7, "openedTs": time.time(),
           "dryRun": False, "source": "bin_straddle", "binTpOid": "t", "binSlOid": "s"}
    server.BOT["positions"]["BTCUSDT"] = pos
    asyncio.run(server._reconcile_bin_position(None, "BTCUSDT", pos, time.time()))
    assert "BTCUSDT" not in server.BOT["positions"], "closed position must be removed (no ghost)"
    assert server.BOT["wins"] == 1 and server.BOT["losses"] == 0
    assert abs(server.BOT["dailyPnl"] - 0.5) < 1e-9     # (tp 100.5 - entry 100) * qty 1
    assert set(cancels) == {"t", "s"}, "leftover native TP/SL algo orders must be cancelled"
    _reset()


def test_reconcile_detects_exchange_close_by_sl(monkeypatch):
    _reset()
    server.BOT["wins"] = 0
    server.BOT["losses"] = 0
    server.BOT["dailyPnl"] = 0.0

    async def fake_positions(http, sym=None):
        return []

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    monkeypatch.setattr(server, "_fapi_algo_cancel", noop)
    monkeypatch.setattr(server, "send_webhook", noop)
    server.STATE["latest"] = {"ETHUSDT": {"base": "ETH", "price": 99.6, "quote": "USDT"}}
    pos = {"symbol": "ETHUSDT", "base": "ETH", "side": "long", "entryPrice": 100.0, "qty": 2.0,
           "quoteSpent": 200.0, "tpPrice": 100.5, "slPrice": 99.7, "openedTs": time.time(),
           "dryRun": False, "source": "bin_straddle"}
    server.BOT["positions"]["ETHUSDT"] = pos
    asyncio.run(server._reconcile_bin_position(None, "ETHUSDT", pos, time.time()))
    assert "ETHUSDT" not in server.BOT["positions"]
    assert server.BOT["losses"] == 1 and server.BOT["wins"] == 0
    assert abs(server.BOT["dailyPnl"] - ((99.7 - 100.0) * 2.0)) < 1e-9   # SL hit
    _reset()


def test_reconcile_open_position_snapshots_live_state(monkeypatch):
    _reset()

    async def fake_positions(http, sym=None):
        return [{"positionAmt": "1.0", "entryPrice": "100.0", "unRealizedProfit": "0.4"}]

    monkeypatch.setattr(server, "_fapi_positions", fake_positions)
    pos = {"symbol": "BTCUSDT", "base": "BTC", "side": "long", "entryPrice": 100.0, "qty": 1.0,
           "quoteSpent": 100.0, "tpPrice": 100.5, "slPrice": 99.7, "openedTs": time.time(),
           "dryRun": False, "source": "bin_straddle"}
    server.BOT["positions"]["BTCUSDT"] = pos
    asyncio.run(server._reconcile_bin_position(None, "BTCUSDT", pos, time.time()))
    assert "BTCUSDT" in server.BOT["positions"], "open position must stay tracked"
    assert pos["liveState"] == "open"
    assert abs(pos["exUpnl"] - 0.4) < 1e-9 and abs(pos["exQty"] - 1.0) < 1e-9
    _reset()


def test_config_clamps_bin_straddle(monkeypatch):
    async def _noop():
        return None
    monkeypatch.setattr(server, "save_bot_config", _noop)

    async def run():
        return await server.bot_config(server.BotConfigUpdate(
            binStradEntryPct=99, binStradTpPct=0.0001, binStradSlPct=99, binStradMonitorMs=99999))
    asyncio.run(run())
    cfg = server.BOT["config"]
    assert cfg["binStradEntryPct"] == 5.0
    assert cfg["binStradTpPct"] == 0.01
    assert cfg["binStradSlPct"] == 5.0
    assert cfg["binStradMonitorMs"] == 5000
    asyncio.run(server.bot_config(server.BotConfigUpdate(binStradEnabled=True)))
    assert server.BOT["config"]["binStradEnabled"] is True
    asyncio.run(server.bot_config(server.BotConfigUpdate(binStradEnabled=False)))
