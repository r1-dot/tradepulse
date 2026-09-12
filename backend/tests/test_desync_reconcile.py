"""Verify the METUSD desync fixes:
  1. _close_position sends a REAL market close, cancels triggers first, VERIFIES flat, and
     does NOT emit a false exit / does NOT drop tracking if the exchange is still open.
  2. _hl_market_close raises on an inner status error (rejected reduce-only close).
  3. reconcile_hl logs DESYNC and fixes: tracked-but-flat -> reconciled exit; untracked-open -> adopted.
Fully mocked — NO real orders are sent."""
import asyncio
import os
import types
import server


class HL:
    """Emulates a HL account with a position book + trigger orders."""
    def __init__(self, positions=None, close_fails=False, close_error=None):
        # positions: {COIN: {"szi":float,"entryPx":float,"upnl":float}}
        self.positions = dict(positions or {})
        self.close_fails = close_fails       # market_close silently doesn't flatten
        self.close_error = close_error       # inner status error string
        self.cancelled = []

    def market_close(self, coin):
        c = coin.upper()
        if self.close_error:
            return {"status": "ok", "response": {"data": {"statuses": [{"error": self.close_error}]}}}
        if not self.close_fails:
            self.positions.pop(c, None)
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"avgPx": "0.2374", "totalSz": "42"}}]}}}

    def cancel(self, coin, oid):
        self.cancelled.append((coin, oid))
        return {"status": "ok"}

    # info side
    def user_state(self, addr):
        return {"assetPositions": [
            {"position": {"coin": c, "szi": p["szi"], "entryPx": p["entryPx"],
                          "unrealizedPnl": p.get("upnl", 0.0)}}
            for c, p in self.positions.items()
        ]}

    def frontend_open_orders(self, addr):
        return []

    def open_orders(self, addr):
        return []


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
    cfg = server.BOT["config"]
    cfg["dryRun"] = False
    cfg["exchange"] = "hyperliquid"
    cfg["hlReconcile"] = True
    server.BOT["positions"].clear()
    server.BOT["closeIntents"].clear()
    server.STATE["latest"] = {}


def test_close_success_flattens_and_exits(monkeypatch):
    hl = HL(positions={"MET": {"szi": -42.0, "entryPx": 0.2379, "upnl": 0.02}})
    _wire(monkeypatch, hl)
    pos = {"symbol": "METUSDT", "base": "MET", "side": "short", "entryPrice": 0.2379,
           "qty": 42.0, "quoteSpent": 10.0, "tpPrice": None, "slPrice": 0.238, "dryRun": False,
           "openedTs": 0, "source": "straddle"}
    server.BOT["positions"]["METUSDT"] = pos
    asyncio.run(server._close_position(None, "METUSDT", 0.2374, "trail-exit"))
    assert "METUSDT" not in server.BOT["positions"], "position should be removed after confirmed close"
    assert "MET" not in hl.positions, "exchange must be flat"


def test_close_failure_keeps_tracking_no_false_exit(monkeypatch):
    # market_close returns ok but never flattens -> must KEEP tracking + set closeIntent, no exit
    hl = HL(positions={"MET": {"szi": -42.0, "entryPx": 0.2379, "upnl": 0.02}}, close_fails=True)
    _wire(monkeypatch, hl)
    pos = {"symbol": "METUSDT", "base": "MET", "side": "short", "entryPrice": 0.2379,
           "qty": 42.0, "quoteSpent": 10.0, "tpPrice": None, "slPrice": 0.238, "dryRun": False,
           "openedTs": 0, "source": "straddle"}
    server.BOT["positions"]["METUSDT"] = pos
    daily_before = server.BOT["dailyPnl"]
    asyncio.run(server._close_position(None, "METUSDT", 0.2374, "trail-exit"))
    assert "METUSDT" in server.BOT["positions"], "must NOT drop tracking when exchange still open"
    assert "MET" in server.BOT["closeIntents"], "must flag closeIntent for reconciler retry"
    assert server.BOT["dailyPnl"] == daily_before, "must NOT book a fake pnl / exit"


def test_market_close_raises_on_inner_error(monkeypatch):
    hl = HL(positions={"MET": {"szi": -42.0, "entryPx": 0.2379}}, close_error="Order would flip position")
    _wire(monkeypatch, hl)
    raised = False
    try:
        asyncio.run(server._hl_market_close("MET"))
    except RuntimeError:
        raised = True
    assert raised, "inner status error must raise"


def test_reconcile_tracked_but_flat_books_exit(monkeypatch):
    hl = HL(positions={})  # exchange has NO positions
    _wire(monkeypatch, hl)
    pos = {"symbol": "METUSDT", "base": "MET", "side": "short", "entryPrice": 0.2379,
           "qty": 42.0, "quoteSpent": 10.0, "tpPrice": None, "slPrice": 0.238, "dryRun": False,
           "openedTs": 0, "source": "straddle", "nativeSlPrice": 0.2374}
    server.BOT["positions"]["METUSDT"] = pos
    asyncio.run(server.reconcile_hl(1000.0))
    assert "METUSDT" not in server.BOT["positions"], "flat-on-exchange position must be reconciled out"
    kinds = [j.get("kind") for j in list(server.BOT["journal"])[:5]]
    assert "desync" in kinds


def test_reconcile_untracked_open_adopts(monkeypatch):
    hl = HL(positions={"BABY": {"szi": 857.0, "entryPx": 0.01166, "upnl": -0.03}})
    _wire(monkeypatch, hl)
    asyncio.run(server.reconcile_hl(1000.0))
    adopted = [p for p in server.BOT["positions"].values() if p["base"] == "BABY"]
    assert len(adopted) == 1, "untracked exchange position must be adopted"
    assert adopted[0]["side"] == "long" and adopted[0]["source"] == "adopted"
    assert adopted[0]["tpPrice"] is None  # trailing manages it
