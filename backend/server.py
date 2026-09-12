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
from fastapi.responses import ORJSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
import os
import asyncio
import time
import json
import hmac
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode
from pathlib import Path
from collections import deque
from typing import Optional
import hyperliquid_converter as hlconv
import hybrid_power as hybrid
import httpx

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

BINANCE_BASE = os.environ.get('BINANCE_BASE', 'https://data-api.binance.vision')
QUOTES = [q.strip().upper() for q in os.environ.get('BINANCE_QUOTE', 'USDT,USDC').split(',') if q.strip()]
QUOTE = QUOTES[0]
ETHERSCAN_API_KEY = os.environ.get('ETHERSCAN_API_KEY', '')
BLOCKCHAIN_API_KEY = os.environ.get('BLOCKCHAIN_API_KEY', '')
COINMARKETCAP_API_KEY = os.environ.get('COINMARKETCAP_API_KEY', '')
COINGECKO_API_KEY = os.environ.get('COINGECKO_API_KEY', '')
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '')
BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '')
BINANCE_TRADE_BASE_URL = os.environ.get('BINANCE_TRADE_BASE_URL', 'https://api.binance.com')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("scanner")

app = FastAPI(default_response_class=ORJSONResponse)
app.add_middleware(GZipMiddleware, minimum_size=800)
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
    "slPct": 1.0,
    "tpPct": 2.0,
    "maxPositionUsdt": 5.0,
    "dailyLossLimit": 5.0,
    "streak": 2,
    "maxOpenPositions": 3,
    "cooldownSec": 30,
    "minVolumeUsd": 5_000_000.0,
    "maxVolumeUsd": 0.0,   # 0 = no upper limit
    "autoExit": True,      # auto-close on TP/SL; if False, positions close only on a SELL signal
    "webhookEnabled": False,
    "webhookUrl": "https://wtalerts.com/bot/runbot",
    "webhookBuyMsg": "",   # message sent on BUY (paste your WunderTrading bot signal)
    "webhookSellMsg": "",  # message sent on SELL
    "exchange": "binance", # "binance" | "hyperliquid"
    "hlTestnet": False,    # Hyperliquid: use testnet
    "maxLossPerTradeUsdt": 0.0002,  # hard cap: auto-close any position at this $ loss
    # --- STRADDLE SYSTEM: on a volume spike, arm a LONG stop above + SHORT stop below.
    # Whichever side price hits first fills; the other is cancelled (OCO breakout).
    "straddleEnabled": False,
    "straddleEntryPct": 0.5,   # entry offset from mark (0.000001 - 5 %)
    "straddleTpPct": 1.0,      # take-profit for the filled leg (0.01 - 10 %)
    "straddleSlPct": 1.0,      # stop-loss for the filled leg (0.0001 - 5 %)
    # --- HYPERLIQUID PERPS: leverage + margin mode (applied per coin before each order)
    "hlLeverage": 1,           # target leverage (clamped to each coin's max)
    "hlCrossMargin": True,     # True = cross margin, False = isolated
    "hlNativeTpsl": True,      # place native TP/SL trigger orders on Hyperliquid at entry
    "hlSlippagePct": 0.0001,   # extra % added to the signal price for instant marketable fill
    # --- HYBRID POWER SYSTEM (short-window power/flow signal engine, Isolated 3x)
    "hybrid_toggle": True,
    "sl_percent": 0.8,
    "tp_percent": 2.0,
    "min_power_1m": 0.6,
    "max_power_1m": 1.8,
    "burst_power": 0.06,
    # --- Orderbook Delta (whale wall) filter for hybrid entries
    "obDeltaFilter": True,
    "minDeltaLong": 1.5,
    "minDeltaShort": 1.5,
    "minPwr1m": 0.65,
    # --- Smart Trailing TP/SL (replaces fixed TP)
    "trailEnabled": True,
    "trailInitialSlPct": 0.30,
    "trailSecure": 0.40,
    "trailBE": 0.05,
    "trailStep": 0.5,
    "trailLock": 50,
    "trailCallback": 0.30,
}

STRADDLE_EXPIRY = 600.0  # seconds an un-filled straddle stays armed before auto-cancel

BOT = {
    "config": dict(BOT_DEFAULTS),
    "positions": {},        # symbol -> position dict
    "straddles": {},        # symbol -> pending straddle (armed long/short stop levels)
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


async def send_webhook(action: str, symbol: str, price: float):
    """POST a buy/sell signal to the configured external webhook (e.g. WunderTrading)."""
    cfg = BOT["config"]
    url = cfg.get("webhookUrl") or ""
    if not cfg.get("webhookEnabled") or not url:
        return
    tmpl = cfg.get("webhookBuyMsg") if action == "buy" else cfg.get("webhookSellMsg")
    if tmpl:
        msg = (tmpl.replace("{{symbol}}", symbol).replace("{{ticker}}", symbol)
                   .replace("{{price}}", str(price)).replace("{{action}}", action)
                   .replace("{{side}}", action))
    else:
        msg = json.dumps({"action": action, "symbol": symbol, "price": price, "exchange": "BINANCE"})
    try:
        r = await app.state.http.post(url, content=msg.encode(), headers={"Content-Type": "text/plain"}, timeout=10)
        jlog("webhook", action=action, symbol=symbol, status=r.status_code, message=msg[:80])
    except Exception as e:  # noqa: BLE001
        jlog("webhook", action=action, symbol=symbol, status="error", message=str(e)[:80])


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


_HL = {"exchange": None, "info": None, "net": None, "agentAddr": None, "mainAddr": None}


def _get_hl_exchange():
    """Build a Hyperliquid Exchange. signer = API/agent private key; account_address = MAIN
    funded wallet that approved the agent. These are NOT the same address for agent wallets."""
    import eth_account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    key = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "") or os.environ.get("HYPERLIQUID_SECRET_KEY", "")
    addr = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "") or os.environ.get("HYPERLIQUID_MAIN_WALLET", "")
    if not key or not addr:
        raise RuntimeError("Hyperliquid key/address not configured")
    net = "testnet" if BOT["config"].get("hlTestnet") else "mainnet"
    base = constants.TESTNET_API_URL if net == "testnet" else constants.MAINNET_API_URL
    signer = eth_account.Account.from_key(key)
    _HL["agentAddr"] = signer.address
    _HL["mainAddr"] = addr
    # DEBUG: verify the wallet derived from the secret is the expected API/agent wallet
    logger.info("Hyperliquid init | net=%s | Main(account_address)=%s | API wallet derived from secret=%s | same=%s",
                net, addr, signer.address, signer.address.lower() == addr.lower())
    if _HL["exchange"] is None or _HL["net"] != net:
        _HL["info"] = Info(base, skip_ws=True)
        _HL["exchange"] = Exchange(signer, base, account_address=addr)
        _HL["net"] = net
    return _HL["exchange"]


async def _ensure_hl_ready():
    """Build the HL client and load the coin/szDecimals map (blocking calls off the loop)."""
    def _build():
        _get_hl_exchange()
        if not hlconv.meta_loaded():
            hlconv.load_hl_meta(_HL["info"])
    await asyncio.to_thread(_build)


