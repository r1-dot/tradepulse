"""Trade-amount band filter — the bot only accepts signals whose calculated trade
amount (tradeVol, from the alert log) is inside [tradeAmtMin, tradeAmtMax].
SIM/dryRun only, never LIVE. Drives the live backend over HTTP like test_bot.py."""
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

SYM = "BTCUSDT"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    yield s
    try:
        s.post(f"{API}/bot/close-all", timeout=15)
        s.post(f"{API}/bot/config", json={"enabled": False, "dryRun": True,
                                           "straddleEnabled": False}, timeout=15)
    except Exception:
        pass


def _reset(client, **extra):
    client.post(f"{API}/bot/close-all", timeout=15)
    body = {
        "enabled": True, "dryRun": True, "streak": 2, "cooldownSec": 0,
        "minVolumeUsd": 0.0, "maxVolumeUsd": 0.0, "maxOpenPositions": 5,
        "straddleEnabled": False,
        "tradeAmtEnabled": True, "tradeAmtMin": 1000.0, "tradeAmtMax": 100_000_000.0,
    }
    body.update(extra)
    client.post(f"{API}/bot/config", json=body, timeout=15)


def _fire(client, trade_vol, n=4):
    for _ in range(n):
        client.post(f"{API}/bot/signal", json={
            "events": [{"symbol": SYM, "side": "buy", "price": 63000,
                        "volume": 10_000_000_000.0, "tradeVol": trade_vol}]
        }, timeout=15)
        time.sleep(0.2)
    return client.get(f"{API}/bot/status", timeout=15).json()


def _open_syms(d):
    return [p["symbol"] for p in d["openPositions"]]


def test_defaults_present(client):
    c = client.get(f"{API}/bot/status", timeout=15).json()["config"]
    assert c["tradeAmtEnabled"] in (True, False)
    assert "tradeAmtMin" in c and "tradeAmtMax" in c


def test_clamped_to_1k_100m(client):
    c = client.post(f"{API}/bot/config", json={
        "tradeAmtMin": 1, "tradeAmtMax": 999_000_000}, timeout=15).json()["config"]
    assert c["tradeAmtMin"] == 1000.0
    assert c["tradeAmtMax"] == 100_000_000.0


def test_below_min_rejected(client):
    _reset(client)
    d = _fire(client, 10)  # 10 < 1000 min
    assert SYM not in _open_syms(d), "below-min trade amount must be rejected"


def test_in_band_accepted(client):
    _reset(client)
    d = _fire(client, 50_000)  # inside 1k-100m
    assert SYM in _open_syms(d), "in-band trade amount should open a SIM position"


def test_above_max_rejected(client):
    _reset(client)
    d = _fire(client, 200_000_000)  # > 100m max
    assert SYM not in _open_syms(d), "above-max trade amount must be rejected"


def test_toggle_off_bypasses_filter(client):
    _reset(client, tradeAmtEnabled=False)
    d = _fire(client, 10)  # tiny amount, but filter is OFF
    assert SYM in _open_syms(d), "filter OFF should let any amount through"


def test_cleanup(client):
    client.post(f"{API}/bot/close-all", timeout=15)
    client.post(f"{API}/bot/config", json={"enabled": False, "dryRun": True,
                                            "straddleEnabled": False}, timeout=15)
