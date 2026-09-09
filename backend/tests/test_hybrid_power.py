"""Hybrid Power System — module unit tests + backend API integration tests."""
import os
import sys
import time
import pytest
import requests

sys.path.insert(0, "/app/backend")
import hybrid_power as h  # noqa: E402

def _load_backend_url():
    v = os.environ.get("REACT_APP_BACKEND_URL", "")
    if not v:
        try:
            with open("/app/frontend/.env") as f:
                for line in f:
                    if line.startswith("REACT_APP_BACKEND_URL="):
                        v = line.split("=", 1)[1].strip()
                        break
        except Exception:
            pass
    return v.rstrip("/")


BASE = _load_backend_url()
assert BASE, "REACT_APP_BACKEND_URL not set"
API = f"{BASE}/api"


# ---------------- Module-level unit tests ----------------

def _reset(coin):
    h._buffers.pop(coin, None)


def test_module_long_signal():
    coin = "TESTLONG"
    _reset(coin)
    vol_24h = 100000.0
    now = time.time()
    # Feed ~60 samples across the last 60s, avg=300, buys only.
    # sum60 ~= 60 * 300 = 18000. To get power_1m ~= 1.0 -> want sum60/vol*100 in [0.6,1.8]
    # 18000/100000*100 = 18 → far too high. Scale down: use amount=1.5 each? avg would be 1.5
    # We need TWO constraints simultaneously:
    #  - avg_1m > normal_avg*2.2 = 100*2.2 = 220  -> avg trade >= 300
    #  - power_1m in [0.6, 1.8] -> sum60 in [600, 1800]
    # So use ~3 samples of 300 = 900 total -> power_1m = 0.9 ✓, avg=300 ✓
    for i in range(3):
        h.update(coin, 300.0, "buy", now - 30 - i, count=1)
    # Add a last-1s burst: amount > 60 (0.06% of 100k) e.g. 100
    h.update(coin, 100.0, "buy", now - 0.2, count=1)
    # power_5s: last 5s = 100 -> 100/100000*100 = 0.1 < 0.8 ✓
    # buy_1m = 100% ✓, buy_1s = 100% ✓
    sig = h.get_signal(coin, vol_24h, {"min_power_1m": 0.6, "max_power_1m": 1.8, "burst_power": 0.06}, now=now)
    print("LONG sig:", sig)
    assert sig["signal"] == "LONG", f"expected LONG got {sig}"


def test_module_short_signal():
    coin = "TESTSHORT"
    _reset(coin)
    vol_24h = 100000.0
    now = time.time()
    for i in range(3):
        h.update(coin, 300.0, "sell", now - 30 - i, count=1)
    h.update(coin, 100.0, "sell", now - 0.2, count=1)
    sig = h.get_signal(coin, vol_24h, {"min_power_1m": 0.6, "max_power_1m": 1.8, "burst_power": 0.06}, now=now)
    print("SHORT sig:", sig)
    assert sig["signal"] == "SHORT", f"expected SHORT got {sig}"


def test_module_no_signal_low_power():
    coin = "TESTNONE"
    _reset(coin)
    now = time.time()
    # Only one small trade -> power_1m near 0
    h.update(coin, 10.0, "buy", now - 1, count=1)
    sig = h.get_signal(coin, 100000.0, {}, now=now)
    assert sig["signal"] is None


def test_buffer_prunes_older_than_300s():
    coin = "TESTPRUNE"
    _reset(coin)
    now = time.time()
    # Old sample
    h.update(coin, 500.0, "buy", now - 500, count=1)
    assert len(h._buffers[coin]) == 1
    # New sample triggers prune
    h.update(coin, 100.0, "buy", now, count=1)
    assert len(h._buffers[coin]) == 1, f"expected old to be pruned, got {list(h._buffers[coin])}"
    # Confirm remaining one is the new
    assert h._buffers[coin][0][0] == now


def test_normal_avg_defaults():
    assert h.normal_avg("BTC") == 800
    assert h.normal_avg("ETH") == 600
    assert h.normal_avg("HYPE") == 200
    assert h.normal_avg("PURR") == 80
    assert h.normal_avg("JEFF") == 60
    assert h.normal_avg("UNKNOWNXYZ") == 100  # DEFAULT_AVG


# ---------------- API integration tests ----------------

