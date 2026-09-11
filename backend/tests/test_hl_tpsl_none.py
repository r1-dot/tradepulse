"""Verify the live-trade crash fix: _hl_place_tpsl must skip a None leg (trailing
positions have no fixed TP) instead of raising 'NoneType * float', and _hl_market_open
must fall back to all_mids() when the fill avgPx is missing. No real orders are placed."""
import asyncio
import types
import server


class FakeExchange:
    def __init__(self):
        self.orders = []

    def order(self, coin, is_buy, sz, lim, ot, reduce_only=False):
        self.orders.append({"coin": coin, "is_buy": is_buy, "sz": sz, "lim": lim,
                            "ot": ot, "reduce_only": reduce_only})
        return {"status": "ok"}


def test_place_tpsl_skips_none_tp(monkeypatch):
    fake = FakeExchange()
    monkeypatch.setattr(server, "_get_hl_exchange", lambda: fake)
    # trailing SHORT: tp is None, sl is a real price -> must place ONLY the SL leg, no crash
    out = asyncio.run(server._hl_place_tpsl("XYZ", is_long=False, sz=10.0, tp_px=None, sl_px=0.00370))
    tags = [t for t, _ in out]
    assert tags == ["sl"], f"expected only sl leg, got {tags}"
    assert len(fake.orders) == 1
    assert fake.orders[0]["reduce_only"] is True
    assert fake.orders[0]["ot"]["trigger"]["tpsl"] == "sl"


def test_place_tpsl_both_legs(monkeypatch):
    fake = FakeExchange()
    monkeypatch.setattr(server, "_get_hl_exchange", lambda: fake)
    out = asyncio.run(server._hl_place_tpsl("XYZ", is_long=True, sz=10.0, tp_px=1.10, sl_px=0.90))
    tags = sorted(t for t, _ in out)
    assert tags == ["sl", "tp"], f"expected both legs, got {tags}"
    assert len(fake.orders) == 2


def test_place_tpsl_both_none(monkeypatch):
    fake = FakeExchange()
    monkeypatch.setattr(server, "_get_hl_exchange", lambda: fake)
    out = asyncio.run(server._hl_place_tpsl("XYZ", is_long=True, sz=10.0, tp_px=None, sl_px=None))
    assert out == []
    assert fake.orders == []


def test_market_open_fill_fallback_all_mids(monkeypatch):
    # market_open returns a status with NO avgPx -> fill price must come from all_mids()
    class FakeEx:
        def market_open(self, coin, is_buy, size, px, slippage):
            return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "5.0"}}]}}}
    monkeypatch.setattr(server, "_get_hl_exchange", lambda: FakeEx())
    server._HL["info"] = types.SimpleNamespace(all_mids=lambda: {"XYZ": "0.0036999"})
    r = asyncio.run(server._hl_market_open("XYZ", 5.0, is_buy=False, leverage=None))
    assert abs(r["fillPrice"] - 0.0036999) < 1e-9, r
    assert r["executedQty"] == 5.0