async def _hl_market_open(coin, size, is_buy=True, leverage=None, is_cross=True, slippage=0.01):
    ex = _get_hl_exchange()
    if leverage:
        # perps: set leverage + margin mode per coin (clamped to the coin's max)
        lev = max(1, min(int(leverage), hlconv.max_leverage(coin)))
        try:
            await asyncio.to_thread(ex.update_leverage, lev, coin, bool(is_cross))
        except Exception as e:  # noqa: BLE001
            jlog("warn", symbol=coin, message=f"update_leverage failed: {str(e)[:120]}", live=True)
    res = await asyncio.to_thread(ex.market_open, coin, is_buy, size, None, slippage)
    if res.get("status") != "ok":
        raise RuntimeError(str(res)[:200])
    data = res.get("response", {}).get("data", {}) or {}
    statuses = data.get("statuses", []) or []
    filled = next((s["filled"] for s in statuses if isinstance(s, dict) and "filled" in s), None)
    # fill price can arrive as filled.avgPx, data.fills[0].px, or data.avgPx depending on SDK/route
    px = None
    if filled:
        px = filled.get("avgPx") or filled.get("px")
    if px in (None, ""):
        fills = data.get("fills") or []
        if fills and isinstance(fills[0], dict):
            px = fills[0].get("px") or fills[0].get("avgPx")
    if px in (None, ""):
        px = data.get("avgPx")
    fill_px = float(px) if px not in (None, "") else 0.0
    if not fill_px:
        # fallback: read the current mid price from Hyperliquid so we never store a 0/None fill
        try:
            mids = _HL["info"].all_mids()
            fill_px = float(mids.get(coin) or 0.0)
        except Exception:  # noqa: BLE001
            fill_px = 0.0
    exec_sz = filled.get("totalSz") if filled else None
    exec_sz = float(exec_sz) if exec_sz not in (None, "") else float(size)
    return {"fillPrice": fill_px, "executedQty": exec_sz}


async def _hl_market_close(base_coin):
    ex = _get_hl_exchange()
    res = await asyncio.to_thread(ex.market_close, base_coin)
    if res and res.get("status") != "ok":
        raise RuntimeError(str(res)[:200])
    fills = (res or {}).get("response", {}).get("data", {}).get("statuses", [])
    filled = next((s["filled"] for s in fills if "filled" in s), None)
    return {"fillPrice": float(filled["avgPx"]) if filled else 0.0}


async def _hl_place_tpsl(coin, is_long, sz, tp_px, sl_px):
    """Place reduce-only TP + SL trigger orders on Hyperliquid so the EXCHANGE closes the
    position when target/stop is hit (survives server downtime, executes instantly).
    Either leg may be None (e.g. trailing positions have no fixed TP) — skip those.
    Returns [{"tag","status","oid"}] so callers can track/cancel the exact order."""
    def _place():
        ex = _get_hl_exchange()
        close_is_buy = not is_long  # close a LONG -> sell; close a SHORT -> buy
        out = []
        for tag, trig in (("tp", tp_px), ("sl", sl_px)):
            if trig is None:
                continue  # no fixed level for this leg (trailing manages it in software)
            try:
                trig_f = float(trig)
            except (TypeError, ValueError):
                continue
            if trig_f <= 0:
                continue
            trig_r = hlconv.round_price(coin, trig_f) or trig_f
            lim = trig_r * (1.08 if close_is_buy else 0.92)  # aggressive limit so market trigger fills
            lim_r = hlconv.round_price(coin, lim) or trig_r
            ot = {"trigger": {"triggerPx": trig_r, "isMarket": True, "tpsl": tag}}
            try:
                r = ex.order(coin, close_is_buy, sz, lim_r, ot, reduce_only=True)
                out.append({"tag": tag, "status": r.get("status") if isinstance(r, dict) else "ok",
                            "oid": _extract_oid(r)})
            except Exception as e:  # noqa: BLE001
                out.append({"tag": tag, "status": f"err:{str(e)[:80]}", "oid": None})
        return out
    return await asyncio.to_thread(_place)


def _extract_oid(r):
    """Pull the order id out of a Hyperliquid order response (resting or filled)."""
    try:
        st = r["response"]["data"]["statuses"][0]
        if isinstance(st, dict):
            return (st.get("resting") or st.get("filled") or {}).get("oid")
    except Exception:  # noqa: BLE001
        pass
    return None


def _order_coin(o):
    return (o.get("coin") or (o.get("order") or {}).get("coin") or "")


def _order_oid(o):
    return o.get("oid") if o.get("oid") is not None else (o.get("order") or {}).get("oid")


def _hl_list_orders(coin):
    """Return raw open orders (resting + trigger) for a coin, matching case-insensitively and
    handling both the flat and {'order': {...}} response shapes across SDK routes."""
    addr = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "") or os.environ.get("HYPERLIQUID_MAIN_WALLET", "")
    orders = []
    for getter in ("frontend_open_orders", "open_orders"):
        try:
            fn = getattr(_HL["info"], getter, None)
            if not fn:
                continue
            got = fn(addr) or []
            if got:
                orders = got
                break
        except Exception:  # noqa: BLE001
            continue
    cu = str(coin).upper()
    return [o for o in orders if str(_order_coin(o)).upper() == cu]


async def _hl_sync_stop(pos, sym, stop_price):
    """CANCEL-REPLACE the native reduce-only SL on Hyperliquid so the exchange always holds
    exactly ONE stop at the latest trailed price. Cancels the tracked SL by oid, sweeps any
    stragglers for the coin, places one fresh SL, then verifies only one remains."""
    cfg = BOT["config"]
    if cfg["dryRun"] or pos.get("dryRun") or cfg.get("exchange") != "hyperliquid" or not cfg.get("hlNativeTpsl", True):
        return
    hl_coin = hlconv.convert_binance_to_hyperliquid(pos["base"])
    if not hl_coin:
        return
    is_long = pos.get("side", "long") == "long"
    sz = hlconv.round_size(hl_coin, pos["qty"]) or pos["qty"]
    old_px = pos.get("nativeSlPrice")
    try:
        await _ensure_hl_ready()
        cancelled = await _hl_cancel_coin(hl_coin)  # kill ALL existing SL/TP triggers for this coin
        res = await _hl_place_tpsl(hl_coin, is_long, sz, tp_px=None, sl_px=stop_price)
        new_oid = next((r.get("oid") for r in res if r.get("tag") == "sl"), None)
        # verify: ensure exactly ONE SL trigger survives (cancel any extra, keep the newest)
        removed = await _hl_dedupe_sl(hl_coin, new_oid)
        pos["nativeSlPrice"] = stop_price
        pos["nativeSlOid"] = new_oid
        _op = f"{old_px:.6g}" if isinstance(old_px, (int, float)) else "none"
        jlog("tpsl", symbol=hl_coin, base=pos["base"], sl=stop_price, live=True,
             message=f"CANCELED old SL {_op} -> PLACED new SL {stop_price:.6g} (cancelled {cancelled + removed} stale, oid={new_oid})")
    except Exception as ex:  # noqa: BLE001
        jlog("warn", symbol=hl_coin, base=pos["base"], live=True,
             message=f"native SL move failed ({str(ex)[:100]}) — software monitor still active")


