"""Tests for straddle TP/SL always-on behavior (autoExit-agnostic) and regression."""
import os
import time
import requests
import pytest

BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://token-scan-analytics.preview.emergentagent.com").rstrip("/")
API = f"{BASE}/api"


def _post_cfg(cfg):
    r = requests.post(f"{API}/bot/config", json=cfg, timeout=15)
    assert r.status_code == 200, f"config post failed: {r.status_code} {r.text}"
    return r.json()


def _status():
    r = requests.get(f"{API}/bot/status", timeout=15)
    assert r.status_code == 200
    return r.json()


def _pick_volatile_symbol():
    r = requests.get(f"{API}/tokens", timeout=20)
    assert r.status_code == 200
    tokens = r.json().get("tokens") or r.json()
    # prefer DOGEUSDT / XRPUSDT if present, else first token
    prefer = ["DOGEUSDT", "XRPUSDT", "SHIBUSDT", "PEPEUSDT"]
    by_sym = {t.get("symbol"): t for t in tokens if isinstance(t, dict)}
    for s in prefer:
        if s in by_sym and by_sym[s].get("price"):
            return s, float(by_sym[s]["price"])
    # fallback
    for t in tokens:
        if isinstance(t, dict) and t.get("symbol") and t.get("price"):
            return t["symbol"], float(t["price"])
    pytest.skip("no usable tokens")


def _send_signal(symbol, side, price):
    r = requests.post(f"{API}/bot/signal", json={
        "events": [{"symbol": symbol, "side": side, "price": price, "volume": 0}]
    }, timeout=15)
    assert r.status_code == 200, f"signal failed: {r.status_code} {r.text}"
    return r.json()


def _close_all():
    try:
        requests.post(f"{API}/bot/close-all", timeout=15)
    except Exception:
        pass


# ---- Config regression ----
def test_status_contains_hl_native_tpsl_and_flags():
    st = _status()
    cfg = st.get("config", {})
    assert "hlNativeTpsl" in cfg
    assert "hlLeverage" in cfg
    assert "hlCrossMargin" in cfg
    assert isinstance(cfg["hlNativeTpsl"], bool)


def test_tokens_endpoint_ok():
    r = requests.get(f"{API}/tokens", timeout=20)
    assert r.status_code == 200
    body = r.json()
    toks = body.get("tokens") if isinstance(body, dict) else body
    assert isinstance(toks, list) and len(toks) > 0


def test_hyperliquid_diagnose_mainnet():
    r = requests.get(f"{API}/hyperliquid/diagnose", timeout=20)
    assert r.status_code == 200
    d = r.json()
    # network should indicate mainnet
    txt = str(d).lower()
    assert "mainnet" in txt or d.get("testnet") is False


# ---- Primary: straddle TP/SL closes with autoExit OFF ----
def test_straddle_tpsl_closes_with_autoexit_off():
    _close_all()
    symbol, price = _pick_volatile_symbol()
    print(f"Using {symbol} @ {price}")
    _post_cfg({
        "enabled": True, "dryRun": True, "exchange": "hyperliquid",
        "straddleEnabled": True,
        "straddleEntryPct": 0.000001,
        "straddleTpPct": 0.02, "straddleSlPct": 0.02,
        "minVolumeUsd": 0, "maxVolumeUsd": 0,
        "cooldownSec": 0, "maxOpenPositions": 10,
        "autoExit": False, "maxLossPerTradeUsdt": 0,
    })
    _send_signal(symbol, "buy", price)

    saw_armed = saw_fill = saw_entry = False
    exit_reason = None
    exit_side = None
    deadline = time.time() + 60
    while time.time() < deadline:
        st = _status()
        journal = st.get("journal", [])
        kinds = [(j.get("kind"), j.get("symbol"), j.get("side"), j.get("reason"), j.get("source")) for j in journal]
        for k, sy, side, reason, src in kinds:
            if sy != symbol:
                continue
            if k == "straddle":
                saw_armed = True
            elif k == "straddle_fill":
                saw_fill = True
            elif k == "entry" and src == "straddle":
                saw_entry = True
            elif k == "exit" and reason in ("take-profit", "stop-loss"):
                exit_reason = reason
                exit_side = side
                break
        if exit_reason:
            break
        time.sleep(4)

    print(f"armed={saw_armed} fill={saw_fill} entry={saw_entry} exit_reason={exit_reason} exit_side={exit_side}")
    assert saw_armed, "straddle was not armed"
    # Fill+entry may not appear if market didn't move enough to trigger straddle, retry note
    if not saw_fill:
        pytest.skip(f"straddle did not fill within timeout for {symbol}; market too calm")
    assert saw_entry, "entry(source=straddle) missing"
    assert exit_reason in ("take-profit", "stop-loss"), f"straddle position did NOT close via TP/SL with autoExit OFF (exit_reason={exit_reason})"
    assert exit_side in ("sell", "cover"), f"unexpected exit side: {exit_side}"


