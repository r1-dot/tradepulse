"""FUTURES entry buffers (Binance USD-M only): LONG enters via the negative buffer
(limit buy below mark), SHORT via the positive buffer (limit sell above mark);
0 = off (plain MARKET order). Fully in-memory, NO real orders."""
import asyncio
import time
import server


class FakeSigned:
    """Capture the params sent to /fapi/v1/order so we can assert order type/price."""
    def __init__(self):
        self.calls = []

    async def __call__(self, http, method, path, params):
        if path == "/fapi/v1/order":
            self.calls.append(params)
            return {"executedQty": params.get("quantity", 1), "avgPrice": params.get("price", 0) or 100.0,
                    "orderId": 123}
        if path == "/fapi/v1/leverage":
            return {"leverage": params.get("leverage")}
        return {}


class FakeHTTP:
    async def get(self, *a, **k):
        raise AssertionError("no market-data fetch expected")


def _patch(monkeypatch):
    fs = FakeSigned()
    monkeypatch.setattr(server, "fapi_signed", fs)

    async def _mode(http):
        return False  # one-way

    async def _rq(http, sym, qty):
        return qty

    async def _rp(http, sym, price):
        return round(price, 2)

    async def _lev(http, sym, lev):
        return {}

    monkeypatch.setattr(server, "fapi_position_mode", _mode)
    monkeypatch.setattr(server, "_fapi_round_qty", _rq)
    monkeypatch.setattr(server, "_fapi_round_price", _rp)
    monkeypatch.setattr(server, "_fapi_set_leverage", _lev)
    return fs


def test_long_uses_limit_below_mark(monkeypatch):
    fs = _patch(monkeypatch)
    r = asyncio.run(server._fapi_market_open(FakeHTTP(), "BTCUSDT", True, 0.01, 3, limit_price=99.0))
    p = fs.calls[-1]
    assert p["type"] == "LIMIT" and p["side"] == "BUY"
    assert p["price"] == 99.0 and p["timeInForce"] == "GTC"
    assert r["orderId"] == "123"


def test_short_uses_limit_above_mark(monkeypatch):
    fs = _patch(monkeypatch)
    asyncio.run(server._fapi_market_open(FakeHTTP(), "BTCUSDT", False, 0.01, 3, limit_price=102.0))
    p = fs.calls[-1]
    assert p["type"] == "LIMIT" and p["side"] == "SELL" and p["price"] == 102.0


def test_zero_buffer_is_market(monkeypatch):
    fs = _patch(monkeypatch)
    asyncio.run(server._fapi_market_open(FakeHTTP(), "BTCUSDT", True, 0.01, 3, limit_price=None))
    p = fs.calls[-1]
    assert p["type"] == "MARKET" and "price" not in p
