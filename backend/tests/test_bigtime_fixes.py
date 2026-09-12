"""Verify BABYUSD/BIGTIME live fixes — cancel-replace guarantees exactly ONE native SL:
  1. _hl_cancel_coin cancels TRIGGER (SL) orders via frontend_open_orders (open_orders misses them),
     matching coin case-insensitively and handling flat + {'order': {...}} shapes.
  2. _hl_sync_stop cancels all stale SLs, places one, and dedupes so only 1 remains.
  3. _hl_market_open parses fill from filled.avgPx / data.fills[0].px / all_mids.
Fully mocked — NO real orders are sent."""
import asyncio
import os
import types
import server


class StatefulHL:
    """Emulates a Hyperliquid account: order() adds a resting trigger, cancel() removes it,
    frontend_open_orders() reflects current state (open_orders returns [] like the real API
    does for untriggered triggers)."""
    def __init__(self, initial=None):
        self._orders = list(initial or [])
        self._next = 1000

    # exchange side
    def order(self, coin, is_buy, sz, lim, ot, reduce_only=False):
        oid = self._next
        self._next += 1
        tag = ot.get("trigger", {}).get("tpsl")
        self._orders.append({"coin": coin, "oid": oid, "isTrigger": True,
                             "orderType": "Stop Market" if tag == "sl" else "Take Profit Market",
                             "reduceOnly": reduce_only})
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}

    def cancel(self, coin, oid):
        def keep(o):
            c = o.get("coin") or (o.get("order") or {}).get("coin")
            i = o.get("oid") if o.get("oid") is not None else (o.get("order") or {}).get("oid")
            return not (str(c).upper() == str(coin).upper() and i == oid)
        self._orders = [o for o in self._orders if keep(o)]
        return {"status": "ok"}

    # info side
    def frontend_open_orders(self, addr):
        return list(self._orders)

    def open_orders(self, addr):
        return []  # real API: untriggered triggers do NOT appear here

    def all_mids(self):
        return {}


def _wire(monkeypatch, hl):
    monkeypatch.setattr(server, "_get_hl_exchange", lambda: hl)
    server._HL["info"] = hl
    os.environ["HYPERLIQUID_ACCOUNT_ADDRESS"] = "0xTEST"
    async def _ready():
        return None
    monkeypatch.setattr(server, "_ensure_hl_ready", _ready)
    monkeypatch.setattr(server.hlconv, "convert_binance_to_hyperliquid", lambda b: b)
    monkeypatch.setattr(server.hlconv, "round_size", lambda c, q: q)
    monkeypatch.setattr(server.hlconv, "round_price", lambda c, p: p)


def test_cancel_uses_frontend_orders_and_shapes(monkeypatch):
    # mix flat + nested {'order': {...}} shapes; lower-case coin to test case-insensitive match
    hl = StatefulHL(initial=[
        {"coin": "BIGTIME", "oid": 1, "isTrigger": True},
        {"order": {"coin": "bigtime", "oid": 2, "isTrigger": True}},
        {"coin": "BIGTIME", "oid": 3, "isTrigger": True},
        {"coin": "ETH", "oid": 9, "isTrigger": True},
    ])
    _wire(monkeypatch, hl)
    n = asyncio.run(server._hl_cancel_coin("BIGTIME"))
    assert n == 3, f"expected 3 cancelled, got {n}"
    assert [o["coin"] for o in hl._orders] == ["ETH"], hl._orders


def test_sync_stop_leaves_exactly_one_sl(monkeypatch):
    # start with 3 stacked SLs (like the BABYUSD screenshot)
    hl = StatefulHL(initial=[
        {"coin": "BABY", "oid": 11, "isTrigger": True, "orderType": "Stop Market"},
        {"coin": "BABY", "oid": 12, "isTrigger": True, "orderType": "Stop Market"},
        {"coin": "BABY", "oid": 13, "isTrigger": True, "orderType": "Stop Market"},
    ])
    _wire(monkeypatch, hl)
    cfg = server.BOT["config"]
    cfg["dryRun"] = False
    cfg["exchange"] = "hyperliquid"
    cfg["hlNativeTpsl"] = True
    pos = {"symbol": "BABYUSD", "base": "BABY", "side": "short", "qty": 857.0,
           "slPrice": 0.011660, "dryRun": False, "nativeSlPrice": 0.011705}
    asyncio.run(server._hl_sync_stop(pos, "BABYUSD", 0.011660))
    sls = [o for o in hl._orders if o["coin"] == "BABY"]
    assert len(sls) == 1, f"expected exactly 1 SL after sync, got {hl._orders}"
    assert pos["nativeSlPrice"] == 0.011660
    assert pos.get("nativeSlOid") == sls[0]["oid"]


def test_dedupe_removes_extra_keeps_newest(monkeypatch):
    hl = StatefulHL(initial=[
        {"coin": "BABY", "oid": 11, "isTrigger": True},
        {"coin": "BABY", "oid": 12, "isTrigger": True},
        {"coin": "BABY", "oid": 99, "isTrigger": True},  # the one to keep
    ])
    _wire(monkeypatch, hl)
    removed = asyncio.run(server._hl_dedupe_sl("BABY", keep_oid=99))
    assert removed == 2
    assert [o["oid"] for o in hl._orders] == [99]


def test_market_open_fill_from_data_fills(monkeypatch):
    class FakeEx:
        def market_open(self, coin, is_buy, size, px, slippage):
            return {"status": "ok", "response": {"data": {
                "statuses": [{"filled": {"totalSz": "857.0"}}],
                "fills": [{"px": "0.0116600"}],
            }}}
    monkeypatch.setattr(server, "_get_hl_exchange", lambda: FakeEx())
    server._HL["info"] = StatefulHL()
    r = asyncio.run(server._hl_market_open("BABY", 857.0, is_buy=False, leverage=None))
    assert abs(r["fillPrice"] - 0.01166) < 1e-9, r
    assert r["executedQty"] == 857.0