async def _hl_dedupe_sl(coin, keep_oid):
    """Safety net: cancel every SL trigger for `coin` except `keep_oid`, so only one stop ever
    rests on the exchange. Returns the number cancelled."""
    def _dd():
        ex = _get_hl_exchange()
        removed = 0
        for o in _hl_list_orders(coin):
            oid = _order_oid(o)
            is_trig = o.get("isTrigger") or (o.get("order") or {}).get("isTrigger") or o.get("triggerPx") or o.get("orderType")
            if oid is None or oid == keep_oid or not is_trig:
                continue
            try:
                ex.cancel(coin, oid)
                removed += 1
            except Exception:  # noqa: BLE001
                pass
        return removed
    try:
        return await asyncio.to_thread(_dd)
    except Exception:  # noqa: BLE001
        return 0



async def _hl_cancel_coin(coin):
    """Cancel ALL resting + trigger (TP/SL) orders for a coin. Trigger orders do NOT show up
    in open_orders — they only appear in frontend_open_orders — and item shape can be flat or
    nested under 'order', so we handle both (prevents duplicate SLs stacking up). Best-effort."""
    def _cxl():
        ex = _get_hl_exchange()
        n = 0
        seen = set()
        for o in _hl_list_orders(coin):
            oid = _order_oid(o)
            if oid is None or oid in seen:
                continue
            seen.add(oid)
            try:
                ex.cancel(coin, oid)
                n += 1
            except Exception:  # noqa: BLE001
                pass
        return n
    try:
        return await asyncio.to_thread(_cxl)
    except Exception:  # noqa: BLE001
        return 0


async def _open_position(http, sym, price, now, side="long", tp_price=None, sl_price=None, source="signal", leverage=None, is_cross=None):
    cfg = BOT["config"]
    is_long = side == "long"
    qty = cfg["maxPositionUsdt"] / price if price > 0 else 0
    order_id = None
    lev = leverage if leverage is not None else cfg.get("hlLeverage", 1)
    cross = is_cross if is_cross is not None else cfg.get("hlCrossMargin", True)
    # base coin, stripped of USDT/USDC (safe even if sym dropped out of the latest snapshot)
    base = (STATE["latest"].get(sym) or {}).get("base") or hlconv.strip_quote(sym)
    if cfg["dryRun"]:
        fill = price
        executed = qty
    elif cfg.get("exchange") == "hyperliquid":
        try:
            await _ensure_hl_ready()
            conv = hlconv.get_rounded_size_and_price(base, qty, price)
            if conv is None:
                # coin not listed on Hyperliquid perps, or size rounds to 0 -> skip + log once
                skipped = BOT.setdefault("hlSkipped", set())
                if base not in skipped:
                    skipped.add(base)
                    reason = "not listed on Hyperliquid perps" if hlconv.convert_binance_to_hyperliquid(base) is None else "order size rounds to 0"
                    jlog("skip", symbol=sym, base=base, message=f"{base}: {reason} — skipped", live=True, venue="hyperliquid")
                return
            hl_coin, rsize, _ = conv
            r = await _hl_market_open(hl_coin, rsize, is_buy=is_long,
                                      leverage=lev, is_cross=cross,
                                      slippage=max(0.0, cfg.get("hlSlippagePct", 0.0001)) / 100.0)
            fill = r["fillPrice"] or price
            executed = r["executedQty"] or rsize
        except Exception as e:  # noqa: BLE001
            # show the actual Hyperliquid coin sent (e.g. CRV), not the Binance pair (CRVUSDT)
            hl_coin = locals().get("hl_coin") or hlconv.convert_binance_to_hyperliquid(base) or base
            jlog("error", symbol=hl_coin, action="LONG" if is_long else "SHORT",
                 message=f"HL sent coin '{hl_coin}': {str(e)[:180]}", live=True, venue="hyperliquid")
            return
    else:
        if not is_long:
            jlog("error", symbol=sym, action="SHORT",
                 message="Short entries need Hyperliquid or simulated mode — Binance spot is long-only, short leg skipped.",
                 live=True)
            return
        try:
            r = await _live_market(http, sym, "BUY", quote_qty=cfg["maxPositionUsdt"])
            fill = r["fillPrice"] or price
            executed = r["executedQty"] or qty
            order_id = r["orderId"]
        except Exception as e:  # noqa: BLE001
            jlog("error", symbol=sym, action="BUY", message=str(e), live=True)
            return
    # Only apply the FIXED tpPct/slPct defaults when the caller passed neither.
    # Straddle & hybrid explicitly pass tp_price=None (trailing manages upside) with
    # a computed sl_price — we MUST preserve tp_price=None so process_bot routes them
    # through _update_trailing() instead of hitting the fixed take-profit.
    if tp_price is None and sl_price is None:
        if is_long:
            tp_price = fill * (1 + cfg["tpPct"] / 100)
            sl_price = fill * (1 - cfg["slPct"] / 100)
        else:
            tp_price = fill * (1 - cfg["tpPct"] / 100)
            sl_price = fill * (1 + cfg["slPct"] / 100)
    pos = {
        "symbol": sym, "base": base, "side": side,
        "entryPrice": fill, "qty": executed, "quoteSpent": fill * executed,
        "tpPrice": tp_price, "slPrice": sl_price,
        "openedTs": now, "orderId": order_id, "dryRun": cfg["dryRun"], "source": source,
    }
    BOT["positions"][sym] = pos
    BOT["lastTradeTs"][sym] = now
    # LIVE Hyperliquid: register TP + SL as native reduce-only trigger orders on the exchange
    # so the position is closed by Hyperliquid itself when target/stop is reached.
    if not cfg["dryRun"] and cfg.get("exchange") == "hyperliquid" and cfg.get("hlNativeTpsl", True):
        hl_coin = hlconv.convert_binance_to_hyperliquid(base)
        if hl_coin:
            try:
                await _hl_cancel_coin(hl_coin)  # clear any stale triggers first
                res = await _hl_place_tpsl(hl_coin, is_long, hlconv.round_size(hl_coin, executed) or executed, tp_price, sl_price)
                pos["nativeSlOid"] = next((r.get("oid") for r in res if r.get("tag") == "sl"), None)
                if sl_price is not None:
                    pos["nativeSlPrice"] = sl_price
                _tp_s = f"{tp_price:.6g}" if tp_price is not None else "trail"
                _sl_s = f"{sl_price:.6g}" if sl_price is not None else "—"
                jlog("tpsl", symbol=hl_coin, base=base, tp=tp_price, sl=sl_price,
                     message=f"Native TP {_tp_s} / SL {_sl_s} placed on Hyperliquid: {res}", live=True)
            except Exception as e:  # noqa: BLE001
                jlog("warn", symbol=hl_coin, base=base, live=True,
                     message=f"native TP/SL failed ({str(e)[:100]}) — software monitor will close on target/stop")
    jlog("entry", symbol=sym, base=pos["base"], side="buy" if is_long else "short", price=fill, qty=executed,
         spent=pos["quoteSpent"], tp=pos["tpPrice"], sl=pos["slPrice"], source=source,
         mode="SIM" if cfg["dryRun"] else "LIVE")
    await send_webhook("buy" if is_long else "sell", sym, fill)


