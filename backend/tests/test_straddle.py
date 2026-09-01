"""Straddle system tests (backend).

Covers: config persistence + clamps, ARM with wide band (pending state),
FILL + OCO with tiny band, short-side correctness (best-effort across many
symbols), close-all cleanup (positions + straddles), and non-straddle regression.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip()
                break
BASE_URL = BASE_URL.rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    yield s
    # Safety cleanup — restore safe defaults
    try:
        s.post(f"{API}/bot/close-all", timeout=15)
        s.post(f"{API}/bot/config", json={
            "enabled": False, "dryRun": True,
            "straddleEnabled": False,
            "straddleEntryPct": 0.5, "straddleTpPct": 1.0, "straddleSlPct": 1.0,
            "streak": 2, "minVolumeUsd": 5_000_000, "maxVolumeUsd": 0,
            "cooldownSec": 30, "maxOpenPositions": 3,
            "autoExit": True, "maxLossPerTradeUsdt": 0.0002,
        }, timeout=15)
    except Exception:
        pass


def _mark(client, sym):
    """Fetch current price from bot status or tokens endpoint."""
    r = client.get(f"{API}/tokens", params={"timeframe": "1s", "quote": "USDT", "limit": 1500}, timeout=15)
    for t in r.json().get("tokens", []):
        if t["symbol"] == sym:
            return float(t["price"])
    return None


# ---------- Config persistence + clamps ----------
class TestStraddleConfig:
    def test_persist_and_echo(self, client):
        r = client.post(f"{API}/bot/config", json={
            "straddleEnabled": True,
            "straddleEntryPct": 0.5,
            "straddleTpPct": 2.0,
            "straddleSlPct": 3.0,
        }, timeout=15)
        assert r.status_code == 200
        cfg = r.json()["config"]
        assert cfg["straddleEnabled"] is True
        assert abs(cfg["straddleEntryPct"] - 0.5) < 1e-9
        assert abs(cfg["straddleTpPct"] - 2.0) < 1e-9
        assert abs(cfg["straddleSlPct"] - 3.0) < 1e-9

        d = client.get(f"{API}/bot/status", timeout=15).json()
        c2 = d["config"]
        assert c2["straddleEnabled"] is True
        assert abs(c2["straddleEntryPct"] - 0.5) < 1e-9
        assert abs(c2["straddleTpPct"] - 2.0) < 1e-9
        assert abs(c2["straddleSlPct"] - 3.0) < 1e-9

    def test_clamps(self, client):
        r = client.post(f"{API}/bot/config", json={
            "straddleEntryPct": 99,   # -> 5
            "straddleTpPct": 0.0001,  # -> 0.01
            "straddleSlPct": 99,      # -> 5
        }, timeout=15)
        assert r.status_code == 200
        c = r.json()["config"]
        assert c["straddleEntryPct"] == 5.0
        assert abs(c["straddleTpPct"] - 0.01) < 1e-9
        assert c["straddleSlPct"] == 5.0


# ---------- ARM: wide band → pending state ----------
class TestStraddleArm:
    def test_arm_pending(self, client):
        # Cleanup first
        client.post(f"{API}/bot/close-all", timeout=15)
        r = client.post(f"{API}/bot/config", json={
            "enabled": True, "dryRun": True,
            "straddleEnabled": True,
            "straddleEntryPct": 5.0,   # wide → stays pending
            "straddleTpPct": 10.0, "straddleSlPct": 5.0,
            "minVolumeUsd": 0, "maxVolumeUsd": 0,
            "cooldownSec": 0, "maxOpenPositions": 10,
            "autoExit": False, "maxLossPerTradeUsdt": 0.0,
        }, timeout=15)
        assert r.status_code == 200

        symbols = ["ETHUSDT", "BNBUSDT"]
        events = []
        for sym in symbols:
            p = _mark(client, sym) or 0.0
            events.append({"symbol": sym, "side": "buy", "price": p, "volume": 0.0})

        r2 = client.post(f"{API}/bot/signal", json={"events": events}, timeout=15)
        assert r2.status_code == 200

        d = client.get(f"{API}/bot/status", timeout=15).json()
        pending = {s["symbol"]: s for s in d["pendingStraddles"]}
        for sym in symbols:
            assert sym in pending, f"{sym} not armed as straddle; pending={list(pending)}"
            s = pending[sym]
            mark = s["mark"]
            # Long ~ +5% above, short ~ -5% below (allow small float rounding)
            assert abs(s["longEntry"] - mark * 1.05) < mark * 1e-6
            assert abs(s["shortEntry"] - mark * 0.95) < mark * 1e-6
        # No open position yet (very unlikely BTC/ETH move ±5% within a second)
        for p in d["openPositions"]:
            assert p["symbol"] not in symbols, f"unexpected fill: {p}"

        # Cleanup
        client.post(f"{API}/bot/close-all", timeout=15)


# ---------- FILL + OCO: tiny band → same-tick fill, only one leg ----------
class TestStraddleFillOCO:
    def test_tiny_band_fill_and_oco(self, client):
        client.post(f"{API}/bot/close-all", timeout=15)
        r = client.post(f"{API}/bot/config", json={
            "enabled": True, "dryRun": True,
            "straddleEnabled": True,
            "straddleEntryPct": 0.000001,  # microscopic
            "straddleTpPct": 10.0, "straddleSlPct": 5.0,
            "minVolumeUsd": 0, "maxVolumeUsd": 0,
            "cooldownSec": 0, "maxOpenPositions": 10,
            "autoExit": False, "maxLossPerTradeUsdt": 0.0,
        }, timeout=15)
        assert r.status_code == 200

        sym = "ETHUSDT"
        p = _mark(client, sym)
        assert p and p > 0
        client.post(f"{API}/bot/signal", json={
            "events": [{"symbol": sym, "side": "buy", "price": p, "volume": 0.0}]
        }, timeout=15)

        # Poll up to 6s for straddle_fill
        filled = None
        deadline = time.time() + 6
        while time.time() < deadline:
            d = client.get(f"{API}/bot/status", timeout=15).json()
            positions = {pp["symbol"]: pp for pp in d["openPositions"]}
            pending = [s for s in d["pendingStraddles"] if s["symbol"] == sym]
            if sym in positions and not pending:
                filled = positions[sym]
                journal = d["journal"]
                break
            time.sleep(0.7)

        assert filled is not None, "straddle never filled with tiny band"
        assert filled["side"] in ("long", "short")
        assert filled["source"] == "straddle"

        # Verify journal contains straddle -> straddle_fill -> entry(source=straddle)
        # journal is newest-first (server slices list(BOT["journal"])[:100])
        kinds = [e.get("kind") for e in journal if e.get("symbol") == sym]
        assert "straddle" in kinds, f"missing straddle-arm log: {kinds}"
        assert "straddle_fill" in kinds, f"missing straddle_fill log: {kinds}"
        assert "entry" in kinds, f"missing entry log: {kinds}"
        # ordering (newest first): entry, straddle_fill, straddle
        # find first (newest) entry and ensure a straddle exists after it in list
        idx_entry = next(i for i, e in enumerate(journal)
                         if e.get("symbol") == sym and e.get("kind") == "entry")
        idx_fill = next(i for i, e in enumerate(journal)
                        if e.get("symbol") == sym and e.get("kind") == "straddle_fill")
        idx_arm = next(i for i, e in enumerate(journal)
                       if e.get("symbol") == sym and e.get("kind") == "straddle")
        assert idx_entry < idx_fill < idx_arm, (
            f"journal order wrong (newest-first): entry@{idx_entry}, fill@{idx_fill}, arm@{idx_arm}"
        )
        entry_ev = journal[idx_entry]
        assert entry_ev.get("source") == "straddle"

        # Only one position for this symbol (OCO enforced)
        d2 = client.get(f"{API}/bot/status", timeout=15).json()
        same_sym = [p for p in d2["openPositions"] if p["symbol"] == sym]
        assert len(same_sym) == 1

        # Cleanup
        client.post(f"{API}/bot/close-all", timeout=15)


# ---------- Short-side correctness (best-effort) ----------
class TestStraddleShortSide:
    def test_short_leg_or_long_only(self, client):
        client.post(f"{API}/bot/close-all", timeout=15)
        r = client.post(f"{API}/bot/config", json={
            "enabled": True, "dryRun": True,
            "straddleEnabled": True,
            "straddleEntryPct": 0.000001,
            "straddleTpPct": 10.0, "straddleSlPct": 5.0,
            "minVolumeUsd": 0, "maxVolumeUsd": 0,
            "cooldownSec": 0, "maxOpenPositions": 20,
            "autoExit": False, "maxLossPerTradeUsdt": 0.0,
        }, timeout=15)
        assert r.status_code == 200

        symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                   "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT"]
        events = []
        for sym in symbols:
            p = _mark(client, sym)
            if p and p > 0:
                events.append({"symbol": sym, "side": "buy", "price": p, "volume": 0.0})
        client.post(f"{API}/bot/signal", json={"events": events}, timeout=15)

        # Wait a few polls
        time.sleep(4)
        d = client.get(f"{API}/bot/status", timeout=15).json()
        shorts = [p for p in d["openPositions"] if p["side"] == "short"]
        longs = [p for p in d["openPositions"] if p["side"] == "long"]

        # Long leg working proven by earlier test; check any shorts for uPnl sign.
        for p in shorts:
            # short profits when currentPrice < entryPrice
            if p["currentPrice"] < p["entryPrice"]:
                assert p["uPnl"] >= 0, f"short uPnl sign wrong: {p}"
            elif p["currentPrice"] > p["entryPrice"]:
                assert p["uPnl"] <= 0, f"short uPnl sign wrong: {p}"

        # Attempt close-all and verify short exits use side='cover' with proper pnl
        if shorts:
            close_r = client.post(f"{API}/bot/close-all", timeout=15)
            assert close_r.status_code == 200
            d2 = close_r.json()
            journal = d2["journal"]
            # Find any exit for a short symbol
            for p in shorts:
                exit_ev = next((e for e in journal
                                if e.get("kind") == "exit" and e.get("symbol") == p["symbol"]), None)
                if exit_ev is not None:
                    assert exit_ev.get("side") == "cover", f"short exit should be cover: {exit_ev}"
                    # pnl sign: (entry - fill) * qty (for short)
                    fill = exit_ev.get("price")
                    entry = exit_ev.get("entry")
                    qty = exit_ev.get("qty")
                    expected = (entry - fill) * qty
                    assert abs(exit_ev.get("pnl") - expected) < 1e-6, (
                        f"pnl mismatch: got {exit_ev.get('pnl')} expected {expected}"
                    )
            print(f"Short-side path exercised: {len(shorts)} short(s) filled.")
        else:
            print(f"Short path could not be triggered deterministically "
                  f"(only longs filled: {len(longs)}). Long+OCO already verified elsewhere.")

        # Cleanup
        client.post(f"{API}/bot/close-all", timeout=15)


# ---------- Close-all cleanup ----------
class TestCloseAllCleanup:
    def test_close_all_clears_positions_and_straddles(self, client):
        # Arm one wide-band straddle to have a pending one
        client.post(f"{API}/bot/close-all", timeout=15)
        client.post(f"{API}/bot/config", json={
            "enabled": True, "dryRun": True,
            "straddleEnabled": True, "straddleEntryPct": 5.0,
            "straddleTpPct": 10.0, "straddleSlPct": 5.0,
            "minVolumeUsd": 0, "cooldownSec": 0, "maxOpenPositions": 10,
            "autoExit": False,
        }, timeout=15)
        sym = "ETHUSDT"
        p = _mark(client, sym)
        client.post(f"{API}/bot/signal", json={
            "events": [{"symbol": sym, "side": "buy", "price": p, "volume": 0.0}]
        }, timeout=15)
        d1 = client.get(f"{API}/bot/status", timeout=15).json()
        assert any(s["symbol"] == sym for s in d1["pendingStraddles"])

        r = client.post(f"{API}/bot/close-all", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["openPositions"] == []
        assert d["pendingStraddles"] == []
        # kill_switch mention of cancelled straddle(s)
        ks = next((e for e in d["journal"] if e.get("kind") == "kill_switch"), None)
        assert ks is not None
        assert "straddle" in (ks.get("message") or "").lower()


# ---------- Regression: normal (non-straddle) mode still works ----------
class TestNonStraddleRegression:
    def test_normal_long_entry(self, client):
        client.post(f"{API}/bot/close-all", timeout=15)
        r = client.post(f"{API}/bot/config", json={
            "enabled": True, "dryRun": True,
            "straddleEnabled": False,
            "streak": 1, "minVolumeUsd": 0, "maxVolumeUsd": 0,
            "cooldownSec": 0, "autoExit": True, "maxOpenPositions": 5,
            "maxLossPerTradeUsdt": 0.0,
        }, timeout=15)
        assert r.status_code == 200

        sym = "ETHUSDT"
        p = _mark(client, sym)
        client.post(f"{API}/bot/signal", json={
            "events": [{"symbol": sym, "side": "buy", "price": p, "volume": 0.0}]
        }, timeout=15)

        # Give a moment for handle_signal
        time.sleep(1.2)
        d = client.get(f"{API}/bot/status", timeout=15).json()
        pos = next((pp for pp in d["openPositions"] if pp["symbol"] == sym), None)
        assert pos is not None, f"normal long entry did not fire: {d['openPositions']}"
        assert pos["side"] == "long"
        assert pos["source"] == "signal"

        # Cleanup
        client.post(f"{API}/bot/close-all", timeout=15)
