"""FUTURES entry buffer (Binance USD-M only): live entry is always a plain MARKET order.
The buffer only shapes the SIM preview fill — LONG previews -buf% below mark, SHORT +buf%
above. Fully in-memory, NO real orders."""
import asyncio
import time
import server


class FakeSigned:
    def __init__(self):
        self.calls = []

    async def __call__(self, http, method, path, params):
        if path == "/fapi/v1/order":
            self.calls.append(params)
            return {"executedQty": params.get("quantity", 1), "avgPrice": 100.0, "orderId": 123}
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

    async def _lev(http, sym, lev):
        return {}

    monkeypatch.setattr(server, "fapi_position_mode", _mode)
    monkeypatch.setattr(server, "_fapi_round_qty", _rq)
    monkeypatch.setattr(server, "_fapi_set_leverage", _lev)
    return fs


def test_long_entry_is_market(monkeypatch):
    fs = _patch(monkeypatch)
    r = asyncio.run(server._fapi_market_open(FakeHTTP(), "BTCUSDT", True, 0.01, 3, limit_price=99.0))
    p = fs.calls[-1]
    assert p["type"] == "MARKET" and p["side"] == "BUY"
    assert "price" not in p and "timeInForce" not in p
    assert r["orderId"] == "123"


def test_short_entry_is_market(monkeypatch):
    fs = _patch(monkeypatch)
    asyncio.run(server._fapi_market_open(FakeHTTP(), "BTCUSDT", False, 0.01, 3, limit_price=102.0))
    p = fs.calls[-1]
    assert p["type"] == "MARKET" and p["side"] == "SELL"
    assert "price" not in p and "timeInForce" not in p


def test_zero_buffer_is_market(monkeypatch):
    fs = _patch(monkeypatch)
    asyncio.run(server._fapi_market_open(FakeHTTP(), "BTCUSDT", True, 0.01, 3, limit_price=None))
    p = fs.calls[-1]
    assert p["type"] == "MARKET" and "price" not in p


def test_sim_preview_fill_direction(monkeypatch):
    """SIM: buffer shifts the recorded entry — LONG -buf% below, SHORT +buf% above mark."""
    server.BOT["positions"].clear()
    cfg = server.BOT["config"]
    cfg.update({"enabled": True, "dryRun": True, "exchange": "binance_futures",
                "futLongBufferPct": 1.0, "futShortBufferPct": 2.0, "futuresLeverage": 1,
                "maxPositionUsdt": 100})
    server.STATE["latest"]["BTCUSDT"] = {"price": 100.0, "quoteVol": 1e9, "base": "BTC"}

    async def run():
        await server._open_position(FakeHTTP(), "BTCUSDT", 100.0, time.time(), side="long",
                                     tp_price=None, sl_price=None, source="signal", pos_key="B#L")
        await server._open_position(FakeHTTP(), "BTCUSDT", 100.0, time.time(), side="short",
                                    tp_price=None, sl_price=None, source="signal", pos_key="B#S")
    asyncio.run(run())
    assert abs(server.BOT["positions"]["B#L"]["entryPrice"] - 99.0) < 1e-9, "long preview = -1%"
    assert abs(server.BOT["positions"]["B#S"]["entryPrice"] - 102.0) < 1e-9, "short preview = +2%"
    cfg.update({"enabled": False, "dryRun": True, "exchange": "hyperliquid",
                "futLongBufferPct": 0.0, "futShortBufferPct": 0.0})
    server.BOT["positions"].clear()