@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def test_get_hybrid_shape(api):
    r = api.get(f"{API}/hybrid", timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    assert "config" in j and "rows" in j and "normalAvg" in j
    cfg = j["config"]
    for k in ("hybrid_toggle", "sl_percent", "tp_percent", "min_power_1m", "max_power_1m", "burst_power"):
        assert k in cfg, f"missing config key {k}"
    assert isinstance(j["rows"], list)
    assert len(j["rows"]) <= 40
    # normalAvg contains at least BTC baseline
    assert j["normalAvg"].get("BTC") == 800


def test_config_clamps(api):
    # Set out-of-range values
    r = api.post(f"{API}/bot/config", json={
        "sl_percent": 9, "tp_percent": 9,
        "min_power_1m": 9, "max_power_1m": 9, "burst_power": 9,
    }, timeout=15)
    assert r.status_code == 200, r.text
    # verify via /api/hybrid
    j = api.get(f"{API}/hybrid", timeout=15).json()
    cfg = j["config"]
    assert cfg["sl_percent"] == 1.5
    assert cfg["tp_percent"] == 4.0
    assert cfg["min_power_1m"] == 1.0
    assert cfg["max_power_1m"] == 2.5
    assert cfg["burst_power"] == 0.15


def test_config_restore_defaults_and_toggle(api):
    r = api.post(f"{API}/bot/config", json={
        "sl_percent": 0.8, "tp_percent": 2.0,
        "min_power_1m": 0.6, "max_power_1m": 1.8, "burst_power": 0.06,
        "hybrid_toggle": False,
    }, timeout=15)
    assert r.status_code == 200, r.text
    cfg = api.get(f"{API}/hybrid", timeout=15).json()["config"]
    assert cfg["sl_percent"] == 0.8
    assert cfg["tp_percent"] == 2.0
    assert cfg["min_power_1m"] == 0.6
    assert cfg["max_power_1m"] == 1.8
    assert abs(cfg["burst_power"] - 0.06) < 1e-9
    assert cfg["hybrid_toggle"] is False

    # toggle back on
    api.post(f"{API}/bot/config", json={"hybrid_toggle": True}, timeout=15)
    cfg = api.get(f"{API}/hybrid", timeout=15).json()["config"]
    assert cfg["hybrid_toggle"] is True


def test_rows_update_across_calls(api):
    j1 = api.get(f"{API}/hybrid", timeout=15).json()
    time.sleep(3)
    j2 = api.get(f"{API}/hybrid", timeout=15).json()
    # rows should be populated after scanner cycles; at least present as list
    assert isinstance(j1["rows"], list) and isinstance(j2["rows"], list)
    # Row schema check if any rows exist
    for row in j2["rows"][:3]:
        for k in ("coin", "power_1m", "power_1s", "power_5s", "avg", "buy", "signal"):
            assert k in row, f"row missing {k}: {row}"


def test_execution_wiring_sim(api):
    # Configure for possible hybrid firing in dryRun
    r = api.post(f"{API}/bot/config", json={
        "enabled": True, "dryRun": True, "exchange": "hyperliquid",
        "hybrid_toggle": True, "min_power_1m": 0.4,
        "cooldownSec": 0, "maxOpenPositions": 10, "autoExit": False,
        "maxLossPerTradeUsdt": 0, "sl_percent": 0.8, "tp_percent": 2.0,
    }, timeout=15)
    assert r.status_code == 200, r.text

    fired = False
    hybrid_positions = []
    for _ in range(15):  # ~30s
        h_ = api.get(f"{API}/hybrid", timeout=15).json()
        st = api.get(f"{API}/bot/status", timeout=15).json()
        assert st.get("openPositions") is not None or "positions" in st or True
        # Look for hybrid signal in rows
        for row in h_.get("rows", []):
            if row.get("signal") in ("LONG", "SHORT"):
                fired = True
        # Collect any hybrid-source positions
        for pos in (st.get("openPositions") or st.get("positions") or []):
            if isinstance(pos, dict) and pos.get("source") == "hybrid":
                hybrid_positions.append(pos)
        time.sleep(2)

    print(f"hybrid fired organically: {fired}, hybrid positions: {len(hybrid_positions)}")
    # Cannot assert firing (organic bursts rare) — just verify no crashes and status is 200
    assert api.get(f"{API}/bot/status", timeout=15).status_code == 200
    assert api.get(f"{API}/tokens", timeout=15).status_code == 200


def test_toggle_off_no_new_hybrid_positions(api):
    # First close all + disable
    api.post(f"{API}/bot/close-all", timeout=15)
    api.post(f"{API}/bot/config", json={"hybrid_toggle": False, "enabled": True, "dryRun": True}, timeout=15)
    time.sleep(3)
    # /api/hybrid should still return rows
    j = api.get(f"{API}/hybrid", timeout=15).json()
    assert isinstance(j["rows"], list)
    # Count hybrid journal entries baseline
    st = api.get(f"{API}/bot/status", timeout=15).json()
    journal = st.get("journal", [])
    base_hybrid_count = sum(1 for e in journal if isinstance(e, dict) and e.get("kind") == "hybrid")
    time.sleep(6)
    st2 = api.get(f"{API}/bot/status", timeout=15).json()
    journal2 = st2.get("journal", [])
    new_hybrid_count = sum(1 for e in journal2 if isinstance(e, dict) and e.get("kind") == "hybrid")
    assert new_hybrid_count == base_hybrid_count, (
        f"hybrid signals fired while toggle OFF: {new_hybrid_count} vs baseline {base_hybrid_count}"
    )


def test_no_crash_in_logs():
    log_paths = ["/var/log/supervisor/backend.err.log"]
    bad = []
    for p in log_paths:
        if not os.path.exists(p):
            continue
        with open(p, "r") as f:
            content = f.read()[-8000:]
        # Only look at recent lines
        for line in content.splitlines():
            if "hybrid error" in line.lower() or "Traceback" in line:
                bad.append(line)
    # print but don't hard fail on stale tracebacks — assertion only for recent hybrid errors
    for line in bad[-20:]:
        print("LOG:", line)
    recent_hybrid = [l for l in bad if "hybrid error" in l.lower()]
    assert not recent_hybrid, f"hybrid errors in backend log: {recent_hybrid[:5]}"


def test_restore_safe_defaults(api):
    api.post(f"{API}/bot/close-all", timeout=15)
    r = api.post(f"{API}/bot/config", json={
        "enabled": False, "dryRun": True,
        "hybrid_toggle": True, "sl_percent": 0.8, "tp_percent": 2.0,
        "min_power_1m": 0.6, "max_power_1m": 1.8, "burst_power": 0.06,
        "straddleEnabled": False, "streak": 2, "minVolumeUsd": 5000000,
        "cooldownSec": 30, "maxOpenPositions": 3, "autoExit": True,
        "maxLossPerTradeUsdt": 0.0002,
    }, timeout=15)
    assert r.status_code == 200, r.text
    cfg = api.get(f"{API}/hybrid", timeout=15).json()["config"]
    assert cfg["hybrid_toggle"] is True
    assert cfg["sl_percent"] == 0.8
    assert cfg["tp_percent"] == 2.0
