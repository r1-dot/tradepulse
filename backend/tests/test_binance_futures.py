"""Binance USD-M Futures: verify signing, position-mode detection (one-way vs hedge),
reduce-only close semantics, and that sim/dry-run routes long+short without real orders.
Fully mocked with a fake httpx client — NO real Binance calls."""
import asyncio
import server


class FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = str(data)

    def json(self):
        return self._data


class FakeHTTP:
    """Records signed requests and returns queued responses keyed by path substring."""
    def __init__(self, routes):
        self.routes = routes       # list of (path_substr, response_dict)
        self.calls = []            # (method, url)

    async def request(self, method, url, headers=None, timeout=None):
        self.calls.append((method, url))
        for sub, resp in self.routes:
            if sub in url:
                return FakeResp(resp)
        return FakeResp({"code": -1, "msg": "unmatched"}, status=400)

    async def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        for sub, resp in self.routes:
            if sub in url:
                return FakeResp(resp)
        return FakeResp({}, status=404)


def _reset_mode():
    server._FAPI["mode"] = None
    server._FAPI["modeTs"] = 0.0
    server._FAPI["lots"] = {}
    server._FAPI["lotsTs"] = 0.0


def test_signing_includes_apikey_and_signature(monkeypatch):
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    http = FakeHTTP([("/fapi/v1/positionSide/dual", {"dualSidePosition": False})])
    asyncio.run(server.fapi_signed(http, "GET", "/fapi/v1/positionSide/dual", {}))
    method, url = http.calls[0]
    assert "signature=" in url and "timestamp=" in url and "recvWindow=" in url


def test_position_mode_detected_not_guessed(monkeypatch):
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    http = FakeHTTP([("/fapi/v1/positionSide/dual", {"dualSidePosition": True})])
    assert asyncio.run(server.fapi_position_mode(http)) is True
    http2 = FakeHTTP([("/fapi/v1/positionSide/dual", {"dualSidePosition": False})])
    _reset_mode()
    assert asyncio.run(server.fapi_position_mode(http2)) is False


def test_open_oneway_uses_both(monkeypatch):
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    http = FakeHTTP([
        ("/fapi/v1/positionSide/dual", {"dualSidePosition": False}),
        ("/fapi/v1/leverage", {"leverage": 5}),
        ("/fapi/v1/exchangeInfo", {"symbols": []}),
        ("/fapi/v1/order", {"executedQty": "0.5", "avgPrice": "100.0", "orderId": 1}),
    ])
    r = asyncio.run(server._fapi_market_open(http, "BTCUSDT", True, 0.5, 5))
    assert r["fillPrice"] == 100.0 and r["executedQty"] == 0.5
    order_url = [u for _, u in http.calls if "/fapi/v1/order" in u][0]
    assert "positionSide=BOTH" in order_url and "side=BUY" in order_url


def test_close_oneway_is_reduce_only(monkeypatch):
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    http = FakeHTTP([
        ("/fapi/v1/positionSide/dual", {"dualSidePosition": False}),
        ("/fapi/v1/exchangeInfo", {"symbols": []}),
        ("/fapi/v1/order", {"avgPrice": "99.0"}),
    ])
    asyncio.run(server._fapi_market_close(http, "BTCUSDT", True, 0.5))  # closing a LONG
    order_url = [u for _, u in http.calls if "/fapi/v1/order" in u][0]
    assert "reduceOnly=true" in order_url and "side=SELL" in order_url and "positionSide=BOTH" in order_url


def test_close_hedge_no_reduce_only_leg_side(monkeypatch):
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    http = FakeHTTP([
        ("/fapi/v1/positionSide/dual", {"dualSidePosition": True}),
        ("/fapi/v1/exchangeInfo", {"symbols": []}),
        ("/fapi/v1/order", {"avgPrice": "99.0"}),
    ])
    asyncio.run(server._fapi_market_close(http, "BTCUSDT", False, 0.5))  # closing a SHORT in hedge
    order_url = [u for _, u in http.calls if "/fapi/v1/order" in u][0]
    assert "reduceOnly" not in order_url
    assert "side=BUY" in order_url and "positionSide=SHORT" in order_url


def test_sim_futures_long_and_short_no_real_orders(monkeypatch):
    cfg = server.BOT["config"]
    cfg["dryRun"] = True
    cfg["exchange"] = "binance_futures"
    cfg["maxPositionUsdt"] = 10.0
    cfg["futuresLeverage"] = 4
    cfg["maxOpenPositions"] = 50
    cfg["dailyLossLimit"] = 1e9
    server.BOT["positions"].clear()
    server.STATE["latest"] = {"BTCUSDT": {"base": "BTC", "price": 100.0, "quote": "USDT"}}
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(server, "send_webhook", _noop)
    # a fapi call in sim would raise (no key) — prove we never touch it
    async def _boom(*a, **k):
        raise AssertionError("real futures API must NOT be called in dryRun")
    monkeypatch.setattr(server, "_fapi_market_open", _boom)
    asyncio.run(server._open_position(None, "BTCUSDT", 100.0, 1000.0, side="short", source="signal"))
    p = server.BOT["positions"]["BTCUSDT"]
    assert p["side"] == "short" and abs(p["qty"] - 0.4) < 1e-9  # 4x * (10/100)
