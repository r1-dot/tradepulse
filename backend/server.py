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
from pydantic import BaseModel
import os
import asyncio
import time
import hmac
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode
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
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '')
BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '')
BINANCE_TRADE_BASE_URL = os.environ.get('BINANCE_TRADE_BASE_URL', 'https://api.binance.com')

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


# ============================ AUTO-TRADE BOT ============================
# Trigger: a symbol qualifies as a "volume alert" when its 24h quote volume >=
# volThresholdUsd. When the same symbol shows the SAME raw price direction
# (buy = price up vs last poll, sell = price down) for `streak` consecutive
# polls within ~1s, the bot fires: BUY opens a long spot position (quoteOrderQty),
# SELL closes an existing position early. Each position auto-exits at TP (+tpPct)
# or SL (-slPct). Realized daily PnL <= -dailyLossLimit halts the bot.
#
# SAFETY: dryRun=True by default => simulated fills at the live mark price (no real
# orders). Set dryRun=False (LIVE) + configure BINANCE_API_KEY/SECRET to place real
# spot orders on BINANCE_TRADE_BASE_URL. api.binance.com is geo-blocked from this
# preview server, so LIVE orders only execute once deployed to a reachable region.

BOT_DEFAULTS = {
    "enabled": False,
    "dryRun": True,
    "slPct": 1.5,
    "tpPct": 2.0,
    "maxPositionUsdt": 5.0,
    "dailyLossLimit": 5.0,
    "streak": 2,
    "maxOpenPositions": 3,
    "cooldownSec": 30,
    "minVolumeUsd": 5_000_000.0,
    "maxVolumeUsd": 0.0,   # 0 = no upper limit
    "autoExit": True,      # auto-close on TP/SL; if False, positions close only on a SELL signal
}

BOT = {
    "config": dict(BOT_DEFAULTS),
    "positions": {},        # symbol -> position dict
    "journal": deque(maxlen=300),
    "dailyPnl": 0.0,
    "dailyDate": "",
    "stopped": False,       # halted by daily loss limit
    "signalStreaks": {},    # symbol -> {"side","count","startTs"}
    "lastSignalTs": 0.0,    # last alert event received from dashboard
    "lastTradeTs": {},      # symbol -> ts (cooldown)
}


def jlog(kind: str, **kw):
    entry = {"ts": time.time(), "kind": kind, **kw}
    BOT["journal"].appendleft(entry)
    return entry


async def binance_signed(http: httpx.AsyncClient, method: str, path: str, params: dict):
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        raise RuntimeError("Binance API key/secret not configured")
    p = {k: str(v) for k, v in params.items() if v is not None}
    p["timestamp"] = int(time.time() * 1000)
    p.setdefault("recvWindow", "5000")
    query = urlencode(p)
    sig = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BINANCE_TRADE_BASE_URL}{path}?{query}&signature={sig}"
    r = await http.request(method, url, headers={"X-MBX-APIKEY": BINANCE_API_KEY}, timeout=15)
    if r.status_code >= 400:
        raise RuntimeError(f"binance {r.status_code}: {r.text[:200]}")
    return r.json()


async def _live_market(http, symbol, side, quote_qty=None, base_qty=None):
    params = {"symbol": symbol, "side": side, "type": "MARKET", "newOrderRespType": "FULL"}
    if quote_qty is not None:
        params["quoteOrderQty"] = round(quote_qty, 2)
    else:
        params["quantity"] = base_qty
    res = await binance_signed(http, "POST", "/api/v3/order", params)
    executed = float(res.get("executedQty", 0) or 0)
    cq = float(res.get("cummulativeQuoteQty", 0) or 0)
    fill = (cq / executed) if executed else 0.0
    return {"executedQty": executed, "quote": cq, "fillPrice": fill, "orderId": res.get("orderId")}


async def _open_position(http, sym, price, now):
    cfg = BOT["config"]
    qty = cfg["maxPositionUsdt"] / price if price > 0 else 0
    order_id = None
    if cfg["dryRun"]:
        fill = price
        executed = qty
    else:
        try:
            r = await _live_market(http, sym, "BUY", quote_qty=cfg["maxPositionUsdt"])
            fill = r["fillPrice"] or price
            executed = r["executedQty"] or qty
            order_id = r["orderId"]
        except Exception as e:  # noqa: BLE001
            jlog("error", symbol=sym, action="BUY", message=str(e), live=True)
            return
    pos = {
        "symbol": sym, "base": sym[: -len(QUOTE)], "side": "long",
        "entryPrice": fill, "qty": executed, "quoteSpent": fill * executed,
        "tpPrice": fill * (1 + cfg["tpPct"] / 100),
        "slPrice": fill * (1 - cfg["slPct"] / 100),
        "openedTs": now, "orderId": order_id, "dryRun": cfg["dryRun"],
    }
    BOT["positions"][sym] = pos
    BOT["lastTradeTs"][sym] = now
    jlog("entry", symbol=sym, base=pos["base"], side="buy", price=fill, qty=executed,
         spent=pos["quoteSpent"], tp=pos["tpPrice"], sl=pos["slPrice"],
         mode="SIM" if cfg["dryRun"] else "LIVE")


