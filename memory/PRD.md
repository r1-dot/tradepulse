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
- Tested: 22/23 backend, 100% frontend

## Known Gaps / Notes
- CoinGecko rate-limits (429) without an API key → Total Mkt Cap/Dominance/Active Coins show "—". Add COINGECKO_API_KEY or COINMARKETCAP_API_KEY.
- Etherscan/CoinMarketCap tiles need keys (graceful "—" without).
- Long timeframes (4d/7d/10d) show `approx` until enough runtime history accumulates.

## Backlog
- P1: Per-token detail drawer with 15-timeframe breakdown + mini sparkline
- P1: Quote-asset filter (BTC/ETH/FDUSD pairs), persist favorites/watchlist
- P2: Trade-rate heat coloring, CSV export, sound/visual alerts on trade spikes
