"""Binance Token Scanner - backend API tests."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', 'https://token-scan-analytics.preview.emergentagent.com').rstrip('/')
API = f"{BASE_URL}/api"

TIMEFRAMES = ["1s", "5s", "15s", "30s", "1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "4d", "7d", "10d"]


@pytest.fixture(scope="session", autouse=True)
def wait_for_engine_warmup():
    """Wait until the engine has polled Binance at least once."""
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            r = requests.get(f"{API}/engine/status", timeout=10)
            if r.status_code == 200 and r.json().get("pairsTracked", 0) > 100:
                return
        except Exception:
            pass
        time.sleep(1)


# ---------- engine status ----------
class TestEngineStatus:
    def test_engine_status_ok(self):
        r = requests.get(f"{API}/engine/status", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["connected"] is True
        assert 500 <= d["pairsTracked"] <= 900, f"pairsTracked={d['pairsTracked']}"
        assert d["source"]
        assert d["lastUpdate"] and d["lastUpdate"] > 0


# ---------- tokens endpoint ----------
class TestTokens:
    def test_default_1s(self):
        r = requests.get(f"{API}/tokens", params={"timeframe": "1s"}, timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert "tokens" in d and len(d["tokens"]) > 0
        assert "totalPairs" in d
        t0 = d["tokens"][0]
        for f in ("symbol", "base", "price", "change", "quoteVol", "trades", "trades24h"):
            assert f in t0
        # sorted trades desc
        trades_list = [t["trades"] for t in d["tokens"]]
        assert trades_list == sorted(trades_list, reverse=True)

    @pytest.mark.parametrize("tf", TIMEFRAMES)
    def test_all_timeframes(self, tf):
        r = requests.get(f"{API}/tokens", params={"timeframe": tf}, timeout=10)
        assert r.status_code == 200, f"{tf}: {r.text}"
        d = r.json()
        assert isinstance(d["tokens"], list) and len(d["tokens"]) > 0
        if tf == "1d":
            assert d["approx"] is False

    def test_bad_timeframe(self):
        r = requests.get(f"{API}/tokens", params={"timeframe": "BAD"}, timeout=10)
        assert r.status_code == 400

    def test_search_filter(self):
        r = requests.get(f"{API}/tokens", params={"timeframe": "1s", "search": "BTC"}, timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert len(d["tokens"]) > 0
        for t in d["tokens"]:
            assert "BTC" in t["symbol"]

    def test_sort_change_desc(self):
        r = requests.get(f"{API}/tokens", params={"timeframe": "1h", "sort": "change_desc"}, timeout=10)
        assert r.status_code == 200
        changes = [t["change"] for t in r.json()["tokens"]]
        assert changes == sorted(changes, reverse=True)


# ---------- token detail ----------
class TestTokenDetail:
    def test_btc_all_timeframes(self):
        r = requests.get(f"{API}/token/BTCUSDT", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["symbol"] == "BTCUSDT"
        assert set(d["timeframes"].keys()) == set(TIMEFRAMES)
        for tf, v in d["timeframes"].items():
            assert "trades" in v and isinstance(v["trades"], int)

    def test_unknown_symbol(self):
        r = requests.get(f"{API}/token/FAKEUSDT", timeout=10)
        assert r.status_code == 404


# ---------- market overview ----------
class TestMarketOverview:
    def test_market_overview(self):
        r = requests.get(f"{API}/market-overview", timeout=20)
        assert r.status_code == 200
        d = r.json()
        assert "metrics" in d and "sources" in d
        m = d["metrics"]
        # Coinbase prices
        assert m["btc"] and m["btc"] > 0
        assert m["eth"] and m["eth"] > 0
        # CoinGecko globals
        assert m["totalMarketCap"] and m["btcDominance"]
        # Blockchain.com
        assert m["btcTxCount24h"] is not None
        # Sources expected states
        assert d["sources"].get("etherscan") == "no api key"
        assert d["sources"].get("coinmarketcap") == "no api key"