# ---- Regression: normal (non-straddle) position obeys autoExit ----
def test_normal_position_respects_autoexit_toggle():
    _close_all()
    symbol, price = _pick_volatile_symbol()
    _post_cfg({
        "enabled": True, "dryRun": True, "exchange": "hyperliquid",
        "straddleEnabled": False, "streak": 1,
        "minVolumeUsd": 0, "maxVolumeUsd": 0,
        "cooldownSec": 0, "autoExit": False,
        "maxLossPerTradeUsdt": 0,
        "tpPct": 0.02, "slPct": 0.02,
        "maxOpenPositions": 10,
    })
    # streak=1 needs 1 buy signal
    start_ts = time.time()
    _send_signal(symbol, "buy", price)

    # wait ~15s and confirm no auto-close via TP/SL for source!=straddle
    t_end = time.time() + 18
    early_exit = None
    saw_entry = False
    while time.time() < t_end:
        st = _status()
        for j in st.get("journal", []):
            if j.get("symbol") != symbol or (j.get("ts") or 0) < start_ts:
                continue
            if j.get("kind") == "entry" and j.get("source") != "straddle":
                saw_entry = True
            if j.get("kind") == "exit" and j.get("reason") in ("take-profit", "stop-loss"):
                # only fail if this exit corresponds to a non-straddle position — reason is set for both
                # heuristic: since straddleEnabled=false, any exit is a regression
                early_exit = j.get("reason")
        if early_exit:
            break
        time.sleep(3)

    if not saw_entry:
        pytest.skip("normal entry did not occur (streak/route)")
    assert early_exit is None, f"non-straddle position auto-closed via {early_exit} with autoExit OFF (regression!)"

    # Now flip autoExit=true and confirm it closes
    _post_cfg({"autoExit": True})
    flip_ts = time.time()
    t_end = time.time() + 40
    later_exit = None
    while time.time() < t_end:
        st = _status()
        for j in st.get("journal", []):
            if j.get("symbol") == symbol and (j.get("ts") or 0) >= flip_ts and j.get("kind") == "exit" and j.get("reason") in ("take-profit", "stop-loss"):
                later_exit = j.get("reason")
                break
        if later_exit:
            break
        time.sleep(3)
    if later_exit is None:
        pytest.skip("market did not move 0.02% within window to test autoExit=on close")
    assert later_exit in ("take-profit", "stop-loss")


# ---- Final: restore safe defaults & confirm no crash ----
def test_z_final_cleanup_and_no_backend_crashes():
    _close_all()
    _post_cfg({
        "enabled": False, "dryRun": True,
        "straddleEnabled": False,
        "straddleEntryPct": 0.5, "straddleTpPct": 1.0, "straddleSlPct": 1.0,
        "streak": 2, "minVolumeUsd": 5000000, "maxVolumeUsd": 0,
        "cooldownSec": 30, "maxOpenPositions": 3,
        "autoExit": True, "maxLossPerTradeUsdt": 0.0002,
    })
    # status still 200
    st = _status()
    assert st.get("config", {}).get("enabled") is False
