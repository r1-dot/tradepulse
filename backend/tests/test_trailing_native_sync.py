"""Verify that when the software trail steps the stop up, the native SL trigger is
re-placed on Hyperliquid (reduce-only). Fully mocked — NO real orders are sent."""
import asyncio
import os
import types
import server


class FakeExchange:
    def __init__(self):
        self.orders = []
        self.cancels = []

    def order(self, coin, is_buy, sz, lim, ot, reduce_only=False):
        self.orders.append({"coin": coin, "ot": ot, "reduce_only": reduce_only, "sz": sz})
        return {"status": "ok"}

    def cancel(self, coin, oid):
        self.cancels.append((coin, oid))
        return {"status": "ok"}


def _setup_live(monkeypatch, fake):
    monkeypatch.setattr(server, "_get_hl_exchange", lambda: fake)
    async def _ready():
        return None
    monkeypatch.setattr(server, "_ensure_hl_ready", _ready)
    server._HL["info"] = types.SimpleNamespace(open_orders=lambda addr: [], all_mids=lambda: {})
    os.environ["HYPERLIQUID_ACCOUNT_ADDRESS"] = "0xTEST"
    monkeypatch.setattr(server.hlconv, "convert_binance_to_hyperliquid", lambda b: b)
    monkeypatch.setattr(server.hlconv, "round_size", lambda c, q: q)
    monkeypatch.setattr(server.hlconv, "round_price", lambda c, p: p)
    cfg = server.BOT["config"]
    for k, v in {"dryRun": False, "exchange": "hyperliquid", "hlNativeTpsl": True,
                 "trailSecure": 0.40, "trailBE": 0.05, "trailStep": 0.5, "trailCallback": 0.30}.items():
        cfg[k] = v


def test_native_sl_synced_on_step_up(monkeypatch):
    fake = FakeExchange()
    _setup_live(monkeypatch, fake)
    pos = {"symbol": "BTCUSDT", "base": "BTC", "side": "long", "entryPrice": 100.0,
           "qty": 1.0, "tpPrice": None, "slPrice": 99.7, "source": "straddle", "dryRun": False}
    # peak = +0.5% -> crosses secure(0.40) -> lock steps up -> native SL must be placed
    asyncio.run(server._update_trailing(None, "BTCUSDT", pos, 100.5))
    sl_orders = [o for o in fake.orders if o["ot"]["trigger"]["tpsl"] == "sl"]
    assert len(sl_orders) == 1, f"expected 1 native SL placed, got {fake.orders}"
    assert sl_orders[0]["reduce_only"] is True
    assert pos.get("nativeSlPrice") is not None
    # no fixed TP for trailing positions
    assert not any(o["ot"]["trigger"]["tpsl"] == "tp" for o in fake.orders)


def test_no_resync_when_not_stepped(monkeypatch):
    fake = FakeExchange()
    _setup_live(monkeypatch, fake)
    pos = {"symbol": "BTCUSDT", "base": "BTC", "side": "long", "entryPrice": 100.0,
           "qty": 1.0, "tpPrice": None, "slPrice": 99.7, "source": "straddle", "dryRun": False,
           "peakPct": 0.5, "slOffPct": 0.05}
    # already secured at this level; price ticks up slightly but the lock offset (0.05) does
    # not increase (peak-step+be still 0.05) -> no new native order
    asyncio.run(server._update_trailing(None, "BTCUSDT", pos, 100.45))
    assert fake.orders == [], f"should not re-place SL when lock did not step up: {fake.orders}"
