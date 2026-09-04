"""Tests for Hyperliquid diagnose endpoint and graceful live-order error handling."""
import os
import time
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback to frontend .env
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")

EXPECTED_MAIN = "0x811702818AA45e40dE2317b4E044E7E8cD911856".lower()


# -------- HL diagnose --------
def test_hl_diagnose():
    r = requests.get(f"{BASE_URL}/api/hyperliquid/diagnose", timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    print("diagnose:", d)
    assert str(d.get("network", "")).lower() == "mainnet"
    assert str(d.get("mainWallet", "")).lower() == EXPECTED_MAIN
    assert str(d.get("apiWalletDerivedFromSecret", "")).lower() == EXPECTED_MAIN
    assert d.get("mainEqualsApiWallet") is True
    assert isinstance(d.get("mainWallet_accountValue"), (int, float))
    assert d.get("mainWallet_accountValue") == 0 or d.get("mainWallet_accountValue") == 0.0
    assert "apiWallet_accountValue" in d
    assert isinstance(d.get("hint"), str) and len(d["hint"]) > 0


# -------- HL account --------
def test_hl_account():
    r = requests.get(f"{BASE_URL}/api/hyperliquid/account", timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    print("account:", d)
    assert str(d.get("network", "")).lower() == "mainnet"
    assert float(d.get("accountValue", -1)) == 0.0
    assert d.get("keyConfigured") is True


# -------- Regression: /api/bot/status & /api/tokens --------
def test_bot_status_has_hl_fields():
    r = requests.get(f"{BASE_URL}/api/bot/status", timeout=30)
    assert r.status_code == 200
    d = r.json()
    cfg = d.get("config", d)
    assert "hlLeverage" in cfg
    assert "hlCrossMargin" in cfg


def test_tokens():
    r = requests.get(f"{BASE_URL}/api/tokens", timeout=30)
    assert r.status_code == 200
    data = r.json()
    tokens = data.get("tokens", data) if isinstance(data, dict) else data
    assert isinstance(tokens, list) and len(tokens) > 0


# -------- Graceful LIVE error handling --------
def test_live_hl_signal_fails_gracefully():
    # 1) get current BTC price from /api/tokens
    tdata = requests.get(f"{BASE_URL}/api/tokens", timeout=30).json()
    tokens = tdata.get("tokens", tdata) if isinstance(tdata, dict) else tdata
    btc = None
    for t in tokens:
        sym = t.get("symbol") or t.get("ticker") or ""
        if sym.upper().startswith("BTC"):
            btc = t
            break
    assert btc is not None, "BTC token not found"
    price = btc.get("price") or btc.get("lastPrice") or btc.get("close")
    assert price, f"no price in token: {btc}"
    price = float(price)
    print(f"BTC price: {price}")

    # 2) Enable LIVE HL config (also disable maxVolumeUsd upper bound so BTC's huge vol passes)
    live_cfg = {
        "enabled": True, "dryRun": False, "exchange": "hyperliquid",
        "hlTestnet": False, "straddleEnabled": False, "streak": 1,
        "minVolumeUsd": 0, "maxVolumeUsd": 0, "cooldownSec": 0,
        "maxOpenPositions": 5, "autoExit": False, "maxLossPerTradeUsdt": 0,
        "hlLeverage": 1, "hlCrossMargin": True,
    }
    r = requests.post(f"{BASE_URL}/api/bot/config", json=live_cfg, timeout=30)
    assert r.status_code == 200, r.text

    # 3) Send signal
    sig = {"events": [{"symbol": "BTCUSDT", "side": "buy", "price": price, "volume": 0}]}
    r = requests.post(f"{BASE_URL}/api/bot/signal", json=sig, timeout=30)
    assert r.status_code == 200, r.text
    print("signal resp:", r.json())

    # 4) Wait for bot loop
    time.sleep(4)

    # 5) Check journal for error entry
    r = requests.get(f"{BASE_URL}/api/bot/status", timeout=30)
    assert r.status_code == 200
    data = r.json()
    journal = data.get("journal", [])
    print(f"journal entries: {len(journal)}")
    errs = [j for j in journal if (j.get("type") == "error" or j.get("level") == "error"
                                   or "error" in str(j).lower())]
    # Look for hyperliquid venue + error
    hl_errs = [j for j in journal if "hyperliquid" in str(j).lower() and
               ("error" in str(j).lower() or "wallet" in str(j).lower())]
    print("hl error entries:", hl_errs[-3:] if hl_errs else "NONE")
    assert len(hl_errs) > 0, f"No graceful HL error in journal. Last journal: {journal[-5:]}"

    # 6) Bot still responsive
    r2 = requests.get(f"{BASE_URL}/api/bot/status", timeout=30)
    assert r2.status_code == 200

    # 7) No positions opened
    positions = data.get("positions", [])
    assert len([p for p in positions if p.get("venue") == "hyperliquid"]) == 0


# -------- Cleanup: restore SIM defaults --------
def test_zzz_restore_sim_defaults():
    requests.post(f"{BASE_URL}/api/bot/close-all", timeout=30)
    safe_cfg = {
        "enabled": False, "dryRun": True, "straddleEnabled": False,
        "streak": 2, "minVolumeUsd": 5000000, "cooldownSec": 30,
        "maxOpenPositions": 3, "autoExit": True,
        "maxLossPerTradeUsdt": 0.0002, "hlLeverage": 1, "hlCrossMargin": True,
    }
    r = requests.post(f"{BASE_URL}/api/bot/config", json=safe_cfg, timeout=30)
    assert r.status_code == 200
