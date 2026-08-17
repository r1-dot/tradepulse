"""Auto-trade bot backend tests (SIM/dryRun only — never LIVE)."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL')
if not BASE_URL:
    # fallback to frontend env
    with open('/app/frontend/.env') as f:
        for line in f:
            if line.startswith('REACT_APP_BACKEND_URL='):
                BASE_URL = line.split('=', 1)[1].strip()
                break
BASE_URL = BASE_URL.rstrip('/')
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    yield s
    # ensure safe cleanup
    try:
        s.post(f"{API}/bot/close-all", timeout=15)
        s.post(f"{API}/bot/config", json={
            "enabled": False, "dryRun": True,
            "volThresholdUsd": 100000000, "maxOpenPositions": 3, "cooldownSec": 30,
        }, timeout=15)
    except Exception:
        pass


def test_status_default_shape(client):
    r = client.get(f"{API}/bot/status", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert "config" in d and "stopped" in d and "dailyPnl" in d
    assert "openPositions" in d and isinstance(d["openPositions"], list)
    assert "journal" in d and isinstance(d["journal"], list)
    assert "keysConfigured" in d and d["keysConfigured"] is False
    assert "watching" in d and isinstance(d["watching"], int)
    c = d["config"]
    for k in ("enabled", "dryRun", "slPct", "tpPct", "maxPositionUsdt",
              "dailyLossLimit", "volThresholdUsd", "streak",
              "maxOpenPositions", "cooldownSec"):
        assert k in c, f"missing config key: {k}"


def test_config_update_and_clamps(client):
    # First: reset to safe defaults
    r = client.post(f"{API}/bot/config", json={
        "enabled": False, "dryRun": True,
        "volThresholdUsd": 100000000, "maxOpenPositions": 3,
        "cooldownSec": 30, "slPct": 1.5, "tpPct": 2.0,
        "maxPositionUsdt": 5, "dailyLossLimit": 5, "streak": 2,
    }, timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["config"]["dryRun"] is True
    assert d["config"]["volThresholdUsd"] == 100000000

    # Clamps: streak min 2, cooldown >= 0, slPct >= 0.1
    r = client.post(f"{API}/bot/config", json={
        "streak": 1, "cooldownSec": -5, "slPct": 0.0001, "tpPct": 0.05,
        "maxOpenPositions": 0, "maxPositionUsdt": 0, "dailyLossLimit": 0,
    }, timeout=15)
    assert r.status_code == 200
    c = r.json()["config"]
    assert c["streak"] >= 2
    assert c["cooldownSec"] >= 0
    assert c["slPct"] >= 0.1
    assert c["tpPct"] >= 0.1
    assert c["maxOpenPositions"] >= 1
    assert c["maxPositionUsdt"] >= 1
    assert c["dailyLossLimit"] >= 0.5


def test_enable_clears_stopped(client):
    r = client.post(f"{API}/bot/config", json={"enabled": True, "dryRun": True}, timeout=15)
    assert r.status_code == 200
    assert r.json()["stopped"] is False
    # journal has a power entry
    j = r.json()["journal"]
    assert any(e.get("kind") == "power" for e in j)


def test_sim_trigger_creates_positions(client):
    # Enable with low threshold + small cooldown + dryRun
    r = client.post(f"{API}/bot/config", json={
        "enabled": True, "dryRun": True,
        "volThresholdUsd": 5000000,
        "streak": 2, "cooldownSec": 2,
        "maxOpenPositions": 5, "maxPositionUsdt": 5,
        "tpPct": 2.0, "slPct": 1.5,
    }, timeout=15)
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg["enabled"] and cfg["dryRun"]
    assert cfg["volThresholdUsd"] == 5000000
    # watching should now include many pairs
    assert r.json()["watching"] >= 1

    # Wait for bot to fire on live polls
    positions = []
    entry_events = 0
    for _ in range(14):
        time.sleep(1)
        d = client.get(f"{API}/bot/status", timeout=15).json()
        positions = d["openPositions"]
        entry_events = sum(1 for e in d["journal"] if e.get("kind") == "entry")
        if positions or entry_events:
            break

    # Verify at least one entry occurred
    assert entry_events >= 1 or len(positions) >= 1, "no bot activity observed within ~14s"
    d = client.get(f"{API}/bot/status", timeout=15).json()
    # maxOpenPositions honored
    assert len(d["openPositions"]) <= d["config"]["maxOpenPositions"]

    # If any position open, validate shape
    if d["openPositions"]:
        p = d["openPositions"][0]
        assert p["entryPrice"] > 0
        assert p["qty"] > 0
        assert abs(p["tpPrice"] - p["entryPrice"] * 1.02) / p["entryPrice"] < 1e-6
        assert abs(p["slPrice"] - p["entryPrice"] * 0.985) / p["entryPrice"] < 1e-6
        assert p["mode"] == "SIM"
    # journal 'entry' events should be marked SIM
    for e in d["journal"]:
        if e.get("kind") == "entry":
            assert e.get("mode") == "SIM"


def test_close_all(client):
    r = client.post(f"{API}/bot/close-all", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["openPositions"] == []
    # journal has kill_switch
    assert any(e.get("kind") == "kill_switch" for e in d["journal"])


def test_reset_daily(client):
    r = client.post(f"{API}/bot/reset-daily", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["dailyPnl"] == 0.0
    assert d["stopped"] is False


def test_cleanup_leaves_bot_safe(client):
    client.post(f"{API}/bot/close-all", timeout=15)
    r = client.post(f"{API}/bot/config", json={
        "enabled": False, "dryRun": True,
        "volThresholdUsd": 100000000, "maxOpenPositions": 3, "cooldownSec": 30,
    }, timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["config"]["enabled"] is False
    assert d["config"]["dryRun"] is True
    assert d["config"]["volThresholdUsd"] == 100000000
    assert d["openPositions"] == []