async def _close_position(http, sym, price, reason):
    pos = BOT["positions"].pop(sym, None)
    if not pos:
        return
    cfg = BOT["config"]
    fill = price
    if not cfg["dryRun"] and not pos.get("dryRun"):
        if cfg.get("exchange") == "hyperliquid":
            try:
                await _ensure_hl_ready()
                hl_coin = hlconv.convert_binance_to_hyperliquid(pos["base"]) or pos["base"]
                r = await _hl_market_close(hl_coin)
                fill = r["fillPrice"] or price
                await _hl_cancel_coin(hl_coin)  # remove the sibling TP/SL trigger
            except Exception as e:  # noqa: BLE001
                jlog("error", symbol=sym, action="CLOSE", message=str(e)[:200], live=True, venue="hyperliquid")
        else:
            try:
                r = await _live_market(http, sym, "SELL", base_qty=pos["qty"])
                fill = r["fillPrice"] or price
            except Exception as e:  # noqa: BLE001
                jlog("error", symbol=sym, action="SELL", message=str(e), live=True)
    pnl = (fill - pos["entryPrice"]) * pos["qty"] * (1 if pos.get("side", "long") == "long" else -1)
    BOT["dailyPnl"] += pnl
    BOT["lastTradeTs"][sym] = time.time()
    jlog("exit", symbol=sym, base=pos["base"], side="sell" if pos.get("side", "long") == "long" else "cover", price=fill,
         entry=pos["entryPrice"], qty=pos["qty"], pnl=pnl, reason=reason,
         mode="SIM" if pos.get("dryRun") else "LIVE")
    await send_webhook("sell" if pos.get("side", "long") == "long" else "buy", sym, fill)
    if BOT["dailyPnl"] <= -cfg["dailyLossLimit"]:
        BOT["stopped"] = True
        cfg["enabled"] = False
        jlog("halt", message=f"Daily loss limit hit ({BOT['dailyPnl']:.2f} USDT). Bot stopped.")
        await save_bot_config()


HYBRID_STATE = {"watch": [], "watch_ts": 0.0, "rows": []}


def _hybrid_watchlist(latest, now):
    """Refresh (~every 30s) the coin set fed into the hybrid engine: top-40 by 24h volume
    plus the NORMAL_AVG coins, as (base, symbol) pairs (USDT quote only)."""
    if now - HYBRID_STATE["watch_ts"] < 30 and HYBRID_STATE["watch"]:
        return HYBRID_STATE["watch"]
    usdt = [(v["base"], sym, v.get("quoteVol", 0)) for sym, v in latest.items() if v.get("quote") == "USDT"]
    usdt.sort(key=lambda x: x[2], reverse=True)
    watch = {b: (b, s) for b, s, _ in usdt[:40]}
    for b, s, _ in usdt:
        if b in hybrid.NORMAL_AVG:
            watch[b] = (b, s)
    HYBRID_STATE["watch"] = list(watch.values())
    HYBRID_STATE["watch_ts"] = now
    return HYBRID_STATE["watch"]


_OB_CACHE = {}


async def _orderbook_delta(http, sym):
    """Fetch top-10 bids/asks; return (buy_wall_usd, sell_wall_usd). Cached ~2s per symbol."""
    now = time.time()
    c = _OB_CACHE.get(sym)
    if c and now - c[0] < 2:
        return c[1], c[2]
    try:
        r = await http.get(f"{BINANCE_BASE}/api/v3/depth", params={"symbol": sym, "limit": 10}, timeout=5)
        d = r.json()
        bw = sum(float(p) * float(q) for p, q in d.get("bids", [])[:10])
        sw = sum(float(p) * float(q) for p, q in d.get("asks", [])[:10])
    except Exception:  # noqa: BLE001
        return 0.0, 0.0
    _OB_CACHE[sym] = (now, bw, sw)
    return bw, sw


async def _process_hybrid(http, now):
    cfg = BOT["config"]
    latest = STATE["latest"]
    watch = _hybrid_watchlist(latest, now)
    trades_1s, _ = compute_trades(1)
    params = {k: cfg.get(k) for k in ("min_power_1m", "max_power_1m", "burst_power")}
    rows = []
    for base, sym in watch:
        v = latest.get(sym)
        if not v:
            continue
        cell = trades_1s.get(sym)
        tc = cell["trades"] if cell else 0
        if tc > 0 and v.get("count24h"):
            tick_usd = tc * (v["quoteVol"] / v["count24h"])
            hybrid.update(base, tick_usd, cell["side"], now, count=tc)
        sig = hybrid.get_signal(base, v["quoteVol"], params, now)
        sig["symbol"] = sym
        sig["price"] = v["price"]
        rows.append(sig)
        # execution: hybrid signals -> Isolated 3x, gated by PWR + flow + real orderbook wall
        if cfg.get("hybrid_toggle") and cfg.get("enabled") and not BOT["stopped"] and sig["signal"]:
            if sym in BOT["positions"] or (now - BOT["lastTradeTs"].get(sym, 0)) < cfg["cooldownSec"]:
                continue
            if len(BOT["positions"]) >= cfg["maxOpenPositions"]:
                continue
            long = sig["signal"] == "LONG"
            px = v["price"]
            pwr = sig["power_1m"]
            flow = sig["buy"] if long else (100 - sig["buy"])
            # orderbook delta = wall in our direction / opposite wall
            bw, sw = await _orderbook_delta(http, sym)
            delta = (bw / sw if sw else 0.0) if long else (sw / bw if bw else 0.0)
            min_delta = cfg.get("minDeltaLong", 1.5) if long else cfg.get("minDeltaShort", 1.5)
            ok_pwr = pwr >= cfg.get("minPwr1m", 0.65)
            ok_flow = flow >= 75
            ok_delta = (not cfg.get("obDeltaFilter", True)) or (delta >= min_delta)
            trade = ok_pwr and ok_flow and ok_delta
            jlog("hybrid", symbol=sym, base=base, side=sig["signal"], power=pwr, buy=sig["buy"], delta=round(delta, 2),
                 decision="TRADE" if trade else "SKIP",
                 message=f"HYBRID {sig['signal']} {base} pwr1m {pwr} buy {sig['buy']}% delta {delta:.2f} -> {'TRADE' if trade else 'SKIP'}")
            if not trade:
                jlog("skip", symbol=sym, base=base,
                     message=f"SKIP {base} pwr1m {pwr} buy {sig['buy']}% delta {delta:.2f} - WEAK WALL")
                continue
            # trailing entry: tight initial SL (-trailInitialSlPct), no fixed TP (trailing manages upside)
            isl = cfg.get("trailInitialSlPct", 0.30)
            sl = px * (1 - isl / 100) if long else px * (1 + isl / 100)
            await _open_position(http, sym, px, now, side="long" if long else "short",
                                 tp_price=None, sl_price=sl, source="hybrid", leverage=3, is_cross=False)
    rows.sort(key=lambda r: (r["signal"] is None, -r["power_1m"]))
    HYBRID_STATE["rows"] = rows[:40]


