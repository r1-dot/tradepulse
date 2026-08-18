"""Auto-trade bot backend tests — SIGNAL-DRIVEN (SIM/dryRun only, never LIVE)."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL')
if not BASE_URL:
    with open('/app/frontend/.env') as f:
        for line in f:
            if line.startswith('REACT_APP_BACKEND_URL='):
                BASE_URL = line.split('=', 1)[1].strip()
                break
BASE_URL = BASE_URL.rstrip('/')
API = f"{BASE_URL}/api"

CFG_KEYS = ("enabled", "dryRun", "slPct", "tpPct", "maxPositionUsdt",
            "dailyLossLimit", "streak", "maxOpenPositions", "cooldownSec", "minVolumeUsd")

HIGH_VOL = 10_000_000_000.0  # bypass volume gate in signal tests


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    yield s
    try:
        s.post(f"{API}/bot/close-all", timeout=15)
        s.post(f"{API}/bot/config", json={"enabled": False, "dryRun": True}, timeout=15)
    except Exception:
        pass


def _disable(client):
    client.post(f"{API}/bot/close-all", timeout=15)
    client.post(f"{API}/bot/config", json={"enabled": False, "dryRun": True}, timeout=15)


# ---------- CORE FIX: signals set alertsFeeding even while bot disabled ----------
def test_alerts_feeding_when_bot_disabled(client):
    _disable(client)
    d0 = client.get(f"{API}/bot/status", timeout=15).json()
    r = client.post(f"{API}/bot/signal", json={
        "events": [{"symbol": "BTCUSDT", "side": "buy", "price": 63000, "volume": HIGH_VOL}]
    }, timeout=15)
    assert r.status_code == 200
    d = client.get(f"{API}/bot/status", timeout=15).json()
    assert d["alertsFeeding"] is True, "alertsFeeding should flip True on signal even when bot disabled"
    assert d["openPositions"] == [], "should not open positions while disabled"


# ---------- volume gate blocks trades when signal volume < minVolumeUsd ----------
def test_volume_gate_blocks(client):
    _disable(client)
    client.post(f"{API}/bot/config", json={
        "enabled": True, "dryRun": True, "streak": 2, "cooldownSec": 0,
        "minVolumeUsd": 900_000_000_000.0,  # absurdly high
    }, timeout=15)
    for _ in range(3):
        client.post(f"{API}/bot/signal", json={
            "events": [{"symbol": "BTCUSDT", "side": "buy", "price": 63000, "volume": 100_000_000.0}]
        }, timeout=15)
        time.sleep(0.2)
    d = client.get(f"{API}/bot/status", timeout=15).json()
    syms = [p["symbol"] for p in d["openPositions"]]
    assert "BTCUSDT" not in syms, "volume gate failed to block low-volume signal"
    # feed still shows as feeding
    assert d["alertsFeeding"] is True


# ---------- status shape ----------
def test_status_shape_new_fields(client):
    _disable(client)
    r = client.get(f"{API}/bot/status", timeout=15)
    assert r.status_code == 200
    d = r.json()
    for k in ("config", "stopped", "dailyPnl", "openPositions", "journal",
              "keysConfigured", "alertsFeeding", "lastSignalAgo", "active"):
        assert k in d, f"missing top-level key: {k}"
    assert d["keysConfigured"] in (True, False)  # binance keys optional
    # no volThresholdUsd / volWindow anymore
    c = d["config"]
    assert "volThresholdUsd" not in c
    assert "volWindow" not in c
    for k in CFG_KEYS:
        assert k in c, f"missing config key: {k}"
    # inactive when disabled
    assert d["active"] is False


# ---------- config clamps ----------
def test_config_clamps(client):
    _disable(client)
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


# ---------- disabled bot ignores signals ----------
def test_disabled_bot_ignores_signals(client):
    _disable(client)
    for _ in range(2):
        r = client.post(f"{API}/bot/signal", json={
            "events": [{"symbol": "BTCUSDT", "side": "buy", "price": 63000, "volume": HIGH_VOL}]
        }, timeout=15)
        assert r.status_code == 200
    d = client.get(f"{API}/bot/status", timeout=15).json()
    assert d["openPositions"] == [], "position opened while bot disabled!"


# ---------- enabled bot: 2 consecutive buys => open ----------
def test_two_buy_signals_open_position(client):
    _disable(client)
    r = client.post(f"{API}/bot/config", json={
        "enabled": True, "dryRun": True, "streak": 2, "cooldownSec": 5,
        "tpPct": 2.0, "slPct": 1.5, "maxPositionUsdt": 5,
        "maxOpenPositions": 3, "dailyLossLimit": 5, "minVolumeUsd": 1_000_000,
    }, timeout=15)
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg["enabled"] and cfg["dryRun"] and cfg["streak"] == 2

    sym = "BTCUSDT"
    price = 63000.0
    # signal #1
    r1 = client.post(f"{API}/bot/signal", json={
        "events": [{"symbol": sym, "side": "buy", "price": price, "volume": HIGH_VOL}]
    }, timeout=15)
    assert r1.status_code == 200
    # signal #2 within ~1s
    time.sleep(0.3)
    r2 = client.post(f"{API}/bot/signal", json={
        "events": [{"symbol": sym, "side": "buy", "price": price, "volume": HIGH_VOL}]
    }, timeout=15)
    assert r2.status_code == 200

    d = client.get(f"{API}/bot/status", timeout=15).json()
    positions = d["openPositions"]
    assert len(positions) == 1, f"expected 1 open position, got {len(positions)}"
    p = positions[0]
    assert p["symbol"] == sym
    assert p["mode"] == "SIM"
    assert abs(p["tpPrice"] - p["entryPrice"] * 1.02) / p["entryPrice"] < 1e-6
    assert abs(p["slPrice"] - p["entryPrice"] * 0.985) / p["entryPrice"] < 1e-6
    # alertsFeeding should now be True (lastSignalTs within 6s)
    assert d["alertsFeeding"] is True
    assert d["active"] is True


# ---------- single signal on different symbol does NOT open ----------
def test_single_signal_does_not_open(client):
    # bot still enabled from previous test
    client.get(f"{API}/bot/status", timeout=15)
    r = client.post(f"{API}/bot/signal", json={
        "events": [{"symbol": "ETHUSDT", "side": "buy", "price": 3200, "volume": HIGH_VOL}]
    }, timeout=15)
    assert r.status_code == 200
    d = client.get(f"{API}/bot/status", timeout=15).json()
    syms = [p["symbol"] for p in d["openPositions"]]
    assert "ETHUSDT" not in syms, "opened a position on single signal!"


# ---------- sell signal closes open position ----------
def test_sell_signal_closes_position(client):
    # from earlier test, BTCUSDT is open. Fire two sell signals -> streak triggers close
    sym = "BTCUSDT"
    price = 63500.0
    client.post(f"{API}/bot/signal", json={"events": [{"symbol": sym, "side": "sell", "price": price, "volume": HIGH_VOL}]}, timeout=15)
    time.sleep(0.2)
    client.post(f"{API}/bot/signal", json={"events": [{"symbol": sym, "side": "sell", "price": price, "volume": HIGH_VOL}]}, timeout=15)
    d = client.get(f"{API}/bot/status", timeout=15).json()
    syms = [p["symbol"] for p in d["openPositions"]]
    assert sym not in syms, "sell-signal did not close position"
    # journal should contain an 'exit' with reason 'sell-signal'
    exit_events = [e for e in d["journal"] if e.get("kind") == "exit" and e.get("symbol") == sym]
    assert exit_events, "no exit journal entry for BTCUSDT"
    assert exit_events[0]["reason"] == "sell-signal"


# ---------- close-all + reset-daily ----------
def test_close_all_and_reset_daily(client):
    # open another position first
    client.post(f"{API}/bot/config", json={"enabled": True, "dryRun": True, "streak": 2, "cooldownSec": 0, "minVolumeUsd": 1_000_000}, timeout=15)
    sym = "SOLUSDT"; px = 150.0
    client.post(f"{API}/bot/signal", json={"events": [{"symbol": sym, "side": "buy", "price": px, "volume": HIGH_VOL}]}, timeout=15)
    time.sleep(0.2)
    client.post(f"{API}/bot/signal", json={"events": [{"symbol": sym, "side": "buy", "price": px, "volume": HIGH_VOL}]}, timeout=15)
    r = client.post(f"{API}/bot/close-all", timeout=15)
    assert r.status_code == 200
    assert r.json()["openPositions"] == []
    r = client.post(f"{API}/bot/reset-daily", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["dailyPnl"] == 0.0
    assert d["stopped"] is False


# ---------- config persists across reads ----------
def test_config_persists(client):
    _disable(client)
    client.post(f"{API}/bot/config", json={"streak": 3, "cooldownSec": 7, "tpPct": 3.0}, timeout=15)
    d = client.get(f"{API}/bot/status", timeout=15).json()
    assert d["config"]["streak"] == 3
    assert d["config"]["cooldownSec"] == 7
    assert abs(d["config"]["tpPct"] - 3.0) < 1e-6
    # second read
    d2 = client.get(f"{API}/bot/status", timeout=15).json()
    assert d2["config"]["streak"] == 3


# ---------- cleanup ----------
def test_cleanup(client):
    client.post(f"{API}/bot/close-all", timeout=15)
    r = client.post(f"{API}/bot/config", json={"enabled": False, "dryRun": True, "streak": 2, "cooldownSec": 30}, timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["config"]["enabled"] is False
    assert d["config"]["dryRun"] is True
    assert d["openPositions"] == []
