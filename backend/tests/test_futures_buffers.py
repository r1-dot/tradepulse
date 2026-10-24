"""FUTURES slippage caps (Binance USD-M only): a buffer>0 turns the entry into a
marketable IOC LIMIT order that fills instantly but never worse than buf% from mark
(LONG caps +buf% above, SHORT -buf% below); buffer 0 = plain MARKET. Fully in-memory."""
import asyncio
import time
import server


class FakeSigned:
    """Capture params sent to /fapi/v1/order so we can assert order type/price/TIF."""
    def __init__(self):
        self.calls = []

    async def __call__(self, http, method, path, params):
        if path == "/fapi/v1/order":
            self.calls.append(params)
            return {"executedQty": params.get("quantity", 1),
                    "avgPrice": params.get("price", 0) or 100.0, "orderId": 123}
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
        return False

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


def test_long_cap_is_marketable_ioc(monkeypatch):
    fs = _patch(monkeypatch)
    # long slippage cap -> price above mark, instant IOC
    r = asyncio.run(server._fapi_market_open(FakeHTTP(), "BTCUSDT", True, 0.01, 3, limit_price=100.1))
    p = fs.calls[-1]
    assert p["type"] == "LIMIT" and p["side"] == "BUY"
    assert p["timeInForce"] == "IOC" and p["price"] == 100.1
    assert r["orderId"] == "123"


def test_short_cap_is_marketable_ioc(monkeypatch):
    fs = _patch(monkeypatch)
    asyncio.run(server._fapi_market_open(FakeHTTP(), "BTCUSDT", False, 0.01, 3, limit_price=99.9))
    p = fs.calls[-1]
    assert p["type"] == "LIMIT" and p["side"] == "SELL"
    assert p["timeInForce"] == "IOC" and p["price"] == 99.9


def test_zero_buffer_is_plain_market(monkeypatch):
    fs = _patch(monkeypatch)
    asyncio.run(server._fapi_market_open(FakeHTTP(), "BTCUSDT", True, 0.01, 3, limit_price=None))
    p = fs.calls[-1]
    assert p["type"] == "MARKET" and "price" not in p and "timeInForce" not in p


def test_open_position_cap_direction(monkeypatch):
    """_open_position must cap LONG at +buf above mark and SHORT at -buf below mark."""
    captured = {}

    async def _fake_open(http, sym, is_long, qty, lev, limit_price=None):
        captured[("long" if is_long else "short")] = limit_price
        return {"executedQty": qty, "fillPrice": limit_price or 100.0, "orderId": 7}

    monkeypatch.setattr(server, "_fapi_market_open", _fake_open)
    server.BOT["positions"].clear()
    cfg = server.BOT["config"]
    cfg.update({"enabled": True, "dryRun": False, "exchange": "binance_futures",
                "futLongBufferPct": 1.0, "futShortBufferPct": 2.0, "futuresLeverage": 1,
                "maxPositionUsdt": 100})
    server.STATE["latest"]["BTCUSDT"] = {"price": 100.0, "quoteVol": 1e9, "base": "BTC"}

    async def run():
        await server._open_position(FakeHTTP(), "BTCUSDT", 100.0, time.time(), side="long",
                                     tp_price=None, sl_price=None, source="signal", pos_key="B#L")
        await server._open_position(FakeHTTP(), "BTCUSDT", 100.0, time.time(), side="short",
                                    tp_price=None, sl_price=None, source="signal", pos_key="B#S")
    asyncio.run(run())
    assert abs(captured["long"] - 99.0) < 1e-9, "long cap = -1% below mark"
    assert abs(captured["short"] - 102.0) < 1e-9, "short cap = +2% above mark"
    cfg.update({"enabled": False, "dryRun": True, "exchange": "hyperliquid",
                "futLongBufferPct": 0.0, "futShortBufferPct": 0.0})
    server.BOT["positions"].clear()
