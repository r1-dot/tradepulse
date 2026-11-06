"""Binance NATIVE straddle engine: on a bot signal, place real Binance USD-M Futures stop-entry
orders (LONG stop above + SHORT stop below), cancel the opposite leg on fill (OCO), and close the
filled leg via native Binance TP/SL trigger orders using its own dedicated TP/SL measurement.
Fully mocked with a fake httpx client — NO real Binance calls."""
import asyncio
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
        self.headers = []

    def _match(self, url):
        for sub, resp in self.routes:
            if sub in url:
                status = resp.get("__status", 200) if isinstance(resp, dict) else 200
                return FakeResp(resp, status=status)
        return FakeResp({"code": -1, "msg": "unmatched"}, status=400)

    async def request(self, method, url, headers=None, timeout=None, follow_redirects=False):
        self.calls.append((method, url))
        self.headers.append(headers or {})
        return self._match(url)

    async def get(self, url, headers=None, timeout=None, follow_redirects=False):
        self.calls.append(("GET", url))
        self.headers.append(headers or {})
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


def test_arm_places_two_native_stop_entry_orders(monkeypatch):
    _reset()
    _cfg()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    server.STATE["latest"] = {"BTCUSDT": {"base": "BTC", "price": 100.0, "quoteVol": 1e6, "quote": "USDT"}}
    http = FakeHTTP([
        ("/fapi/v1/positionSide/dual", {"dualSidePosition": False}),
        ("/fapi/v1/leverage", {"leverage": 3}),
        ("/fapi/v1/exchangeInfo", {"symbols": []}),
        ("/fapi/v1/order", {"orderId": 111, "status": "NEW"}),
    ])
    asyncio.run(server.handle_signal(http, "BTCUSDT", "buy", 100.0, time.time(), volume=1e6))
    s = server.BOT["binStraddles"].get("BTCUSDT")
    assert s is not None and s["live"] is True
    order_urls = [u for _, u in http.calls if "/fapi/v1/order" in u]
    assert len(order_urls) == 2, order_urls
    joined = " ".join(order_urls)
    assert "type=STOP_MARKET" in joined
    # LONG stop-entry (BUY) above the mark, SHORT stop-entry (SELL) below
    import re
    def _sp(u):
        m = re.search(r"stopPrice=([0-9.]+)", u)
        return float(m.group(1)) if m else None
    buy = [u for u in order_urls if "side=BUY" in u]
    sell = [u for u in order_urls if "side=SELL" in u]
    assert buy and sell
    assert abs(_sp(buy[0]) - 100.08) < 1e-3 and _sp(buy[0]) > 100.0
    assert abs(_sp(sell[0]) - 99.92) < 1e-3 and _sp(sell[0]) < 100.0
    # market-trigger orders must NOT carry timeInForce
    assert "timeInForce" not in joined
    assert "workingType=MARK_PRICE" in joined
    _cfg(dryRun=True, exchange="binance", binStradEnabled=False)


def test_live_fill_cancels_opposite_and_places_native_tpsl(monkeypatch):
    _reset()
    _cfg()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    cancels = []
    tpsl_calls = []

    async def fake_status(http, sym, oid):
        return {"status": "FILLED", "avgPrice": "100.08"} if oid == "L1" else {"status": "NEW"}

    async def fake_cancel(http, sym, oid):
        cancels.append(oid)

    async def fake_tpsl(http, sym, is_long, tp, sl):
        tpsl_calls.append((is_long, tp, sl))
        return {"tp": {"orderId": "t1"}, "sl": {"orderId": "s1"}}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(server, "_fapi_order_status", fake_status)
    monkeypatch.setattr(server, "_fapi_cancel_order", fake_cancel)
    monkeypatch.setattr(server, "_fapi_place_native_tpsl", fake_tpsl)
    monkeypatch.setattr(server, "send_webhook", noop)

    now = time.time()
    server.BOT["binStraddles"]["BTCUSDT"] = {
        "symbol": "BTCUSDT", "base": "BTC", "mark": 100.0, "qty": 1.0, "lev": 3,
        "longStop": 100.08, "shortStop": 99.92, "longOid": "L1", "shortOid": "S1",
        "createdTs": now, "live": True, "lastPollTs": 0.0,
    }
    asyncio.run(server._process_bin_straddles(None, now + 3))
    assert "S1" in cancels, "opposite (SHORT) leg must be cancelled on LONG fill (OCO)"
    assert "BTCUSDT" not in server.BOT["binStraddles"]
    pos = server.BOT["positions"]["BTCUSDT"]
    assert pos["source"] == "bin_straddle" and pos["side"] == "long"
    assert abs(pos["entryPrice"] - 100.08) < 1e-9
    # dedicated TP/SL: LONG tp above, sl below
    assert tpsl_calls and tpsl_calls[0][0] is True
    assert abs(pos["tpPrice"] - 100.08 * 1.005) < 1e-6
    assert abs(pos["slPrice"] - 100.08 * 0.997) < 1e-6
    _reset()
    _cfg(dryRun=True, exchange="binance", binStradEnabled=False)


