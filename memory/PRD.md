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
- **Alert-history Buy/Sell running totals**: two summary sections at the top of the Alert History panel — Buying·total and Selling·total — each showing Σ trades + Σ traded USD across all logged alerts, plus a net-pressure bar (`Net → BUYING/SELLING %`). Accumulates over time so dominant side/direction is visible at a glance (`alertTotals` memo in `Scanner.jsx`). Verified live: Buy 58tr/$9.30K vs Sell 392tr/$82.41K → Net SELLING 90%.

## Known Gaps / Notes (Session 2)
- User-supplied Binance/Hyperliquid values from last prompt were partial (Binance single key w/o secret; HL value was an address, not a private key). Existing working Binance key+secret in `.env` left untouched; live trading still requires deploy (api.binance.com 451 on preview) + funded HL collateral.
- `_TOKEN_SESSIONS` pruned at 120s; consider LRU cap if many idle tabs (low priority).

## Backlog
- P1: Per-token detail drawer with 15-timeframe breakdown + mini sparkline
- P2: Hyperliquid size precision rounding (szDecimals) to avoid order rejections
- P2: Trade Analytics (win-rate, avg P&L, equity curve) in bot panel
- P2: Telegram alerts for bot entries/exits/daily-limit halts
- P3: Migrate 1s REST polling to WebSockets; partial exits + shorting for WunderTrading