async def _close_position(http, sym, price, reason):
    pos = BOT["positions"].pop(sym, None)
    if not pos:
        return
    cfg = BOT["config"]
    fill = price
    if not cfg["dryRun"] and not pos.get("dryRun"):
        try:
            r = await _live_market(http, sym, "SELL", base_qty=pos["qty"])
            fill = r["fillPrice"] or price
        except Exception as e:  # noqa: BLE001
            jlog("error", symbol=sym, action="SELL", message=str(e), live=True)
    pnl = (fill - pos["entryPrice"]) * pos["qty"]
    BOT["dailyPnl"] += pnl
    BOT["lastTradeTs"][sym] = time.time()
    jlog("exit", symbol=sym, base=pos["base"], side="sell", price=fill,
         entry=pos["entryPrice"], qty=pos["qty"], pnl=pnl, reason=reason,
         mode="SIM" if pos.get("dryRun") else "LIVE")
    if BOT["dailyPnl"] <= -cfg["dailyLossLimit"]:
        BOT["stopped"] = True
        cfg["enabled"] = False
        jlog("halt", message=f"Daily loss limit hit ({BOT['dailyPnl']:.2f} USDT). Bot stopped.")
        await save_bot_config()


async def process_bot(http: httpx.AsyncClient, now: float):
    cfg = BOT["config"]
    latest = STATE["latest"]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if BOT["dailyDate"] != today:
        BOT["dailyDate"] = today
        BOT["dailyPnl"] = 0.0
        BOT["stopped"] = False
        jlog("daily_reset", date=today)

    # 1) manage TP/SL exits (only when autoExit enabled; otherwise exits happen
    #    solely on a SELL signal via handle_signal)
    if cfg.get("autoExit", True):
        for sym in list(BOT["positions"].keys()):
            v = latest.get(sym)
            if not v:
                continue
            price = v["price"]
            pos = BOT["positions"][sym]
            if price >= pos["tpPrice"]:
                await _close_position(http, sym, price, "take-profit")
            elif price <= pos["slPrice"]:
                await _close_position(http, sym, price, "stop-loss")

    # Entries are driven ONLY by alert-history events pushed from the dashboard
    # (see handle_signal + POST /api/bot/signal). If no alerts are enabled/feeding,
    # the bot stays inactive. process_bot only handles daily reset + protective exits.


async def handle_signal(http: httpx.AsyncClient, sym: str, side: str, price: float, now: float, volume: float = 0.0):
    """Consume one alert-history event; fire a trade on `streak` consecutive
    same-direction alerts for the same symbol within ~1s. BUY only on buy signals,
    SELL only on sell signals; both gated by the token volume filter."""
    cfg = BOT["config"]
    if not cfg["enabled"] or BOT["stopped"]:
        return
    info = STATE["latest"].get(sym)  # only trade tracked symbols; use authoritative server data
    if not info:
        return
    price = info["price"]
    volume = info["quoteVol"]
    if price <= 0 or volume < cfg.get("minVolumeUsd", 0):  # server-side volume gate (anti-spoof)
        return
    _maxv = cfg.get("maxVolumeUsd", 0) or 0
    if _maxv > 0 and volume > _maxv:  # upper bound of the volume band
        return
    st = BOT["signalStreaks"].get(sym)
    if st and st["side"] == side and (now - st["startTs"]) <= 1.5:
        st["count"] += 1
    else:
        st = {"side": side, "count": 1, "startTs": now}
        BOT["signalStreaks"][sym] = st
    if st["count"] >= cfg["streak"]:
        cd = now - BOT["lastTradeTs"].get(sym, 0) >= cfg["cooldownSec"]
        if side == "buy":
            if sym not in BOT["positions"] and cd and len(BOT["positions"]) < cfg["maxOpenPositions"]:
                await _open_position(http, sym, price, now)
        else:  # sell closes an open position early
            if sym in BOT["positions"]:
                await _close_position(http, sym, price, "sell-signal")
        BOT["signalStreaks"].pop(sym, None)


async def save_bot_config():
    await db.bot_config.update_one({"_id": "config"}, {"$set": BOT["config"]}, upsert=True)


async def load_bot_config():
    doc = await db.bot_config.find_one({"_id": "config"})
    if doc:
        for k in BOT_DEFAULTS:
            if k in doc:
                BOT["config"][k] = doc[k]



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
                try:
                    await process_bot(http, now)
                except Exception as be:  # noqa: BLE001
                    logger.error("bot error: %s", be)
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


# ----------------------------- Auto-Trade Bot endpoints -----------------------------
class BotConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    dryRun: Optional[bool] = None
    slPct: Optional[float] = None
    tpPct: Optional[float] = None
    maxPositionUsdt: Optional[float] = None
    dailyLossLimit: Optional[float] = None
    streak: Optional[int] = None
    maxOpenPositions: Optional[int] = None
    cooldownSec: Optional[int] = None
    minVolumeUsd: Optional[float] = None
    maxVolumeUsd: Optional[float] = None
    autoExit: Optional[bool] = None


