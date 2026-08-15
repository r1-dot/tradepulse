"""
Binance USDT Token Scanner — real-time trade-count engine.

Data source: data-api.binance.vision (public market data mirror, not geo-blocked).
Core idea: the 24hr ticker returns a monotonically increasing `lastId` per symbol
(= cumulative trade id). Differencing `lastId` across two points in time gives the
EXACT number of trades in that window. We keep rolling snapshot histories so trade
counts for 1s ... 10d can be computed exactly (long windows fill in as the engine runs;
until then they are scaled from the real 24h trade count).
"""
from fastapi import FastAPI, APIRouter, Query, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import asyncio
import time
import logging
from pathlib import Path
from collections import deque
from typing import Optional
import httpx

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

BINANCE_BASE = os.environ.get('BINANCE_BASE', 'https://data-api.binance.vision')
QUOTE = os.environ.get('BINANCE_QUOTE', 'USDT')
ETHERSCAN_API_KEY = os.environ.get('ETHERSCAN_API_KEY', '')
COINMARKETCAP_API_KEY = os.environ.get('COINMARKETCAP_API_KEY', '')
COINGECKO_API_KEY = os.environ.get('COINGECKO_API_KEY', '')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("scanner")

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ----------------------------- Timeframes -----------------------------
TIMEFRAMES = [
    ("1s", 1), ("5s", 5), ("15s", 15), ("30s", 30),
    ("1m", 60), ("5m", 300), ("15m", 900), ("30m", 1800),
    ("1h", 3600), ("2h", 7200), ("4h", 14400),
    ("1d", 86400), ("4d", 345600), ("7d", 604800), ("10d", 864000),
]
TF_SECONDS = {k: v for k, v in TIMEFRAMES}
DAY = 86400

# ----------------------------- In-memory engine state -----------------------------
STATE = {
    "latest": {},              # symbol -> {base, price, change, quoteVol, count24h, lastId}
    "fine": deque(maxlen=360),     # (ts, {symbol: lastId})  ~6 min @1s
    "coarse": deque(maxlen=3200),  # (ts, {symbol: lastId})  ~11 days @5min
    "last_coarse_ts": 0.0,
    "start_ts": time.time(),
    "last_update": 0.0,
    "connected": False,
    "poll_count": 0,
    "error": None,
}
COARSE_INTERVAL = 300  # seconds


async def poll_binance(http: httpx.AsyncClient):
    backoff = 1.0
    url = f"{BINANCE_BASE}/api/v3/ticker/24hr"
    while True:
        t0 = time.time()
        try:
            r = await http.get(url, timeout=15)
            if r.status_code in (418, 429):
                logger.warning("Binance rate limited (%s), backing off", r.status_code)
                await asyncio.sleep(min(backoff * 2, 30))
                backoff = min(backoff * 2, 30)
                continue
            r.raise_for_status()
            data = r.json()
            backoff = 1.0
            now = time.time()
            latest = {}
            snap = {}
            for x in data:
                sym = x["symbol"]
                if not sym.endswith(QUOTE):
                    continue
                try:
                    last_id = int(x["lastId"])
                except (KeyError, ValueError, TypeError):
                    continue
                if last_id < 0:
                    continue
                latest[sym] = {
                    "base": sym[: -len(QUOTE)],
                    "price": float(x["lastPrice"]),
                    "change": float(x["priceChangePercent"]),
                    "quoteVol": float(x["quoteVolume"]),
                    "count24h": int(x["count"]),
                    "lastId": last_id,
                }
                snap[sym] = (last_id, latest[sym]["price"])
            if latest:
                STATE["latest"] = latest
                STATE["fine"].append((now, snap))
                if now - STATE["last_coarse_ts"] >= COARSE_INTERVAL:
                    STATE["coarse"].append((now, snap))
                    STATE["last_coarse_ts"] = now
                STATE["last_update"] = now
                STATE["connected"] = True
                STATE["poll_count"] += 1
                STATE["error"] = None
        except Exception as e:  # noqa: BLE001
            STATE["connected"] = False
            STATE["error"] = str(e)
            logger.error("poll error: %s", e)
        # aim for ~1s cadence
        elapsed = time.time() - t0
        await asyncio.sleep(max(0.2, 1.0 - elapsed))


def _find_ref(target_ts: float) -> Optional[dict]:
    """Snapshot with the largest ts <= target_ts across both histories, or None."""
    best, best_ts = None, -1.0
    for hist in (STATE["fine"], STATE["coarse"]):
        for ts, snap in reversed(hist):
            if ts <= target_ts:
                if ts > best_ts:
                    best_ts, best = ts, snap
                break
    return best


def _side(change: float) -> str:
    return "buy" if change >= 0 else "sell"


