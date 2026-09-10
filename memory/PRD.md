# TradePulse — Binance Token Scanner

## Original Problem Statement
Scan all ~669 Binance USDT tokens across 15 timeframes (1s, 5s, 15s, 30s, 1m, 5m, 15m, 30m, 1h, 2h, 4h, 1d, 4d, 7d, 10d). Dashboard always shows how many trades have been made per token. White background. Integrate Binance, Etherscan, Blockchain.com, CoinMarketCap, CoinGecko, Coinbase APIs.

## User Choices
- Metric per token: number of trades only
- Scan all USDT trading pairs (~671 live)
- Auto-refresh every 1 second
- Show all 15 timeframes (approximate where history insufficient)
- White background

## Architecture
- **Backend** (FastAPI, `/app/backend/server.py`): async background poller hits `data-api.binance.vision /api/v3/ticker/24hr` every ~1s (api.binance.com is geo-blocked from this server). Each symbol's monotonic `lastId` is snapshotted into fine (1s cadence, ~6min) + coarse (5min cadence, ~11d) rolling histories. Trade count for any window = `lastId_now − lastId_(now−window)` → **exact**. 1d uses the exact 24h `count`. Windows lacking history scale from 24h count (`approx=true`) and become exact as history accumulates.
- **Frontend** (React, `/app/frontend/src/pages/Scanner.jsx`): polls `/api/tokens?timeframe=` every 1s, renders a virtualized (@tanstack/react-virtual) high-density table of all pairs with tick-flash animation. Swiss/Bloomberg light theme, JetBrains Mono tabular numbers.
- **External APIs**: Coinbase (BTC/ETH/SOL spot), Blockchain.com (BTC tx/hashrate), CoinGecko (global mcap/dominance — needs key to avoid 429), CoinMarketCap (optional key, mcap fallback), Etherscan (optional key, ETH gas).

## Implemented (2026-06)
- Real-time trade-count engine across all 15 timeframes for ~671 USDT pairs (exact via lastId diff)
- `/api/tokens` (timeframe, search, sort, limit), `/api/token/{symbol}`, `/api/engine/status`, `/api/timeframes`, `/api/market-overview`
- Virtualized dashboard: market-overview strip, 15 timeframe pills, sortable/searchable table, live status, 1s auto-refresh, footer aggregates
- **Trade Alerts** (client-side): thresholds 50/100/200/300/400/500/600/700/800/900/1000/1300/1500/2000; row highlight + bell flag, toast + audio beep on new crossings, "Only alerts" filter, mute toggle, live alert count in header/footer
- Tested: 22/23 backend (iter1), 100% frontend alerts (iter2)

## Known Gaps / Notes
- CoinGecko rate-limits (429) without an API key → Total Mkt Cap/Dominance/Active Coins show "—". Add COINGECKO_API_KEY or COINMARKETCAP_API_KEY.
- Etherscan/CoinMarketCap tiles need keys (graceful "—" without).
- Long timeframes (4d/7d/10d) show `approx` until enough runtime history accumulates.

## Implemented (2026-08) — Session 2
- **Delta token stream** (`GET /api/tokens/delta`): sends full ordered symbol list every tick but only the rows that changed (positional arrays `[price, change, quoteVol, trades, sideNum, trades24h]`). Keyed by `sid` + query signature; param change → full refresh. Cuts bandwidth on top of gzip. Frontend (`Scanner.jsx`) rebuilds rows from a persistent `rowMapRef`. Classic `GET /api/tokens` kept for back-compat.
- **Star-mark system**: per-row star button + `starred-only-toggle`, persisted in `localStorage`. When ≥1 token is starred, ONLY starred tokens feed the Auto-Trade Bot (`botSignals` filtered before POST /api/bot/signal).
- **Hard max-loss-per-trade cap** (`maxLossPerTradeUsdt`, default 0.0002 USDT): `process_bot` force-closes any position the instant unrealized loss hits the cap — overrides TP/SL, auto-exit and every other rule (runs even when autoExit is off). Editable in BotPanel (`bot-max-loss`). Journal reason = `max-loss-cap`.
- **Market-overview enrichment**: Etherscan key added → ETH gas (migrated to Etherscan **V2** `chainid=1` endpoint); Blockchain.com key added → new **BTC Block height** tile (`mo-block`).
- Verified: 8/8 backend pytest + full frontend flow (iteration_11.json), 100%.