def _bot_status():
    cfg = BOT["config"]
    latest = STATE["latest"]
    positions = []
    unrealized = 0.0
    for sym, pos in BOT["positions"].items():
        price = latest.get(sym, {}).get("price", pos["entryPrice"])
        upnl = (price - pos["entryPrice"]) * pos["qty"]
        unrealized += upnl
        positions.append({
            "symbol": sym, "base": pos["base"], "entryPrice": pos["entryPrice"],
            "currentPrice": price, "qty": pos["qty"], "quoteSpent": pos["quoteSpent"],
            "tpPrice": pos["tpPrice"], "slPrice": pos["slPrice"],
            "uPnl": upnl, "openedTs": pos["openedTs"], "mode": "SIM" if pos.get("dryRun") else "LIVE",
        })
    now = time.time()
    alerts_feeding = (now - BOT["lastSignalTs"]) < 6 and BOT["lastSignalTs"] > 0
    return {
        "config": cfg,
        "stopped": BOT["stopped"],
        "dailyPnl": BOT["dailyPnl"],
        "dailyDate": BOT["dailyDate"],
        "unrealizedPnl": unrealized,
        "openPositions": positions,
        "journal": list(BOT["journal"])[:100],
        "keysConfigured": bool(BINANCE_API_KEY and BINANCE_API_SECRET),
        "tradeBase": BINANCE_TRADE_BASE_URL,
        "alertsFeeding": alerts_feeding,
        "lastSignalAgo": round(now - BOT["lastSignalTs"], 1) if BOT["lastSignalTs"] else None,
        "active": bool(cfg["enabled"] and not BOT["stopped"] and alerts_feeding),
    }


@api_router.get("/bot/status")
async def bot_status():
    return _bot_status()


@api_router.post("/bot/config")
async def bot_config(update: BotConfigUpdate):
    cfg = BOT["config"]
    data = update.model_dump(exclude_none=True)
    # basic clamps
    if "slPct" in data:
        data["slPct"] = max(0.1, min(50.0, data["slPct"]))
    if "tpPct" in data:
        data["tpPct"] = max(0.1, min(100.0, data["tpPct"]))
    if "maxPositionUsdt" in data:
        data["maxPositionUsdt"] = max(1.0, min(100000.0, data["maxPositionUsdt"]))
    if "dailyLossLimit" in data:
        data["dailyLossLimit"] = max(0.5, min(100000.0, data["dailyLossLimit"]))
    if "streak" in data:
        data["streak"] = max(1, min(20, int(data["streak"])))
    if "maxOpenPositions" in data:
        data["maxOpenPositions"] = max(1, min(50, int(data["maxOpenPositions"])))
    if "cooldownSec" in data:
        data["cooldownSec"] = max(0, min(3600, int(data["cooldownSec"])))
    if "minVolumeUsd" in data:
        data["minVolumeUsd"] = max(0.0, float(data["minVolumeUsd"]))
    if "maxVolumeUsd" in data:
        data["maxVolumeUsd"] = max(0.0, float(data["maxVolumeUsd"]))
    if data.get("enabled"):
        BOT["stopped"] = False  # re-enabling clears the halt
    cfg.update(data)
    await save_bot_config()
    if "enabled" in data:
        jlog("power", message=f"Bot {'STARTED' if data['enabled'] else 'STOPPED'}",
             mode="SIM" if cfg["dryRun"] else "LIVE")
    return _bot_status()


class SignalIn(BaseModel):
    symbol: str
    side: str
    price: float
    volume: Optional[float] = 0.0


class SignalBatch(BaseModel):
    events: list[SignalIn]


@api_router.post("/bot/signal")
async def bot_signal(batch: SignalBatch):
    now = time.time()
    BOT["lastSignalTs"] = now
    fired = 0
    for e in batch.events[:200]:
        if e.side in ("buy", "sell") and e.price > 0:
            await handle_signal(app.state.http, e.symbol.upper(), e.side, e.price, now, e.volume or 0.0)
            fired += 1
    return {"ok": True, "received": fired, "openPositions": len(BOT["positions"]), "active": bool(BOT["config"]["enabled"] and not BOT["stopped"])}


@api_router.post("/bot/close-all")
async def bot_close_all():
    latest = STATE["latest"]
    closed = 0
    for sym in list(BOT["positions"].keys()):
        price = latest.get(sym, {}).get("price", BOT["positions"][sym]["entryPrice"])
        await _close_position(app.state.http, sym, price, "manual-close")
        closed += 1
    jlog("kill_switch", message=f"Manually closed {closed} position(s)")
    return _bot_status()


@api_router.post("/bot/reset-daily")
async def bot_reset_daily():
    BOT["dailyPnl"] = 0.0
    BOT["stopped"] = False
    jlog("daily_reset", message="Manual daily reset")
    return _bot_status()



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
    try:
        await load_bot_config()
    except Exception as e:  # noqa: BLE001
        logger.error("bot config load failed: %s", e)
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