def compute_trades(window: int):
    """Return (dict symbol -> {"trades": int, "side": "buy"|"sell"}, is_approx).

    side = net aggressor direction over the window, derived from price movement:
    price up over the window => net buying, price down => net selling.
    """
    latest = STATE["latest"]
    if window == DAY:  # exact trade count from ticker; side from 24h change
        return ({s: {"trades": v["count24h"], "side": _side(v["change"])}
                 for s, v in latest.items()}, False)
    now = time.time()
    ref = _find_ref(now - window)
    if ref is None:
        return ({s: {"trades": round(v["count24h"] * window / DAY), "side": _side(v["change"])}
                 for s, v in latest.items()}, True)
    out = {}
    for s, v in latest.items():
        then = ref.get(s)
        if then is None:
            out[s] = {"trades": round(v["count24h"] * window / DAY), "side": _side(v["change"])}
        else:
            then_id, then_price = then
            side = "buy" if v["price"] >= then_price else "sell"
            out[s] = {"trades": max(0, v["lastId"] - then_id), "side": side}
    return out, False


def _history_depth() -> float:
    ts_list = [h[0][0] for h in (STATE["fine"], STATE["coarse"]) if len(h)]
    if not ts_list:
        return 0.0
    return time.time() - min(ts_list)


# ----------------------------- Endpoints -----------------------------
@api_router.get("/")
async def root():
    return {"message": "Binance Token Scanner API", "pairs": len(STATE["latest"])}


@api_router.get("/timeframes")
async def get_timeframes():
    return {"timeframes": [k for k, _ in TIMEFRAMES]}


@api_router.get("/engine/status")
async def engine_status():
    return {
        "connected": STATE["connected"],
        "pairsTracked": len(STATE["latest"]),
        "quote": QUOTE,
        "source": BINANCE_BASE,
        "pollCount": STATE["poll_count"],
        "lastUpdate": STATE["last_update"],
        "uptimeSec": round(time.time() - STATE["start_ts"]),
        "historyDepthSec": round(_history_depth()),
        "error": STATE["error"],
    }


@api_router.get("/tokens")
async def get_tokens(
    timeframe: str = Query("1s"),
    search: str = Query(""),
    sort: str = Query("trades_desc"),
    limit: int = Query(1000, le=2000),
):
    if timeframe not in TF_SECONDS:
        raise HTTPException(400, f"invalid timeframe. valid: {list(TF_SECONDS)}")
    if not STATE["latest"]:
        return {"timeframe": timeframe, "approx": True, "total": 0, "tokens": [], "warming": True}

    trades, approx = compute_trades(TF_SECONDS[timeframe])
    latest = STATE["latest"]
    q = search.strip().upper()

    rows = []
    total_trades = 0
    for sym, v in latest.items():
        if q and q not in sym:
            continue
        cell = trades.get(sym) or {"trades": 0, "side": "buy"}
        tc = cell["trades"]
        total_trades += tc
        rows.append({
            "symbol": sym,
            "base": v["base"],
            "price": v["price"],
            "change": v["change"],
            "quoteVol": v["quoteVol"],
            "trades": tc,
            "side": cell["side"],
            "trades24h": v["count24h"],
        })

    reverse = sort.endswith("_desc")
    key = sort.rsplit("_", 1)[0]
    keymap = {
        "trades": lambda r: r["trades"],
        "change": lambda r: r["change"],
        "volume": lambda r: r["quoteVol"],
        "price": lambda r: r["price"],
        "symbol": lambda r: r["symbol"],
    }
    rows.sort(key=keymap.get(key, keymap["trades"]), reverse=reverse)
    matched = len(rows)
    rows = rows[:limit]
    return {
        "timeframe": timeframe,
        "approx": approx,
        "total": matched,
        "totalPairs": len(latest),
        "totalTrades": total_trades,
        "historyDepthSec": round(_history_depth()),
        "tokens": rows,
    }


@api_router.get("/token/{symbol}")
async def token_detail(symbol: str):
    symbol = symbol.upper()
    v = STATE["latest"].get(symbol)
    if not v:
        raise HTTPException(404, "symbol not tracked")
    tf_counts = {}
    for name, secs in TIMEFRAMES:
        counts, approx = compute_trades(secs)
        cell = counts.get(symbol) or {"trades": 0, "side": "buy"}
        tf_counts[name] = {"trades": cell["trades"], "side": cell["side"], "approx": approx}
    return {"symbol": symbol, "base": v["base"], "price": v["price"],
            "change": v["change"], "quoteVol": v["quoteVol"],
            "trades24h": v["count24h"], "timeframes": tf_counts}


