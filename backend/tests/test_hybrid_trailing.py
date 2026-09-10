"""Tests for hybrid orderbook delta filter + smart trailing TP (iteration 17)."""
import os
import sys
import time

import pytest
import requests

def _load_base():
    b = os.environ.get("REACT_APP_BACKEND_URL", "")
    if not b:
        try:
            with open("/app/frontend/.env") as f:
                for line in f:
                    if line.startswith("REACT_APP_BACKEND_URL="):
                        b = line.split("=", 1)[1].strip()
                        break
        except Exception:
            pass
    return b.rstrip("/")

BASE = _load_base()
assert BASE, "REACT_APP_BACKEND_URL must be set"

sys.path.insert(0, "/app/backend")
import hybrid_power as h  # noqa: E402


# ---------- UNIT: trail_stop_offset ----------
class TestTrailStopOffset:
    def test_below_secure(self):
        assert h.trail_stop_offset(0.30, 0.40, 0.05, 0.5) is None

    def test_at_secure(self):
        assert h.trail_stop_offset(0.40, 0.40, 0.05, 0.5) == 0.05

    def test_step1(self):
        assert h.trail_stop_offset(0.90, 0.40, 0.05, 0.5) == pytest.approx(0.45)

    def test_step2(self):
        assert h.trail_stop_offset(1.40, 0.40, 0.05, 0.5) == pytest.approx(0.95)


# ---------- Orderbook endpoint ----------
class TestOrderbook:
    @pytest.mark.parametrize("sym", ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    def test_orderbook(self, sym):
        r = requests.get(f"{BASE}/api/hybrid/orderbook", params={"symbol": sym}, timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ("buyWall", "sellWall", "deltaLong", "deltaShort"):
            assert k in d, f"missing {k}"
            assert isinstance(d[k], (int, float))
            assert d[k] >= 0
        # deltaLong approx 1/deltaShort
        if d["deltaShort"] > 0:
            assert d["deltaLong"] == pytest.approx(1.0 / d["deltaShort"], rel=1e-3)


# ---------- Config keys + clamps ----------
DEFAULTS = {
    "obDeltaFilter": True,
    "minDeltaLong": 1.5,
    "minDeltaShort": 1.5,
    "minPwr1m": 0.65,
    "trailEnabled": True,
    "trailInitialSlPct": 0.30,
    "trailSecure": 0.40,
    "trailBE": 0.05,
    "trailStep": 0.5,
    "trailLock": 50,
    "trailCallback": 0.30,
}


def _post_cfg(payload):
    r = requests.post(f"{BASE}/api/bot/config", json=payload, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def _get_hybrid():
    r = requests.get(f"{BASE}/api/hybrid", timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


class TestConfigAndClamps:
    def test_hybrid_config_has_all_keys(self):
        data = _get_hybrid()
        cfg = data.get("config", data)
        for k in DEFAULTS:
            assert k in cfg, f"missing hybrid config key: {k}"

    def test_rows_present(self):
        d = _get_hybrid()
        assert "rows" in d
        assert isinstance(d["rows"], list)

    def test_obdelta_toggle_persists(self):
        _post_cfg({"obDeltaFilter": False})
        cfg = _get_hybrid().get("config", {})
        assert cfg["obDeltaFilter"] is False
        _post_cfg({"obDeltaFilter": True})
        cfg = _get_hybrid().get("config", {})
        assert cfg["obDeltaFilter"] is True

    def test_clamps(self):
        _post_cfg({
            "minDeltaLong": 999, "minDeltaShort": 999,
            "minPwr1m": 9,
            "trailInitialSlPct": 9, "trailSecure": 9, "trailStep": 9,
            "trailCallback": 9, "trailLock": 999,
        })
        cfg = _get_hybrid().get("config", {})
        assert cfg["minDeltaLong"] == 100
        assert cfg["minDeltaShort"] == 100
        assert cfg["minPwr1m"] == 5
        assert cfg["trailInitialSlPct"] == 5
        assert cfg["trailSecure"] == 5
        assert cfg["trailStep"] == 5
        assert cfg["trailCallback"] == 5
        assert cfg["trailLock"] == 100

    def test_restore_defaults(self):
        _post_cfg(DEFAULTS)
        cfg = _get_hybrid().get("config", {})
        for k, v in DEFAULTS.items():
            assert cfg[k] == v, f"{k}: got {cfg[k]} vs {v}"


# ---------- Execution filter (best-effort) ----------
class TestExecutionFilter:
    def test_run_sim_briefly(self):
        _post_cfg({
            "enabled": True, "dryRun": True, "exchange": "hyperliquid",
            "hybrid_toggle": True, "obDeltaFilter": True,
            "minPwr1m": 0.4, "cooldownSec": 0, "maxOpenPositions": 10, "autoExit": False,
        })
        end = time.time() + 30
        hybrid_entries = []
        while time.time() < end:
            r = requests.get(f"{BASE}/api/hybrid", timeout=10)
            if r.status_code == 200:
                journal = r.json().get("journal") or r.json().get("log") or []
                hybrid_entries = [e for e in journal if isinstance(e, dict)
                                  and e.get("kind") in ("hybrid", "skip")]
            time.sleep(2)
        # if any hybrid entry appeared, validate format
        for e in hybrid_entries:
            msg = (e.get("msg") or e.get("message") or "").lower()
            if e.get("kind") == "hybrid":
                assert "delta" in msg, f"hybrid entry missing 'delta': {msg}"
                assert "trade" in msg or "skip" in msg, msg
            if "skip" in msg:
                assert "weak wall" in msg or "weak" in msg
        print(f"hybrid entries observed: {len(hybrid_entries)}")

    def test_status_and_tokens_ok(self):
        assert requests.get(f"{BASE}/api/bot/status", timeout=10).status_code == 200
        assert requests.get(f"{BASE}/api/tokens", timeout=15).status_code == 200


# ---------- Cleanup ----------
class TestZCleanup:
    def test_close_and_restore(self):
        try:
            requests.post(f"{BASE}/api/bot/close-all", timeout=10)
        except Exception:
            pass
        _post_cfg({
            "enabled": False, "dryRun": True, "hybrid_toggle": True,
            "obDeltaFilter": True, "minDeltaLong": 1.5, "minDeltaShort": 1.5,
            "minPwr1m": 0.65, "trailEnabled": True, "trailInitialSlPct": 0.30,
            "trailSecure": 0.40, "trailBE": 0.05, "trailStep": 0.5,
            "trailLock": 50, "trailCallback": 0.30,
            "straddleEnabled": False, "streak": 2, "minVolumeUsd": 5000000,
            "cooldownSec": 30, "maxOpenPositions": 3, "autoExit": True,
            "maxLossPerTradeUsdt": 0.0002,
        })
        cfg = _get_hybrid().get("config", {})
        assert cfg["obDeltaFilter"] is True
        assert cfg["minDeltaLong"] == 1.5