## Implemented (2026-09) — Session 2b
- **Bot low volume bands**: min/max volume-band dropdowns now include sub-million bands `$50K / $100K / $300K / $600K / $900K / $1M` in addition to $5M–$900M (`BOT_VOL` in `BotPanel.jsx`, `fmtM` handles <1M as K, option values `Math.round(m*1e6)`).
- **Alert-history trade volume**: every logged alert now records `tradeVol` = estimated USD volume of the counted trades (`trades × quoteVol/trades24h`), shown as `≈$X · N tr` beside the 24h volume and added as a "Trade Vol" column in the PDF export (`Scanner.jsx`). Verified live via screenshot.
- **Alert-history Buy/Sell running totals**: two summary sections at the top of the Alert History panel — Buying·total and Selling·total — each showing Σ trades + Σ traded USD across all logged alerts, plus a net-pressure bar (`Net → BUYING/SELLING %`). Accumulates over time so dominant side/direction is visible at a glance (`alertTotals` memo in `Scanner.jsx`).
- **Alert-history per-token totals ("By token" view)**: `Log`/`By token` toggle in the panel; the By-token view keeps each token's bought vs sold totals separately (trades + USD) with a per-token net bar + BUYING/SELLING verdict, sorted by total traded volume (`tokenTotals` memo, `historyView` state). Verified live via screenshot (ETH 212/0→BUYING, BTC 60/91→SELLING).

## Implemented (2026-09) — Session 2c: STRADDLE SYSTEM
- **Straddle System** in the Auto-Trade Bot: on a volume-spike alert, arms a LONG stop-entry at `+straddleEntryPct%` and a SHORT stop-entry at `−straddleEntryPct%` of the mark. Whichever price hits first fills and opens a position; the opposite leg is cancelled (OCO). Each filled leg gets its own TP (`straddleTpPct`) / SL (`straddleSlPct`). Ranges: entry 0.000001–5%, TP 0.01–10%, SL 0.0001–5%. Un-filled straddles auto-cancel after `STRADDLE_EXPIRY`=600s.
- Added full **short-side** support: `_open_position(side, tp_price, sl_price, source)`, side-aware PnL in `_close_position` (cover), side-aware TP/SL + hard max-loss in `process_bot`, `_hl_market_open(is_buy)`. Shorts run in Sim + Hyperliquid only (Binance spot logs "long-only, short leg skipped").
- Backend: `BOT['straddles']`, `handle_signal` straddle-arm branch, straddle-fill loop in `process_bot`, `pendingStraddles` + position `side`/`source` in `_bot_status`, clamps + config fields, `close-all` clears straddles.
- Frontend `BotPanel.jsx`: Straddle section (toggle + Entry±/TP/SL), position L/S side badges, "Armed straddles" block, straddle/fill/cover journal lines.
- Verified: iteration_12.json — 7/7 backend pytest + full frontend lifecycle, 100%, no bugs. Safe defaults restored.
- Note (backlog): pending straddles occupy `maxOpenPositions` slots until filled/expired; consider a separate `maxPendingStraddles` cap.

