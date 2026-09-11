"""Straddle System now uses Smart Trailing TP (shared with Hybrid).

Verifies:
  1. A filled straddle leg opens a position with tpPrice=None and SL derived
     from cfg['trailInitialSlPct'] (NOT the old fixed straddleTpPct/straddleSlPct).
  2. The straddle-arm journal message references Smart Trailing TP / init SL,
     not the old "TP x% / SL y%" text.
  3. POST /api/bot/config accepts + clamps all trail* fields and straddleEntryPct.
  4. Regression: normal (non-straddle) BUY signal still opens a fixed-TP long position.
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
    # cleanup: restore safe defaults
    try:
        s.post(f"{API}/bot/close-all", timeout=15)
        s.post(f"{API}/bot/config", json={
            "enabled": False, "dryRun": True, "straddleEnabled": False,
            "straddleEntryPct": 0.5,
            "trailEnabled": True, "trailInitialSlPct": 0.30, "trailSecure": 0.40,
            "trailBE": 0.05, "trailStep": 0.5, "trailCallback": 0.30,
            "streak": 2, "minVolumeUsd": 5_000_000, "maxVolumeUsd": 0,
            "cooldownSec": 30, "maxOpenPositions": 3, "autoExit": True,
            "maxLossPerTradeUsdt": 0.0002,
        }, timeout=15)
    except Exception:
        pass


def _mark(client, sym):
    r = client.get(f"{API}/tokens", params={"timeframe": "1s", "quote": "USDT", "limit": 1500}, timeout=15)
    for t in r.json().get("tokens", []):
        if t["symbol"] == sym:
            return float(t["price"])
    return None


# ---------- 1. Config: trail* accepted + clamped, plus straddleEntryPct ----------
class TestTrailConfig:
    def test_trail_fields_accepted_and_persisted(self, client):
        payload = {
            "trailEnabled": True,
            "trailInitialSlPct": 0.25,
            "trailSecure": 0.50,
            "trailBE": 0.10,
            "trailStep": 0.40,
            "trailCallback": 0.35,
            "straddleEntryPct": 0.7,
        }
        r = client.post(f"{API}/bot/config", json=payload, timeout=15)
        assert r.status_code == 200
        cfg = r.json()["config"]
        for k, v in payload.items():
            if isinstance(v, bool):
                assert cfg[k] == v
            else:
                assert abs(cfg[k] - v) < 1e-9, f"{k}={cfg[k]} expected {v}"

        # confirm on GET status
        d = client.get(f"{API}/bot/status", timeout=15).json()
        for k, v in payload.items():
            if isinstance(v, bool):
                assert d["config"][k] == v
            else:
                assert abs(d["config"][k] - v) < 1e-9

    def test_trail_fields_clamped(self, client):
        # out-of-range values clamped: trail* to [0.05, 5.0] (mostly), straddleEntryPct to [1e-6, 5]
        r = client.post(f"{API}/bot/config", json={
            "trailInitialSlPct": 999,  # -> 5.0
            "trailSecure": 999,        # -> 5.0
            "trailBE": 999,            # -> 2.0
            "trailStep": 999,          # -> 5.0
            "trailCallback": 999,      # -> 5.0
            "straddleEntryPct": 999,   # -> 5.0
        }, timeout=15)
        assert r.status_code == 200
        c = r.json()["config"]
        assert c["trailInitialSlPct"] == 5.0
        assert c["trailSecure"] == 5.0
        assert c["trailBE"] == 2.0
        assert c["trailStep"] == 5.0
        assert c["trailCallback"] == 5.0
        assert c["straddleEntryPct"] == 5.0

        # lower clamp
        r2 = client.post(f"{API}/bot/config", json={
            "trailInitialSlPct": 0.0001,   # -> 0.05
            "trailStep": 0.0001,           # -> 0.05
            "trailCallback": 0.0001,       # -> 0.05
            "straddleEntryPct": 0.0,       # -> 1e-6
        }, timeout=15)
        c2 = r2.json()["config"]
        assert c2["trailInitialSlPct"] == 0.05
        assert c2["trailStep"] == 0.05
        assert c2["trailCallback"] == 0.05
        assert abs(c2["straddleEntryPct"] - 1e-6) < 1e-12


# ---------- 2. Straddle-arm journal message no longer says "TP x% / SL y%" ----------
class TestStraddleArmJournal:
    def test_arm_journal_mentions_smart_trailing_not_fixed_tp_sl(self, client):
        client.post(f"{API}/bot/close-all", timeout=15)
        client.post(f"{API}/bot/config", json={
            "enabled": True, "dryRun": True, "straddleEnabled": True,
            "straddleEntryPct": 5.0,   # wide -> stays pending
            "trailInitialSlPct": 0.30,
            "minVolumeUsd": 0, "maxVolumeUsd": 0, "cooldownSec": 0,
            "maxOpenPositions": 10, "autoExit": False,
            "maxLossPerTradeUsdt": 0.0,
        }, timeout=15)

        sym = "ETHUSDT"
        p = _mark(client, sym)
        assert p and p > 0
        client.post(f"{API}/bot/signal", json={
            "events": [{"symbol": sym, "side": "buy", "price": p, "volume": 0.0}]
        }, timeout=15)

        d = client.get(f"{API}/bot/status", timeout=15).json()
        arm = next((e for e in d["journal"]
                    if e.get("kind") == "straddle" and e.get("symbol") == sym), None)
        assert arm is not None, f"no straddle-arm log for {sym}"
        msg = arm.get("message", "")
        # New wording asserts
        assert "Smart Trailing TP" in msg, f"expected 'Smart Trailing TP' in msg, got: {msg!r}"
        assert "init SL" in msg.lower() or "init sl" in msg.lower(), f"expected initial SL reference, got: {msg!r}"
        # Old wording MUST be gone
        assert "TP " not in msg.replace("Smart Trailing TP", ""), f"old TP wording still present: {msg!r}"

        client.post(f"{API}/bot/close-all", timeout=15)


# ---------- 3. Filled straddle leg -> tpPrice=None, SL from trailInitialSlPct ----------
class TestStraddleFillUsesTrailing:
    def test_filled_leg_has_no_fixed_tp_and_trail_sl(self, client):
        client.post(f"{API}/bot/close-all", timeout=15)
        # tiny band + a specific trailInitialSlPct so we can verify slPrice
        isl = 0.40
        client.post(f"{API}/bot/config", json={
            "enabled": True, "dryRun": True, "straddleEnabled": True,
            "straddleEntryPct": 0.000001,   # microscopic -> fills same tick
            "trailEnabled": True,
            "trailInitialSlPct": isl,
            "trailSecure": 0.50, "trailBE": 0.05, "trailStep": 0.5, "trailCallback": 0.30,
            "minVolumeUsd": 0, "maxVolumeUsd": 0, "cooldownSec": 0,
            "maxOpenPositions": 10, "autoExit": False,
            "maxLossPerTradeUsdt": 0.0,
        }, timeout=15)

        sym = "ETHUSDT"
        p = _mark(client, sym)
        assert p and p > 0
        client.post(f"{API}/bot/signal", json={
            "events": [{"symbol": sym, "side": "buy", "price": p, "volume": 0.0}]
        }, timeout=15)

        filled = None
        deadline = time.time() + 8
        while time.time() < deadline:
            d = client.get(f"{API}/bot/status", timeout=15).json()
            pos = next((pp for pp in d["openPositions"] if pp["symbol"] == sym), None)
            if pos and pos.get("source") == "straddle":
                filled = pos
                break
            time.sleep(0.7)

        assert filled is not None, "straddle never filled"
        # CRITICAL: tpPrice must be None so exit routes through _update_trailing()
        assert filled["tpPrice"] is None, f"straddle-source pos must have tpPrice=None, got {filled['tpPrice']}"
        # slPrice must be tight: entry * (1 - isl/100) for long, (1 + isl/100) for short
        entry = filled["entryPrice"]
        expected_sl = entry * (1 - isl / 100.0) if filled["side"] == "long" else entry * (1 + isl / 100.0)
        assert abs(filled["slPrice"] - expected_sl) / entry < 1e-6, (
            f"slPrice mismatch: got {filled['slPrice']} expected {expected_sl} "
            f"(entry={entry}, side={filled['side']}, isl={isl}%)"
        )

        client.post(f"{API}/bot/close-all", timeout=15)


# ---------- 4. Regression: normal non-straddle BUY still uses fixed TP ----------
class TestNonStraddleRegression:
    def test_normal_signal_still_gets_fixed_tp(self, client):
        client.post(f"{API}/bot/close-all", timeout=15)
        client.post(f"{API}/bot/config", json={
            "enabled": True, "dryRun": True, "straddleEnabled": False,
            "streak": 1, "tpPct": 2.0, "slPct": 1.0,
            "minVolumeUsd": 0, "maxVolumeUsd": 0, "cooldownSec": 0,
            "autoExit": True, "maxOpenPositions": 5,
            "maxLossPerTradeUsdt": 0.0,
        }, timeout=15)

        sym = "BNBUSDT"
        p = _mark(client, sym)
        client.post(f"{API}/bot/signal", json={
            "events": [{"symbol": sym, "side": "buy", "price": p, "volume": 0.0}]
        }, timeout=15)

        time.sleep(1.5)
        d = client.get(f"{API}/bot/status", timeout=15).json()
        pos = next((pp for pp in d["openPositions"] if pp["symbol"] == sym), None)
        assert pos is not None, "normal non-straddle long did not open"
        assert pos["source"] == "signal"
        # regular signal-source pos DOES have a fixed tpPrice (~ +2%)
        assert pos["tpPrice"] is not None
        assert pos["tpPrice"] > pos["entryPrice"]

        client.post(f"{API}/bot/close-all", timeout=15)
