"""Tests for delta token stream + hard max-loss-cap + market overview enrichment."""
import os
import time
import uuid
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

HIGH_VOL = 10_000_000_000.0


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    yield s
    # Safety cleanup
    try:
        s.post(f"{API}/bot/close-all", timeout=15)
        s.post(f"{API}/bot/config", json={
            "enabled": False, "dryRun": True, "autoExit": True,
            "streak": 2, "minVolumeUsd": 5_000_000,
            "maxLossPerTradeUsdt": 0.0002,
        }, timeout=15)
    except Exception:
        pass


# ---------- Delta endpoint ----------
class TestDelta:
    def test_first_call_full(self, client):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        r = client.get(f"{API}/tokens/delta", params={
            "sid": sid, "timeframe": "1s", "quote": "USDT", "limit": 1000
        }, timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["full"] is True
        assert "order" in d and isinstance(d["order"], list)
        assert "rows" in d and isinstance(d["rows"], dict)
        assert len(d["order"]) == len(d["rows"])
        assert d["totalPairs"] > 0
        assert "totalTrades" in d
        # Row shape: [price, change, quoteVol, trades, sideNum, trades24h]
        first_sym = d["order"][0]
        row = d["rows"][first_sym]
        assert isinstance(row, list) and len(row) == 6
        assert row[4] in (0, 1), "sideNum must be 0 or 1"

    def test_second_call_partial(self, client):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        # First call establishes session
        r1 = client.get(f"{API}/tokens/delta", params={
            "sid": sid, "timeframe": "1s", "quote": "USDT", "limit": 1000
        }, timeout=15)
        assert r1.json()["full"] is True
        # Wait for state to shift a bit
        time.sleep(1.2)
        r2 = client.get(f"{API}/tokens/delta", params={
            "sid": sid, "timeframe": "1s", "quote": "USDT", "limit": 1000
        }, timeout=15)
        d2 = r2.json()
        assert d2["full"] is False
        assert "changed" in d2 and isinstance(d2["changed"], dict)
        assert "removed" in d2 and isinstance(d2["removed"], list)
        # Partial should generally not include EVERY row
        assert len(d2["changed"]) <= len(r1.json()["rows"])

    def test_param_change_forces_full(self, client):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        client.get(f"{API}/tokens/delta", params={
            "sid": sid, "timeframe": "1s", "quote": "USDT", "limit": 1000
        }, timeout=15)
        # Same sid, different timeframe -> signature changes -> full again
        r = client.get(f"{API}/tokens/delta", params={
            "sid": sid, "timeframe": "1m", "quote": "USDT", "limit": 1000
        }, timeout=15)
        d = r.json()
        assert d["full"] is True, "changing timeframe should reset session and return full"
        assert d["timeframe"] == "1m"

    def test_backwards_compat_tokens(self, client):
        r = client.get(f"{API}/tokens", params={"timeframe": "1s", "quote": "USDT"}, timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert "tokens" in d and isinstance(d["tokens"], list)
        assert "total" in d
        assert "totalPairs" in d
        assert "totalTrades" in d
        t = d["tokens"][0]
        for f in ("symbol", "price", "change", "quoteVol", "trades", "trades24h", "side"):
            assert f in t


# ---------- Market overview enrichment ----------
class TestMarketOverviewEnriched:
    def test_eth_gas_and_btc_block(self, client):
        r = client.get(f"{API}/market-overview", timeout=25)
        assert r.status_code == 200
        d = r.json()
        m = d["metrics"]
        s = d["sources"]
        # New enriched fields
        assert m.get("ethGasGwei") is not None, f"ethGasGwei missing; sources={s}"
        assert m.get("btcBlockHeight") is not None, f"btcBlockHeight missing; sources={s}"
        assert isinstance(m["btcBlockHeight"], (int, float)) and m["btcBlockHeight"] > 500_000
        # Sources status
        assert s.get("etherscan") == "ok", f"etherscan not ok: {s.get('etherscan')}"
        assert s.get("blockchain.com") == "ok", f"blockchain.com not ok: {s.get('blockchain.com')}"


# ---------- Hard max-loss-cap force-close ----------
class TestMaxLossCap:
    def test_config_accepts_max_loss(self, client):
        r = client.post(f"{API}/bot/config", json={
            "enabled": False, "dryRun": True, "maxLossPerTradeUsdt": 0.0002,
        }, timeout=15)
        assert r.status_code == 200
        c = r.json()["config"]
        assert abs(c["maxLossPerTradeUsdt"] - 0.0002) < 1e-9
        # And it's returned in /bot/status
        d = client.get(f"{API}/bot/status", timeout=15).json()
        assert abs(d["config"]["maxLossPerTradeUsdt"] - 0.0002) < 1e-9

    def test_hard_cap_force_closes(self, client):
        # Reset
        client.post(f"{API}/bot/close-all", timeout=15)
        # Configure: autoExit OFF, tiny max-loss cap, small position
        r = client.post(f"{API}/bot/config", json={
            "enabled": True, "dryRun": True, "streak": 1,
            "minVolumeUsd": 0, "maxVolumeUsd": 0,
            "cooldownSec": 0, "maxPositionUsdt": 5,
            "maxLossPerTradeUsdt": 0.0002, "autoExit": False,
        }, timeout=15)
        assert r.status_code == 200
        cfg = r.json()["config"]
        assert cfg["enabled"] and cfg["autoExit"] is False
        assert cfg["streak"] == 2 or cfg["streak"] >= 1  # server may clamp to >=2

        sym = "BTCUSDT"
        # Fire exactly 2 same-side signals within 1.5s to satisfy streak=2 exactly.
        for _ in range(2):
            client.post(f"{API}/bot/signal", json={
                "events": [{"symbol": sym, "side": "buy", "price": 63000.0, "volume": HIGH_VOL}]
            }, timeout=15)
            time.sleep(0.3)

        # Poll status up to 45s for the position to appear and then force-close
        deadline = time.time() + 45
        saw_open = False
        exit_reason = None
        while time.time() < deadline:
            d = client.get(f"{API}/bot/status", timeout=15).json()
            positions = d["openPositions"]
            if any(p["symbol"] == sym for p in positions):
                saw_open = True
            # Look for max-loss-cap exit in journal
            for e in d.get("journal", []):
                if e.get("kind") == "exit" and e.get("symbol") == sym and e.get("reason") == "max-loss-cap":
                    exit_reason = "max-loss-cap"
                    break
            if exit_reason:
                break
            time.sleep(1.0)

        assert saw_open, "position never opened for streak-triggered buy"
        assert exit_reason == "max-loss-cap", (
            f"expected hard-cap force-close (autoExit=False), got journal without max-loss-cap; "
            f"last status keys: journal has {len(d.get('journal',[]))} entries"
        )

    def test_restore_safe_defaults(self, client):
        client.post(f"{API}/bot/close-all", timeout=15)
        r = client.post(f"{API}/bot/config", json={
            "enabled": False, "dryRun": True, "autoExit": True,
            "streak": 2, "minVolumeUsd": 5_000_000,
        }, timeout=15)
        assert r.status_code == 200
        c = r.json()["config"]
        assert c["enabled"] is False and c["dryRun"] is True
        assert c["autoExit"] is True
        assert c["streak"] == 2
        assert c["minVolumeUsd"] == 5_000_000