async def _update_trailing(http, sym, pos, price):
    """Smart trailing exit: tight initial stop, secure to BE+ at trailSecure, then trail the
    SL up in trailStep increments (locking >= trailLock% of profit), and exit on a
    trailCallback% drop from peak. Works for long & short."""
    cfg = BOT["config"]
    e = pos["entryPrice"]
    if e <= 0:
        return
    long = pos.get("side", "long") == "long"
    profit_pct = ((price - e) / e * 100.0) if long else ((e - price) / e * 100.0)
    peak = pos.get("peakPct", 0.0)
    if profit_pct > peak:
        peak = profit_pct
        pos["peakPct"] = peak
    secure = cfg.get("trailSecure", 0.40)
    be = cfg.get("trailBE", 0.05)
    step = cfg.get("trailStep", 0.5)
    cb = cfg.get("trailCallback", 0.30)
    base_sl_off = pos.get("slOffPct")  # locked SL offset in % from entry (None until secured)

    new_off = hybrid.trail_stop_offset(peak, secure, be, step)
    stepped = False
    if new_off is not None and (base_sl_off is None or new_off > base_sl_off + 1e-9):
        first = base_sl_off is None
        pos["slOffPct"] = new_off
        base_sl_off = new_off
        stepped = True
        if first:
            jlog("trail", symbol=sym, base=pos["base"],
                 message=f"{pos['base']} +{peak:.2f}% -> SECURED BE+{be:.2f}%")
        else:
            jlog("trail", symbol=sym, base=pos["base"],
                 message=f"{pos['base']} +{peak:.2f}% -> TRAIL SL to +{new_off:.2f}%")

    # effective stop price
    if base_sl_off is not None:
        step_sl = e * (1 + base_sl_off / 100.0) if long else e * (1 - base_sl_off / 100.0)
        peak_price = e * (1 + peak / 100.0) if long else e * (1 - peak / 100.0)
        cb_sl = peak_price * (1 - cb / 100.0) if long else peak_price * (1 + cb / 100.0)
        stop = max(step_sl, cb_sl) if long else min(step_sl, cb_sl)
        pos["slPrice"] = stop
        hit = price <= stop if long else price >= stop
        if hit:
            await _close_position(http, sym, price, f"trail-exit +{peak:.2f}%")
            return
        # mirror the locked stop onto the exchange whenever it steps up (LIVE HL only)
        if stepped:
            await _hl_sync_stop(pos, sym, stop)
    else:
        # not yet secured -> initial tight stop
        stop = pos["slPrice"]
        hit = price <= stop if long else price >= stop
        if hit:
            await _close_position(http, sym, price, "stop-loss")