# ----------------------------- Market overview (external APIs) -----------------------------
_MO_CACHE = {"ts": 0.0, "data": None}


async def _coinbase(http, sym):
    r = await http.get(f"https://api.coinbase.com/v2/prices/{sym}/spot", timeout=8)
    return float(r.json()["data"]["amount"])


async def market_overview(http: httpx.AsyncClient):
    sources = {}
    out = {"btc": None, "eth": None, "sol": None, "totalMarketCap": None,
           "totalVolume24h": None, "btcDominance": None, "ethGasGwei": None,
           "btcTxCount24h": None, "btcHashRate": None, "activeCryptos": None}

    async def cb():
        try:
            btc, eth, sol = await asyncio.gather(
                _coinbase(http, "BTC-USD"), _coinbase(http, "ETH-USD"), _coinbase(http, "SOL-USD"))
            out["btc"], out["eth"], out["sol"] = btc, eth, sol
            sources["coinbase"] = "ok"
        except Exception as e:  # noqa: BLE001
            sources["coinbase"] = f"error: {e}"

    async def bc():
        try:
            r = await http.get("https://api.blockchain.info/stats?cors=true", timeout=8)
            d = r.json()
            out["btcTxCount24h"] = int(d.get("n_tx", 0))
            out["btcHashRate"] = float(d.get("hash_rate", 0))
            sources["blockchain.com"] = "ok"
        except Exception as e:  # noqa: BLE001
            sources["blockchain.com"] = f"error: {e}"

    async def cg():
        try:
            headers = {}
            if COINGECKO_API_KEY:
                headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
            r = await http.get("https://api.coingecko.com/api/v3/global", headers=headers, timeout=8)
            if r.status_code == 429:
                sources["coingecko"] = "rate limited (add COINGECKO_API_KEY for higher limits)"
                return
            r.raise_for_status()
            d = r.json()["data"]
            out["totalMarketCap"] = d["total_market_cap"]["usd"]
            out["totalVolume24h"] = d["total_volume"]["usd"]
            out["btcDominance"] = d["market_cap_percentage"]["btc"]
            out["activeCryptos"] = d["active_cryptocurrencies"]
            sources["coingecko"] = "ok"
        except Exception as e:  # noqa: BLE001
            sources["coingecko"] = f"error: {e}"

    async def cmc():
        if not COINMARKETCAP_API_KEY:
            sources["coinmarketcap"] = "no api key"
            return
        try:
            r = await http.get(
                "https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest",
                headers={"X-CMC_PRO_API_KEY": COINMARKETCAP_API_KEY}, timeout=8)
            d = r.json()["data"]
            if out["totalMarketCap"] is None:
                out["totalMarketCap"] = d["quote"]["USD"]["total_market_cap"]
                out["totalVolume24h"] = d["quote"]["USD"]["total_volume_24h"]
                out["btcDominance"] = d["btc_dominance"]
                out["activeCryptos"] = d["active_cryptocurrencies"]
            sources["coinmarketcap"] = "ok"
        except Exception as e:  # noqa: BLE001
            sources["coinmarketcap"] = f"error: {e}"

    async def eth_gas():
        if not ETHERSCAN_API_KEY:
            sources["etherscan"] = "no api key"
            return
        try:
            r = await http.get(
                f"https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={ETHERSCAN_API_KEY}",
                timeout=8)
            d = r.json()
            if d.get("status") == "1":
                out["ethGasGwei"] = float(d["result"]["ProposeGasPrice"])
                sources["etherscan"] = "ok"
            else:
                sources["etherscan"] = f"error: {d.get('message') or d.get('result')}"
        except Exception as e:  # noqa: BLE001
            sources["etherscan"] = f"error: {e}"

    await asyncio.gather(cb(), bc(), cg(), cmc(), eth_gas())
    return {"metrics": out, "sources": sources}


@api_router.get("/market-overview")
async def get_market_overview():
    now = time.time()
    if _MO_CACHE["data"] and now - _MO_CACHE["ts"] < 30:
        return _MO_CACHE["data"]
    data = await market_overview(app.state.http)
    _MO_CACHE["data"] = data
    _MO_CACHE["ts"] = now
    return data


# ----------------------------- lifecycle -----------------------------
app.include_router(api_router)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    app.state.http = httpx.AsyncClient(headers={"User-Agent": "token-scanner/1.0"})
    app.state.poller = asyncio.create_task(poll_binance(app.state.http))
    logger.info("scanner started, source=%s quote=%s", BINANCE_BASE, QUOTE)


@app.on_event("shutdown")
async def shutdown():
    task = getattr(app.state, "poller", None)
    if task:
        task.cancel()
    http = getattr(app.state, "http", None)
    if http:
        await http.aclose()
    client.close()