## Implemented (2026-09) — Session 2d: Hyperliquid PERPS live trading
- **Hyperliquid keys added** to backend/.env (HYPERLIQUID_ACCOUNT_ADDRESS `0x8117…1856`, HYPERLIQUID_PRIVATE_KEY, mainnet). `/api/hyperliquid/account` authenticates OK. NOTE: account balance is $0 USDC — must deposit before live orders fill (HL ~$10 min notional).
- **PERPS confirmed** (coin names via `Info.meta()` perp universe, not spot pairs).
- **`hyperliquid_converter.py`** (new): `load_hl_meta` (coin→szDecimals + maxLeverage), `convert_binance_to_hyperliquid` (filters coins not on HL, maps 1000X→kX), `round_size`/`round_price` (fixes `float_to_wire` — size→szDecimals, price→5 sig-fig & ≤6-szDecimals dp), `get_rounded_size_and_price`. 233 coins loaded; unlisted (ARKM/DIA/RED/OPEN/XAUT/NEIRO/DEXE) skipped + logged once.
- **Long/short entries**: BUY→LONG, SELL→SHORT (perps), market orders; opposite signal on an open position closes it (reverse-signal). Verified SIM (sell BTC→short, buy SOL→long).
- **Leverage + margin**: config `hlLeverage` (1–40, clamped to each coin's max) + `hlCrossMargin` (default Cross). Applied per-coin via `exchange.update_leverage` before each order. UI controls in BotPanel (Hyperliquid → Leverage input + Cross/Isolated toggle). User sets leverage manually.
- Verified: converter unit-tested, backend starts clean, SIM long/short works, UI renders. Live execution pending USDC deposit.

## Bug RCA (2026-09) — "User or API Wallet 0x8117… does not exist"
- **Not a key swap.** `_get_hl_exchange` init is correct (signer=private key, account_address=main). Verified via new `/api/hyperliquid/diagnose`: the wallet derived from HYPERLIQUID_PRIVATE_KEY == HYPERLIQUID_ACCOUNT_ADDRESS == 0x8117…1856, and that address has **no Hyperliquid account (accountValue 0.0, "does not exist")**.
- **Root cause**: the configured wallet has no funded HL account. Either 0x8117 is an API/agent wallet whose real MAIN funded wallet address is different & missing, or 0x8117 is the user's main wallet that was never funded.
- **Fixes** (verified iteration_13.json, backend 100%): (1) `/api/hyperliquid/diagnose` endpoint with actionable hint; (2) HL-init debug log (Main vs derived, net); (3) env fallbacks HYPERLIQUID_SECRET_KEY / HYPERLIQUID_MAIN_WALLET; (4) mainnet enforced; (5) graceful journal 'error' on live HL failure (bot never crashes); (6) throttled 'skip' journal log when a live signal is dropped by the volume band (observability).
- **User action required**: fund 0x8117 on Hyperliquid, OR set HYPERLIQUID_ACCOUNT_ADDRESS to the correct funded MAIN wallet if 0x8117 is an agent key. Rotate the key (shared in chat).

## Bug fix (2026-09) — Symbol format + KeyError crash
- Report: bot appeared to send Binance pairs (CRVUSDT) to Hyperliquid. Reality: base was already extracted; the journal LABEL showed the pair, and the actual error was the unchanged unfunded-wallet error. Fixes (verified iteration_14.json, 100%):
  - `hyperliquid_converter.convert_binance_to_hyperliquid` now strips trailing quote assets (USDT/USDC/BUSD/FDUSD/TUSD/USD) so a full pair maps to the base; added public `strip_quote`.
  - New `GET /api/hyperliquid/resolve?symbols=` shows the exact HL coin per Binance symbol (BTCUSDT→BTC … 1000PEPEUSDT→KPEPE, unlisted→null).
  - Fixed a real `KeyError('UNIUSDT')` crash: `_open_position` now derives base safely (`STATE.latest.get(sym).base or strip_quote(sym)`).
  - Live HL error journal now shows the base coin ('CRV') + "HL sent coin 'CRV'" instead of the Binance pair.
- Still blocked by the unfunded/incorrect wallet — real fills need the correct funded MAIN wallet.

## Feature/Fix (2026-09) — Straddle TP/SL now enforced + native HL trigger orders
- Report: straddle opened positions on Hyperliquid but TP (target) / SL never closed them. Root cause: TP/SL monitor was gated by the global `autoExit` toggle, so straddle positions with autoExit OFF were never exited.
- Fix (verified iteration_15.json, 100%):
  - `process_bot` TP/SL loop now ALWAYS runs for `source=='straddle'` positions regardless of `autoExit` (`if not (_auto or source=='straddle'): continue`). Closes via HL market-close (live) / SIM. Non-straddle positions still obey `autoExit` (regression confirmed).
  - LIVE entries now also place NATIVE reduce-only TP/SL **trigger orders** on Hyperliquid (`order(order_type={'trigger':{triggerPx,isMarket:True,tpsl}}, reduce_only=True)`) via `_hl_place_tpsl`, so the exchange itself closes at target/stop. `_hl_cancel_coin` clears orphaned triggers on close/before re-entry. New config `hlNativeTpsl` (default true).
  - Verified in SIM: DOGEUSDT straddle SHORT closed via `cover take-profit` with autoExit OFF. Native trigger orders can't be exercised until the HL wallet is funded ($0).

## Feature (2026-09) — Configurable entry price buffer (slippage)
- New `hlSlippagePct` config (percent, default 0.0001, clamped 0–5%) passed as `slippage` to Hyperliquid `market_open`, so the market order is priced aggressively enough to fill within milliseconds. Adjustable box in BotPanel → Hyperliquid ("Entry buffer %", data-testid `bot-hl-slippage`). Verified: config round-trips + clamps (99→5.0); UI box renders. Applies to LIVE entries (exits keep a safe default so they always fill).

## Feature (2026-09) — HYBRID POWER SYSTEM
- New module `backend/hybrid_power.py`: 300s per-coin trade buffer, `NORMAL_AVG={BTC:800,ETH:600,HYPE:200,PURR:80,JEFF:60}`, `update(coin,amount_usd,side,ts,count)`, `get_signal(coin,vol_24h,params)` → power_1m/power_1s/power_5s/avg_1m/buy_1m/buy_1s + LONG/SHORT/None per spec thresholds.
- Fed each second in `_process_hybrid` from scanner deltas (estimated per-second USD, dominant side, count) for a top-40-by-vol + NORMAL_AVG watchlist.
- Config (clamped): `hybrid_toggle` (default ON), `sl_percent` 0.3–1.5/0.8, `tp_percent` 0.8–4.0/2.0, `min_power_1m` 0.4–1.0/0.6, `max_power_1m` 1.2–2.5/1.8, `burst_power` 0.03–0.15/0.06. `GET /api/hybrid` returns config+rows+normalAvg.
- Execution (only when toggle ON + bot enabled): opens **Isolated 3x** via `_open_position(source='hybrid', leverage=3, is_cross=False)` with slider SL/TP; hybrid positions get always-on TP/SL enforcement.
- Dashboard: `HybridPanel.jsx` (via "Hybrid Power" header button) — header, toggle, 5 live sliders, table Coin|pwr1m|pwr1s|pwr5s|avg|buy%|SIGNAL (top 40, 1s refresh).
- Verified: iteration_16.json 13/13 backend + UI screenshot. Organic signals need a real volume burst (rare); live execution also pending HL wallet funding.

## Known Gaps / Notes (Session 2)

## Feature/Fix (2026-09) — Whale Orderbook-Delta Filter + Smart Trailing TP
- Fixed fake-trade losses on hybrid signals by adding a real orderbook confirmation + trailing exits.
- **Orderbook Delta filter** (`_orderbook_delta` via data-api.binance.vision /api/v3/depth top-10): buy_wall=Σbid$, sell_wall=Σask$; LONG needs delta_long=buy/sell ≥ `minDeltaLong`(1.5), SHORT needs delta_short=sell/buy ≥ `minDeltaShort`(1.5). Hybrid entry gated by `minPwr1m`(0.65) + buy/sell%≥75 + delta. Config `obDeltaFilter`(ON). Journal: `HYBRID LONG TRX pwr1m .. buy ..% delta 2.1 -> TRADE|SKIP`; SKIP also logs `SKIP <coin> .. - WEAK WALL` and does NOT enter. Debug: `GET /api/hybrid/orderbook?symbol=`.
- **Smart Trailing TP** (replaces fixed TP; `_update_trailing` + `hybrid_power.trail_stop_offset`): initial SL −`trailInitialSlPct`(0.30%); at peak≥`trailSecure`(0.40%) → SL=entry+`trailBE`(0.05%) ("SECURED BE+"); then trail up in `trailStep`(0.5%) increments (offset=max(be, peak−step+be)) → +0.90%→+0.45%, +1.40%→+0.95%; exit on `trailCallback`(0.30%) drop from peak → reason `trail-exit +X%`. Config `trailEnabled`(ON), `trailLock`(50, informational). Applies to all bot positions where tpPrice is None.
- UI: HybridPanel gains "Enable Orderbook Delta Filter" checkbox + Min Delta LONG/SHORT + Min PWR1m, and a Smart Trailing TP section (Initial SL/Secure/BE/Step/Lock/Callback); fixed TP slider removed.
- Verified: iteration_17.json 15/15 backend (orderbook endpoint, trailing math, config+clamps, no-crash) + UI screenshot. Organic hybrid signals are burst-dependent; live fills still pending HL wallet funding.

- User-supplied Binance/Hyperliquid values from last prompt were partial (Binance single key w/o secret; HL value was an address, not a private key). Existing working Binance key+secret in `.env` left untouched; live trading still requires deploy (api.binance.com 451 on preview) + funded HL collateral.
- `_TOKEN_SESSIONS` pruned at 120s; consider LRU cap if many idle tabs (low priority).

## Backlog
- P1: Per-token detail drawer with 15-timeframe breakdown + mini sparkline
- P2: Hyperliquid size precision rounding (szDecimals) to avoid order rejections
- P2: Trade Analytics (win-rate, avg P&L, equity curve) in bot panel
- P2: Telegram alerts for bot entries/exits/daily-limit halts
- P3: Migrate 1s REST polling to WebSockets; partial exits + shorting for WunderTrading