async def process_bot(http: httpx.AsyncClient, now: float):
    cfg = BOT["config"]
    latest = STATE["latest"]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if BOT["dailyDate"] != today:
        BOT["dailyDate"] = today
        BOT["dailyPnl"] = 0.0
        BOT["stopped"] = False
        jlog("daily_reset", date=today)

    # HARD per-trade max-loss cap — overrides autoExit and every other rule.
    # Runs every poll regardless of autoExit so a position can never bleed past it.
    hard_cap = cfg.get("maxLossPerTradeUsdt", 0) or 0
    if hard_cap > 0:
        for sym in list(BOT["positions"].keys()):
            v = latest.get(sym)
            if not v:
                continue
            pos = BOT["positions"][sym]
            sgn = 1 if pos.get("side", "long") == "long" else -1
            upnl = (v["price"] - pos["entryPrice"]) * pos["qty"] * sgn
            if upnl <= -hard_cap:
                await _close_position(http, sym, v["price"], "max-loss-cap")

    # STRADDLE fills: whichever stop level price hits first fills; the other is cancelled.
    if BOT["straddles"]:
        for sym in list(BOT["straddles"].keys()):
            s = BOT["straddles"][sym]
            v = latest.get(sym)
            if not v:
                continue
            price = v["price"]
            if now - s["createdTs"] > STRADDLE_EXPIRY:
                BOT["straddles"].pop(sym, None)
                jlog("straddle_cancel", symbol=sym, base=s["base"], reason="expired")
                continue
            # Filled legs use the SMART TRAILING TP system (same as Hybrid): no fixed TP,
            # just a tight initial stop; _update_trailing() then secures BE+ and trails the SL.
            isl = cfg.get("trailInitialSlPct", 0.30)
            if price >= s["longEntry"]:
                BOT["straddles"].pop(sym, None)
                sl = s["longEntry"] * (1 - isl / 100)
                jlog("straddle_fill", symbol=sym, base=s["base"], side="long", entry=s["longEntry"],
                     message="LONG leg filled — SHORT leg cancelled")
                await _open_position(http, sym, s["longEntry"], now, side="long", tp_price=None, sl_price=sl, source="straddle")
            elif price <= s["shortEntry"]:
                BOT["straddles"].pop(sym, None)
                sl = s["shortEntry"] * (1 + isl / 100)
                jlog("straddle_fill", symbol=sym, base=s["base"], side="short", entry=s["shortEntry"],
                     message="SHORT leg filled — LONG leg cancelled")
                await _open_position(http, sym, s["shortEntry"], now, side="short", tp_price=None, sl_price=sl, source="straddle")

    # 1) manage exits. If trailEnabled, use SMART TRAILING (dynamic SL that locks profit);
    #    else fixed TP/SL. Straddle/hybrid always managed regardless of the autoExit toggle.
    _auto = cfg.get("autoExit", True)
    _trail = cfg.get("trailEnabled", True)
    for sym in list(BOT["positions"].keys()):
        pos = BOT["positions"][sym]
        if not (_auto or pos.get("source") in ("straddle", "hybrid")):
            continue
        v = latest.get(sym)
        if not v:
            continue
        price = v["price"]
        if _trail and pos.get("tpPrice") is None:
            await _update_trailing(http, sym, pos, price)
            continue
        # fixed TP/SL fallback (used when trailing off or a fixed TP was set, e.g. straddle)
        if pos.get("side", "long") == "long":
            if pos.get("tpPrice") and price >= pos["tpPrice"]:
                await _close_position(http, sym, price, "take-profit")
            elif price <= pos["slPrice"]:
                await _close_position(http, sym, price, "stop-loss")
        else:
            if pos.get("tpPrice") and price <= pos["tpPrice"]:
                await _close_position(http, sym, price, "take-profit")
            elif price >= pos["slPrice"]:
                await _close_position(http, sym, price, "stop-loss")

    # Entries are driven ONLY by alert-history events pushed from the dashboard
    # (see handle_signal + POST /api/bot/signal). If no alerts are enabled/feeding,
    # the bot stays inactive. process_bot only handles daily reset + protective exits.

    # HYBRID POWER SYSTEM: feed per-second power metrics + fire its own LONG/SHORT entries.
    try:
        await _process_hybrid(http, now)
    except Exception as he:  # noqa: BLE001
        logger.error("hybrid error: %s", he)


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
    _maxv = cfg.get("maxVolumeUsd", 0) or 0
    _minv = cfg.get("minVolumeUsd", 0) or 0
    if price <= 0 or volume < _minv or (_maxv > 0 and volume > _maxv):
        # volume outside the configured band -> drop. Log (throttled, LIVE only) so operators
        # can see WHY a live signal produced no order instead of it silently vanishing.
        if cfg.get("enabled") and not cfg.get("dryRun") and price > 0 and (now - BOT.get("_volSkipTs", 0)) > 15:
            BOT["_volSkipTs"] = now
            jlog("skip", symbol=sym, base=sym[: -len(QUOTE)],
                 message=f"{sym} vol ${volume:,.0f} outside band [${_minv:,.0f} – {('$'+format(_maxv, ',.0f')) if _maxv else '∞'}] — no trade")
        return

    # STRADDLE MODE: a qualifying volume-spike alert arms a long/short breakout straddle
    # (instead of the streak-based directional entry).
    if cfg.get("straddleEnabled"):
        if sym in BOT["positions"] or sym in BOT["straddles"]:
            return
        if (now - BOT["lastTradeTs"].get(sym, 0)) < cfg["cooldownSec"]:
            return
        if (len(BOT["positions"]) + len(BOT["straddles"])) >= cfg["maxOpenPositions"]:
            return
        eo = cfg["straddleEntryPct"] / 100
        BOT["straddles"][sym] = {
            "symbol": sym, "base": sym[: -len(QUOTE)], "mark": price,
            "longEntry": price * (1 + eo), "shortEntry": price * (1 - eo),
            "createdTs": now,
        }
        jlog("straddle", symbol=sym, base=sym[: -len(QUOTE)], mark=price,
             longEntry=price * (1 + eo), shortEntry=price * (1 - eo),
             message=f"Straddle armed ±{cfg['straddleEntryPct']}% · Smart Trailing TP (init SL {cfg.get('trailInitialSlPct', 0.30)}%)")
        return

    st = BOT["signalStreaks"].get(sym)
    if st and st["side"] == side and (now - st["startTs"]) <= 1.5:
        st["count"] += 1
    else:
        st = {"side": side, "count": 1, "startTs": now}
        BOT["signalStreaks"][sym] = st
    if st["count"] >= cfg["streak"]:
        cd = now - BOT["lastTradeTs"].get(sym, 0) >= cfg["cooldownSec"]
        want = "long" if side == "buy" else "short"
        pos = BOT["positions"].get(sym)
        if pos is None:
            # flat: BUY opens LONG, SELL opens SHORT (perps)
            if cd and len(BOT["positions"]) < cfg["maxOpenPositions"]:
                await _open_position(http, sym, price, now, side=want)
        elif pos.get("side", "long") != want:
            # opposite signal on an open position -> close it
            await _close_position(http, sym, price, "reverse-signal")
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
                quote = next((q for q in QUOTES if sym.endswith(q)), None)
                if quote is None:
                    continue
                try:
                    last_id = int(x["lastId"])
                except (KeyError, ValueError, TypeError):
                    continue
                if last_id < 0:
                    continue
                latest[sym] = {
                    "base": sym[: -len(quote)],
                    "quote": quote,
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


def _compute_rows(timeframe: str, search: str, sort: str, quote: str, limit: int):
    """Shared row builder for /tokens and /tokens/delta."""
    trades, approx = compute_trades(TF_SECONDS[timeframe])
    latest = STATE["latest"]
    q = search.strip().upper()
    quote = quote.strip().upper()
    rows = []
    total_trades = 0
    total_pairs = 0
    for sym, v in latest.items():
        if quote != "ALL" and v.get("quote") != quote:
            continue
        total_pairs += 1
        if q and q not in sym:
            continue
        cell = trades.get(sym) or {"trades": 0, "side": "buy"}
        tc = cell["trades"]
        total_trades += tc
        rows.append({
            "symbol": sym,
            "base": v["base"],
            "quote": v.get("quote", QUOTE),
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
    meta = {
        "approx": approx, "quote": quote, "totalPairs": total_pairs,
        "totalTrades": total_trades, "matched": matched,
        "historyDepthSec": round(_history_depth()),
    }
    return rows, meta


@api_router.get("/tokens")
async def get_tokens(
    timeframe: str = Query("1s"),
    search: str = Query(""),
    sort: str = Query("trades_desc"),
    quote: str = Query("USDT"),
    limit: int = Query(1000, le=2000),
):
    if timeframe not in TF_SECONDS:
        raise HTTPException(400, f"invalid timeframe. valid: {list(TF_SECONDS)}")
    if not STATE["latest"]:
        return {"timeframe": timeframe, "approx": True, "total": 0, "tokens": [], "warming": True}

    rows, meta = _compute_rows(timeframe, search, sort, quote, limit)
    return {
        "timeframe": timeframe,
        "approx": meta["approx"],
        "quote": meta["quote"],
        "quotes": QUOTES,
        "total": meta["matched"],
        "totalPairs": meta["totalPairs"],
        "totalTrades": meta["totalTrades"],
        "historyDepthSec": meta["historyDepthSec"],
        "tokens": rows,
    }


# Delta stream: sends the full ordered symbol list every tick (cheap) but only the
# ROWS whose values actually changed since this client's last poll. Rows are encoded
# as positional arrays [price, change, quoteVol, trades, side(1=buy/0=sell), trades24h]
# which, combined with delta + gzip, cuts bandwidth dramatically vs full snapshots.
_TOKEN_SESSIONS = {}  # sid -> {"ts", "sig", "rows": {sym: [..]}}


@api_router.get("/tokens/delta")
async def get_tokens_delta(
    sid: str = Query(...),
    timeframe: str = Query("1s"),
    search: str = Query(""),
    sort: str = Query("trades_desc"),
    quote: str = Query("USDT"),
    limit: int = Query(1000, le=2000),
):
    if timeframe not in TF_SECONDS:
        raise HTTPException(400, f"invalid timeframe. valid: {list(TF_SECONDS)}")
    now = time.time()
    # prune stale sessions
    for k in [k for k, v in _TOKEN_SESSIONS.items() if now - v["ts"] > 120]:
        _TOKEN_SESSIONS.pop(k, None)
    if not STATE["latest"]:
        return {"full": True, "warming": True, "timeframe": timeframe,
                "order": [], "rows": {}, "totalPairs": 0, "totalTrades": 0, "approx": True}

    rows, meta = _compute_rows(timeframe, search, sort, quote, limit)
    sig = f"{timeframe}|{meta['quote']}|{search.strip().upper()}|{sort}|{limit}"
    order = [r["symbol"] for r in rows]
    current = {
        r["symbol"]: [r["price"], r["change"], r["quoteVol"], r["trades"],
                      1 if r["side"] == "buy" else 0, r["trades24h"]]
        for r in rows
    }
    base = {
        "timeframe": timeframe, "approx": meta["approx"], "quote": meta["quote"],
        "quotes": QUOTES, "order": order, "totalPairs": meta["totalPairs"],
        "totalTrades": meta["totalTrades"], "matched": meta["matched"],
        "historyDepthSec": meta["historyDepthSec"],
    }
    sess = _TOKEN_SESSIONS.get(sid)
    if sess is None or sess["sig"] != sig:
        _TOKEN_SESSIONS[sid] = {"ts": now, "sig": sig, "rows": current}
        return {**base, "full": True, "rows": current}
    prev = sess["rows"]
    changed = {s: vals for s, vals in current.items() if prev.get(s) != vals}
    removed = [s for s in prev if s not in current]
    sess["ts"] = now
    sess["rows"] = current
    return {**base, "full": False, "changed": changed, "removed": removed}


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
    webhookEnabled: Optional[bool] = None
    webhookUrl: Optional[str] = None
    webhookBuyMsg: Optional[str] = None
    webhookSellMsg: Optional[str] = None
    exchange: Optional[str] = None
    hlTestnet: Optional[bool] = None
    maxLossPerTradeUsdt: Optional[float] = None
    straddleEnabled: Optional[bool] = None
    straddleEntryPct: Optional[float] = None
    straddleTpPct: Optional[float] = None
    straddleSlPct: Optional[float] = None
    hlLeverage: Optional[int] = None
    hlCrossMargin: Optional[bool] = None
    hlNativeTpsl: Optional[bool] = None
    hlSlippagePct: Optional[float] = None
    hybrid_toggle: Optional[bool] = None
    sl_percent: Optional[float] = None
    tp_percent: Optional[float] = None
    min_power_1m: Optional[float] = None
    max_power_1m: Optional[float] = None
    burst_power: Optional[float] = None
    obDeltaFilter: Optional[bool] = None
    minDeltaLong: Optional[float] = None
    minDeltaShort: Optional[float] = None
    minPwr1m: Optional[float] = None
    trailEnabled: Optional[bool] = None
    trailInitialSlPct: Optional[float] = None
    trailSecure: Optional[float] = None
    trailBE: Optional[float] = None
    trailStep: Optional[float] = None
    trailLock: Optional[float] = None
    trailCallback: Optional[float] = None


def _bot_status():
    cfg = BOT["config"]
    latest = STATE["latest"]
    positions = []
    unrealized = 0.0
    for sym, pos in BOT["positions"].items():
        price = latest.get(sym, {}).get("price", pos["entryPrice"])
        sgn = 1 if pos.get("side", "long") == "long" else -1
        upnl = (price - pos["entryPrice"]) * pos["qty"] * sgn
        unrealized += upnl
        positions.append({
            "symbol": sym, "base": pos["base"], "side": pos.get("side", "long"),
            "entryPrice": pos["entryPrice"],
            "currentPrice": price, "qty": pos["qty"], "quoteSpent": pos["quoteSpent"],
            "tpPrice": pos["tpPrice"], "slPrice": pos["slPrice"], "source": pos.get("source", "signal"),
            "uPnl": upnl, "openedTs": pos["openedTs"], "mode": "SIM" if pos.get("dryRun") else "LIVE",
        })
    straddles = [
        {"symbol": s["symbol"], "base": s["base"], "mark": s["mark"],
         "longEntry": s["longEntry"], "shortEntry": s["shortEntry"],
         "currentPrice": latest.get(sym2, {}).get("price", s["mark"]), "createdTs": s["createdTs"]}
        for sym2, s in BOT["straddles"].items()
    ]
    now = time.time()
    alerts_feeding = (now - BOT["lastSignalTs"]) < 6 and BOT["lastSignalTs"] > 0
    return {
        "config": cfg,
        "stopped": BOT["stopped"],
        "dailyPnl": BOT["dailyPnl"],
        "dailyDate": BOT["dailyDate"],
        "unrealizedPnl": unrealized,
        "openPositions": positions,
        "pendingStraddles": straddles,
        "journal": list(BOT["journal"])[:100],
        "keysConfigured": bool(BINANCE_API_KEY and BINANCE_API_SECRET),
        "hlKeyConfigured": bool(os.environ.get("HYPERLIQUID_PRIVATE_KEY")),
        "hlAddress": os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", ""),
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
        data["slPct"] = max(0.00000001, min(1.0, data["slPct"]))
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
    if "exchange" in data and data["exchange"] not in ("binance", "hyperliquid"):
        data.pop("exchange")
    if "maxVolumeUsd" in data:
        data["maxVolumeUsd"] = max(0.0, float(data["maxVolumeUsd"]))
    if "maxLossPerTradeUsdt" in data:
        data["maxLossPerTradeUsdt"] = max(0.0, float(data["maxLossPerTradeUsdt"]))
    if "straddleEntryPct" in data:
        data["straddleEntryPct"] = max(0.000001, min(5.0, float(data["straddleEntryPct"])))
    if "straddleTpPct" in data:
        data["straddleTpPct"] = max(0.01, min(10.0, float(data["straddleTpPct"])))
    if "straddleSlPct" in data:
        data["straddleSlPct"] = max(0.0001, min(5.0, float(data["straddleSlPct"])))
    if "hlLeverage" in data:
        data["hlLeverage"] = max(1, min(50, int(data["hlLeverage"])))
    if "hlSlippagePct" in data:
        data["hlSlippagePct"] = max(0.0, min(5.0, float(data["hlSlippagePct"])))
    for k, lo, hi in (("sl_percent", 0.3, 1.5), ("tp_percent", 0.8, 4.0),
                      ("min_power_1m", 0.4, 1.0), ("max_power_1m", 1.2, 2.5),
                      ("burst_power", 0.03, 0.15)):
        if k in data:
            data[k] = max(lo, min(hi, float(data[k])))
    for k, lo, hi in (("minDeltaLong", 0.1, 100.0), ("minDeltaShort", 0.1, 100.0), ("minPwr1m", 0.0, 5.0),
                      ("trailInitialSlPct", 0.05, 5.0), ("trailSecure", 0.05, 5.0), ("trailBE", 0.0, 2.0),
                      ("trailStep", 0.05, 5.0), ("trailLock", 0.0, 100.0), ("trailCallback", 0.05, 5.0)):
        if k in data:
            data[k] = max(lo, min(hi, float(data[k])))
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
    straddle_n = len(BOT["straddles"])
    BOT["straddles"].clear()
    jlog("kill_switch", message=f"Manually closed {closed} position(s) · cancelled {straddle_n} straddle(s)")
    return _bot_status()


@api_router.post("/bot/sync-stops")
async def bot_sync_stops():
    """Re-arm exchange-side protection: for every LIVE Hyperliquid position, cancel any
    stale native stops and place a single fresh reduce-only SL trigger at the position's
    current stop price. Guarantees exactly one native SL per open position."""
    cfg = BOT["config"]
    synced, skipped = 0, 0
    if cfg["dryRun"] or cfg.get("exchange") != "hyperliquid":
        jlog("kill_switch", message="Sync stops skipped — bot is in SIM or not on Hyperliquid")
        return {**_bot_status(), "synced": 0, "skipped": 0}
    for sym in list(BOT["positions"].keys()):
        pos = BOT["positions"][sym]
        if pos.get("dryRun"):
            skipped += 1
            continue
        stop = pos.get("slPrice")
        if not stop or stop <= 0:
            skipped += 1
            continue
        # _hl_sync_stop cancels stale triggers (cancel-replace) then places one SL at `stop`
        await _hl_sync_stop(pos, sym, stop)
        synced += 1
    jlog("kill_switch", message=f"Synced native stops on {synced} position(s) · skipped {skipped}", live=True)
    return {**_bot_status(), "synced": synced, "skipped": skipped}



@api_router.post("/bot/reset-daily")
async def bot_reset_daily():
    BOT["dailyPnl"] = 0.0
    BOT["stopped"] = False
    jlog("daily_reset", message="Manual daily reset")
    return _bot_status()


@api_router.post("/bot/test-webhook")
async def bot_test_webhook():
    price = STATE["latest"].get("BTCUSDT", {}).get("price", 0) or 0
    await send_webhook("buy", "BTCUSDT", price)
    return _bot_status()


@api_router.get("/hyperliquid/account")
async def hyperliquid_account():
    """Read-only Hyperliquid account state for the configured address (no private key needed)."""
    addr = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "")
    net = os.environ.get("HYPERLIQUID_NETWORK", "mainnet").lower()
    if not addr:
        raise HTTPException(400, "HYPERLIQUID_ACCOUNT_ADDRESS not configured")
    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        base = constants.MAINNET_API_URL if net == "mainnet" else constants.TESTNET_API_URL
        info = Info(base, skip_ws=True)
        state = await asyncio.to_thread(info.user_state, addr)
        ms = state.get("marginSummary", {})
        positions = [
            {"coin": p["position"]["coin"], "szi": p["position"]["szi"],
             "entryPx": p["position"].get("entryPx"), "unrealizedPnl": p["position"].get("unrealizedPnl")}
            for p in state.get("assetPositions", [])
        ]
        return {
            "address": addr, "network": net,
            "accountValue": float(ms.get("accountValue", 0) or 0),
            "withdrawable": float(state.get("withdrawable", 0) or 0),
            "positions": positions,
            "keyConfigured": bool(os.environ.get("HYPERLIQUID_PRIVATE_KEY")),
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Hyperliquid info failed: {e}")


@api_router.get("/hybrid/orderbook")
async def hybrid_orderbook(symbol: str = Query("BTCUSDT")):
    bw, sw = await _orderbook_delta(app.state.http, symbol)
    return {"symbol": symbol, "buyWall": bw, "sellWall": sw,
            "deltaLong": (bw / sw if sw else 0.0), "deltaShort": (sw / bw if bw else 0.0)}


@api_router.get("/hybrid")
async def hybrid_status():
    cfg = BOT["config"]
    return {
        "config": {k: cfg.get(k) for k in ("hybrid_toggle", "sl_percent", "tp_percent",
                                           "min_power_1m", "max_power_1m", "burst_power",
                                           "obDeltaFilter", "minDeltaLong", "minDeltaShort", "minPwr1m",
                                           "trailEnabled", "trailInitialSlPct", "trailSecure", "trailBE",
                                           "trailStep", "trailLock", "trailCallback")},
        "rows": HYBRID_STATE["rows"],
        "normalAvg": hybrid.NORMAL_AVG,
    }


@api_router.get("/hyperliquid/resolve")
async def hyperliquid_resolve(symbols: str = Query("BTCUSDT,ETHUSDT,SOLUSDT")):
    """Show exactly which Hyperliquid coin each Binance symbol maps to (base only, no USDT).
    Returns None for coins not listed on Hyperliquid perps."""
    await _ensure_hl_ready()
    out = {}
    for s in [x.strip() for x in symbols.split(",") if x.strip()]:
        out[s] = hlconv.convert_binance_to_hyperliquid(s)
    return {"coinsLoaded": len(hlconv.hl_coins()), "resolved": out}


@api_router.get("/hyperliquid/diagnose")
async def hyperliquid_diagnose():
    """Diagnose the 'User or API Wallet' error: derive the wallet from the secret, compare to
    the configured main account, and report which address actually holds a funded HL account."""
    import eth_account
    key = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "") or os.environ.get("HYPERLIQUID_SECRET_KEY", "")
    main = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "") or os.environ.get("HYPERLIQUID_MAIN_WALLET", "")
    net = "testnet" if BOT["config"].get("hlTestnet") else "mainnet"
    out = {"network": net, "mainWallet": main, "secretConfigured": bool(key)}
    if not key:
        return {**out, "error": "no secret configured"}
    derived = eth_account.Account.from_key(key).address
    out["apiWalletDerivedFromSecret"] = derived
    out["mainEqualsApiWallet"] = derived.lower() == (main or "").lower()
    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        base = constants.MAINNET_API_URL if net == "mainnet" else constants.TESTNET_API_URL
        info = await asyncio.to_thread(Info, base, True)
        for label, a in [("mainWallet", main), ("apiWallet", derived)]:
            try:
                st = await asyncio.to_thread(info.user_state, a)
                out[f"{label}_accountValue"] = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
            except Exception as e:  # noqa: BLE001
                out[f"{label}_accountValue"] = f"error: {str(e)[:80]}"
    except Exception as e:  # noqa: BLE001
        out["infoError"] = str(e)[:120]
    if out["mainEqualsApiWallet"]:
        out["hint"] = ("account_address equals the wallet derived from your secret. If 0x8117… is an "
                       "API/agent wallet, set HYPERLIQUID_ACCOUNT_ADDRESS to your MAIN funded wallet "
                       "(the one that approved this agent). If 0x8117… IS your main wallet, just deposit USDC into it.")
    else:
        out["hint"] = "Main wallet differs from the API wallet (correct agent setup). Ensure the agent is approved and the main wallet is funded."
    return out



# ----------------------------- Market overview (external APIs) -----------------------------
_MO_CACHE = {"ts": 0.0, "data": None}


async def _coinbase(http, sym):
    r = await http.get(f"https://api.coinbase.com/v2/prices/{sym}/spot", timeout=8)
    return float(r.json()["data"]["amount"])


async def market_overview(http: httpx.AsyncClient):
    sources = {}
    out = {"btc": None, "eth": None, "sol": None, "totalMarketCap": None,
           "totalVolume24h": None, "btcDominance": None, "ethGasGwei": None,
           "btcTxCount24h": None, "btcHashRate": None, "btcBlockHeight": None,
           "activeCryptos": None}

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
            url = "https://api.blockchain.info/stats?cors=true"
            if BLOCKCHAIN_API_KEY:
                url += f"&key={BLOCKCHAIN_API_KEY}"
            r = await http.get(url, timeout=8)
            d = r.json()
            out["btcTxCount24h"] = int(d.get("n_tx", 0))
            out["btcHashRate"] = float(d.get("hash_rate", 0))
            out["btcBlockHeight"] = int(d.get("n_blocks_total", 0)) or None
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
                f"https://api.etherscan.io/v2/api?chainid=1&module=gastracker&action=gasoracle&apikey={ETHERSCAN_API_KEY}",
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
