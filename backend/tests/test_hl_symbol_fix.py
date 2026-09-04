"""Tests for Hyperliquid symbol-format fixes (resolve endpoint, KeyError fix, base-coin error logging)."""
import os
import time
import re
import requests
import pytest

def _load_backend_url():
    url = os.environ.get("REACT_APP_BACKEND_URL")
    if not url:
        try:
            with open("/app/frontend/.env") as f:
                for line in f:
                    if line.startswith("REACT_APP_BACKEND_URL="):
                        url = line.split("=", 1)[1].strip()
                        break
        except Exception:
            pass
    return (url or "").rstrip("/")


BASE_URL = _load_backend_url()
API = f"{BASE_URL}/api"


# --- Resolve endpoint ------------------------------------------------------
def test_hyperliquid_resolve_strips_usdt():
    syms = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,LINKUSDT,ARBUSDT,CRVUSDT,UNIUSDT,1000PEPEUSDT,ARKMUSDT,ENSOUSDT"
    r = requests.get(f"{API}/hyperliquid/resolve", params={"symbols": syms}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "resolved" in body and "coinsLoaded" in body
    resolved = body["resolved"]
    expected = {
        "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL", "BNBUSDT": "BNB",
        "XRPUSDT": "XRP", "LINKUSDT": "LINK", "ARBUSDT": "ARB", "CRVUSDT": "CRV",
        "UNIUSDT": "UNI", "1000PEPEUSDT": "KPEPE",
        "ARKMUSDT": None, "ENSOUSDT": None,
    }
    for k, v in expected.items():
        assert resolved.get(k) == v, f"{k} => {resolved.get(k)} (expected {v})"
    # ~233 coins loaded
    assert body["coinsLoaded"] > 150, f"coinsLoaded={body['coinsLoaded']}"


# --- Regression ------------------------------------------------------------
def test_diagnose_still_ok():
    r = requests.get(f"{API}/hyperliquid/diagnose", timeout=15)
    assert r.status_code == 200
    b = r.json()
    assert b.get("net") == "mainnet" or b.get("network") == "mainnet" or "mainnet" in str(b).lower()
    # main equals api wallet
    assert b.get("mainEqualsApiWallet") is True, b


def test_bot_status_returns_config():
    r = requests.get(f"{API}/bot/status", timeout=10)
    assert r.status_code == 200
    b = r.json()
    assert "config" in b
    assert "journal" in b


def test_tokens_endpoint():
    r = requests.get(f"{API}/tokens", timeout=15)
    assert r.status_code == 200
    b = r.json()
    # tolerate list or dict-with-tokens
    if isinstance(b, dict):
        toks = b.get("tokens") or b.get("data") or []
    else:
        toks = b
    assert len(toks) > 0


# --- KeyError fix ----------------------------------------------------------
def test_no_new_keyerror_in_bot_loop():
    log_path = "/var/log/supervisor/backend.err.log"
    if not os.path.exists(log_path):
        pytest.skip("no backend err log")
    start_size = os.path.getsize(log_path)
    # confirm bot is up before waiting
    r0 = requests.get(f"{API}/bot/status", timeout=10)
    assert r0.status_code == 200
    time.sleep(20)
    # bot must still be responsive after the wait (loop hasn't died)
    r1 = requests.get(f"{API}/bot/status", timeout=10)
    assert r1.status_code == 200, "bot API not responsive after wait"
    # read appended log
    with open(log_path, "rb") as f:
        f.seek(start_size)
        new = f.read().decode("utf-8", errors="ignore")
    # Search for new KeyError with Binance-style symbol
    bad = re.findall(r"bot error.*KeyError.*['\"][A-Z0-9]+USDT['\"]", new)
    assert not bad, f"new KeyError with Binance symbol found: {bad[:3]}"
    # More general: any KeyError in new window is suspect
    key_errs = re.findall(r"KeyError:.*", new)
    # allow non-symbol keyerrors? Report them
    assert not key_errs, f"new KeyError lines: {key_errs[:3]}"


# --- Live HL error journal uses base coin ---------------------------------
def _get_current_price(symbol="CRVUSDT"):
    try:
        r = requests.get(f"{API}/tokens", timeout=15).json()
        toks = r.get("tokens") if isinstance(r, dict) else r
        for t in toks or []:
            if (t.get("symbol") or "").upper() == symbol.upper():
                return float(t.get("price") or t.get("lastPrice") or 0) or None
    except Exception:
        return None
    return None


def test_live_hl_error_uses_base_coin():
    # Snapshot original config
    st0 = requests.get(f"{API}/bot/status", timeout=10).json()
    orig = dict(st0.get("config", {}))

    # Relax filters and disable straddle so a manual signal opens a real position (which will fail on unfunded HL)
    relax = {"maxVolumeUsd": 0, "minVolumeUsd": 0, "cooldownSec": 0, "streak": 1, "straddleEnabled": False}
    r = requests.post(f"{API}/bot/config", json=relax, timeout=10)
    assert r.status_code == 200, r.text

    try:
        price = _get_current_price("CRVUSDT") or 0.5
        payload = {"events": [{"symbol": "CRVUSDT", "side": "buy", "price": price, "volume": 0}]}
        r = requests.post(f"{API}/bot/signal", json=payload, timeout=15)
        assert r.status_code in (200, 201, 202), r.text

        found = None
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(2)
            st = requests.get(f"{API}/bot/status", timeout=10).json()
            journal = st.get("journal") or []
            for e in reversed(journal):
                kind = (e.get("kind") or e.get("type") or "").lower()
                if kind != "error":
                    continue
                if (e.get("venue") or "").lower() != "hyperliquid":
                    continue
                msg = e.get("message") or ""
                if "HL sent coin" in msg:
                    found = e
                    break
            if found:
                break

        assert found is not None, f"no HL live error entry produced. Last journal: {(st.get('journal') or [])[-5:]}"
        assert "HL sent coin 'CRV'" in found.get("message", ""), found
        assert found.get("symbol") == "CRV", found
    finally:
        # Restore sensible values (also re-enable straddle if user had it on)
        restore = {"streak": 2, "minVolumeUsd": 5000000, "maxVolumeUsd": 0, "cooldownSec": 30}
        if orig.get("straddleEnabled"):
            restore["straddleEnabled"] = True
        requests.post(f"{API}/bot/config", json=restore, timeout=10)
