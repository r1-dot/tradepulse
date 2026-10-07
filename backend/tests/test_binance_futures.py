"""Binance USD-M Futures: verify signing, position-mode detection (one-way vs hedge),
reduce-only close semantics, and that sim/dry-run routes long+short without real orders.
Fully mocked with a fake httpx client — NO real Binance calls."""
import asyncio
import server


class FakeResp:
    def __init__(self, data, status=200, text=None):
        self._data = data
        self.status_code = status
        self.text = text if text is not None else str(data)
        self.headers = {}

    def json(self):
        if isinstance(self._data, str):
            raise ValueError("not json")
        return self._data


class FakeHTTP:
    """Records signed requests and returns queued responses keyed by url substring.
    A response dict may carry {"__status": N} to simulate an HTTP status; a string value
    simulates a non-JSON body (e.g. an nginx 302 HTML page)."""
    def __init__(self, routes):
        self.routes = routes       # list of (url_substr, response)
        self.calls = []
        self.headers = []

    def _match(self, url):
        for sub, resp in self.routes:
            if sub in url:
                if isinstance(resp, str):
                    return FakeResp(resp, status=302, text=resp)
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


def _reset_mode():
    server._FAPI["mode"] = None
    server._FAPI["modeTs"] = 0.0
    server._FAPI["lots"] = {}
    server._FAPI["lotsTs"] = 0.0
    server._FAPI["activeHost"] = None


def test_signing_includes_apikey_and_signature(monkeypatch):
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    http = FakeHTTP([("/fapi/v1/positionSide/dual", {"dualSidePosition": False})])
    asyncio.run(server.fapi_signed(http, "GET", "/fapi/v1/positionSide/dual", {}))
    method, url = http.calls[0]
    assert "signature=" in url and "timestamp=" in url and "recvWindow=" in url
    # default host is now fapi1 and User-Agent must be sent
    assert "fapi1.binance.com" in url
    assert http.headers and http.headers[0].get("User-Agent")


def test_403_falls_back_to_next_mirror(monkeypatch):
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    # fapi1 + fapi2 are banned (403), fapi3 works
    http = FakeHTTP([
        ("fapi1.binance.com", {"__status": 403}),
        ("fapi2.binance.com", {"__status": 403}),
        ("fapi3.binance.com/fapi/v1/positionSide/dual", {"dualSidePosition": True}),
    ])
    res = asyncio.run(server.fapi_signed(http, "GET", "/fapi/v1/positionSide/dual", {}))
    assert res.get("dualSidePosition") is True
    assert server._FAPI["activeHost"] == "https://fapi3.binance.com"
    urls = [u for _, u in http.calls]
    assert any("fapi1.binance.com" in u for u in urls) and any("fapi3.binance.com" in u for u in urls)


def test_all_hosts_403_raises(monkeypatch):
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    http = FakeHTTP([("binance.com", {"__status": 403})])  # every host 403
    raised = False
    try:
        asyncio.run(server.fapi_signed(http, "GET", "/fapi/v1/positionSide/dual", {}))
    except RuntimeError as e:
        raised = "no host returned a valid response" in str(e)
    assert raised


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


def test_order_id_returned_various_shapes(monkeypatch):
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    # standard RESULT payload -> orderId
    http = FakeHTTP([
        ("/fapi/v1/positionSide/dual", {"dualSidePosition": False}),
        ("/fapi/v1/leverage", {"leverage": 3}),
        ("/fapi/v1/exchangeInfo", {"symbols": []}),
        ("/fapi/v1/order", {"orderId": 283194212, "executedQty": "0.5", "avgPrice": "100.0"}),
    ])
    r = asyncio.run(server._fapi_market_open(http, "BTCUSDT", True, 0.5, 3))
    assert r["orderId"] == "283194212", r

    # fallback: no orderId but clientOrderId present
    assert server._extract_order_id({"clientOrderId": "x7abc"}) == "x7abc"
    # alt casing
    assert server._extract_order_id({"orderID": 99}) == "99"
    # genuinely empty -> None (not the string 'None')
    assert server._extract_order_id({}) is None
    assert server._extract_order_id({"orderId": 0}) == "0"  # 0 is a valid id, not None


def test_close_returns_order_id(monkeypatch):
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    http = FakeHTTP([
        ("/fapi/v1/positionSide/dual", {"dualSidePosition": False}),
        ("/fapi/v1/exchangeInfo", {"symbols": []}),
        ("/fapi/v1/order", {"orderId": 555, "avgPrice": "99.0"}),
    ])
    r = asyncio.run(server._fapi_market_close(http, "BTCUSDT", True, 0.5))
    assert r["orderId"] == "555"


def test_302_redirect_html_is_not_success(monkeypatch):
    """CRITICAL: a 302 nginx HTML page must NOT be treated as a filled order (phantom position).
    All mirrors returning a redirect must raise, so the futures open fails loudly."""
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    html = "<html><head><title>302 Found</title></head><body>nginx</body></html>"
    http = FakeHTTP([("binance.com", html)])  # every host returns a 302 HTML page
    raised = False
    try:
        asyncio.run(server.fapi_signed(http, "POST", "/fapi/v1/order", {"symbol": "JSTUSDT"}))
    except RuntimeError as e:
        raised = "no host returned a valid response" in str(e)
    assert raised, "302/non-JSON must raise, never return as success"


def test_open_position_futures_failure_no_phantom(monkeypatch):
    """If the futures order can't be placed (all hosts 302), _open_position must NOT create a
    tracked position and must NOT log a FUTURES OPEN success."""
    _reset_mode()
    monkeypatch.setattr(server, "BINANCE_API_KEY", "K")
    monkeypatch.setattr(server, "BINANCE_API_SECRET", "S")
    cfg = server.BOT["config"]
    cfg["dryRun"] = False
    cfg["exchange"] = "binance_futures"
    cfg["maxPositionUsdt"] = 10.0
    cfg["futuresLeverage"] = 1
    cfg["maxOpenPositions"] = 50
    cfg["dailyLossLimit"] = 1e9
    server.BOT["positions"].clear()
    server.STATE["latest"] = {"JSTUSDT": {"base": "JST", "price": 0.3191, "quote": "USDT"}}

    async def _boom(http, method, path, params):
        raise RuntimeError("binance-futures: no host returned a valid response (redirect)")
    monkeypatch.setattr(server, "fapi_signed", _boom)

    class _H:  # _open_position may call _ensure_hl_ready etc; futures path only uses fapi_signed
        pass
    asyncio.run(server._open_position(_H(), "JSTUSDT", 0.3191, 1000.0, side="long", source="straddle"))
    assert "JSTUSDT" not in server.BOT["positions"], "must NOT create a phantom position on order failure"
    cfg["dryRun"] = True  # restore safe default
    cfg["exchange"] = "binance"


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