def test_native_tpsl_payload_long_close_all(monkeypatch):
    _reset()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    http = FakeHTTP([
        ("/fapi/v1/positionSide/dual", {"dualSidePosition": False}),
        ("/fapi/v1/exchangeInfo", {"symbols": []}),
        ("/fapi/v1/order", {"orderId": 7}),
    ])
    asyncio.run(server._fapi_place_native_tpsl(http, "BTCUSDT", True, 105.0, 98.0))
    order_urls = [u for _, u in http.calls if "/fapi/v1/order" in u]
    assert len(order_urls) == 2
    joined = " ".join(order_urls)
    assert "type=TAKE_PROFIT_MARKET" in joined and "type=STOP_MARKET" in joined
    # closing a LONG -> SELL triggers, close-all (no quantity, no reduceOnly)
    assert all("side=SELL" in u for u in order_urls)
    assert "closePosition=true" in joined
    assert "reduceOnly" not in joined and "quantity=" not in joined


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
    # price breaks above the long stop -> SIM long fill
    server.STATE["latest"]["ETHUSDT"]["price"] = 201.0
    asyncio.run(server._process_bin_straddles(None, now + 1))
    assert "ETHUSDT" not in server.BOT["binStraddles"]
    pos = server.BOT["positions"]["ETHUSDT"]
    assert pos["side"] == "long" and pos["dryRun"] is True and pos["source"] == "bin_straddle"
    # SIM qty scaled by leverage (3x * 100/200.16)
    assert abs(pos["qty"] - (100.0 / 200.16) * 3) < 1e-6
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
    asyncio.run(server._register_bin_straddle_fill(None, s, "short", 99.92, now, live=False))
    pos = server.BOT["positions"]["SOLUSDT"]
    assert pos["side"] == "short"
    assert abs(pos["tpPrice"] - 99.92 * 0.995) < 1e-6  # SHORT TP below entry
    assert abs(pos["slPrice"] - 99.92 * 1.003) < 1e-6  # SHORT SL above entry
    _reset()


def test_live_arm_expiry_cancels_both_legs(monkeypatch):
    _reset()
    _cfg()
    cancels = []

    async def fake_cancel(http, sym, oid):
        cancels.append(oid)

    monkeypatch.setattr(server, "_fapi_cancel_order", fake_cancel)
    now = time.time()
    server.BOT["binStraddles"]["BTCUSDT"] = {
        "symbol": "BTCUSDT", "base": "BTC", "mark": 100.0, "qty": 1.0, "lev": 3,
        "longStop": 100.08, "shortStop": 99.92, "longOid": "L1", "shortOid": "S1",
        "createdTs": now - (server.STRADDLE_EXPIRY + 5), "live": True, "lastPollTs": 0.0,
    }
    asyncio.run(server._process_bin_straddles(None, now))
    assert "BTCUSDT" not in server.BOT["binStraddles"]
    assert set(cancels) == {"L1", "S1"}, "both resting legs must be cancelled on expiry"
    _reset()
    _cfg(dryRun=True, exchange="binance", binStradEnabled=False)


def test_config_clamps_bin_straddle(monkeypatch):
    async def _noop():
        return None
    monkeypatch.setattr(server, "save_bot_config", _noop)
    async def run():
        return await server.bot_config(server.BotConfigUpdate(
            binStradEntryPct=99, binStradTpPct=0.0001, binStradSlPct=99))
    asyncio.run(run())
    cfg = server.BOT["config"]
    assert cfg["binStradEntryPct"] == 5.0
    assert cfg["binStradTpPct"] == 0.01
    assert cfg["binStradSlPct"] == 5.0
    # toggle round-trips
    asyncio.run(server.bot_config(server.BotConfigUpdate(binStradEnabled=True)))
    assert server.BOT["config"]["binStradEnabled"] is True
    asyncio.run(server.bot_config(server.BotConfigUpdate(binStradEnabled=False)))
