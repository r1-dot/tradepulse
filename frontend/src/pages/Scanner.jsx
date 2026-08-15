import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import axios from "axios";
import { useVirtualizer } from "@tanstack/react-virtual";
import { SiBinance } from "react-icons/si";
import { Search, ArrowDown, ArrowUp, ChevronDown } from "lucide-react";
import MarketOverview from "@/components/MarketOverview";
import { withCommas, compactUsd, fmtPrice, fmtPct } from "@/lib/format";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const TIMEFRAMES = ["1s", "5s", "15s", "30s", "1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "4d", "7d", "10d"];
const SORTS = [
  { key: "trades_desc", label: "Trades ↓" },
  { key: "trades_asc", label: "Trades ↑" },
  { key: "change_desc", label: "Gainers" },
  { key: "change_asc", label: "Losers" },
  { key: "volume_desc", label: "Volume" },
  { key: "symbol_asc", label: "A → Z" },
];
const ROW_H = 34;

export default function Scanner() {
  const [timeframe, setTimeframe] = useState("1s");
  const [sort, setSort] = useState("trades_desc");
  const [search, setSearch] = useState("");
  const [tokens, setTokens] = useState([]);
  const [meta, setMeta] = useState({});
  const [status, setStatus] = useState({});
  const [market, setMarket] = useState(null);
  const [sortOpen, setSortOpen] = useState(false);

  const prevTrades = useRef({});
  const parentRef = useRef(null);

  // stable refs for interval callback
  const cfg = useRef({ timeframe, sort, search });
  cfg.current = { timeframe, sort, search };

  const fetchTokens = useCallback(async () => {
    const { timeframe, sort, search } = cfg.current;
    try {
      const { data } = await axios.get(`${API}/tokens`, {
        params: { timeframe, sort, search, limit: 1000 },
      });
      const prev = prevTrades.current;
      const next = {};
      const rows = (data.tokens || []).map((t) => {
        const before = prev[t.symbol];
        let dir = 0;
        if (before !== undefined && t.trades !== before) dir = t.trades > before ? 1 : -1;
        next[t.symbol] = t.trades;
        return { ...t, dir };
      });
      prevTrades.current = next;
      setTokens(rows);
      setMeta({
        approx: data.approx, total: data.total, totalPairs: data.totalPairs,
        totalTrades: data.totalTrades, historyDepthSec: data.historyDepthSec,
      });
    } catch (e) {
      // keep last data on transient errors
    }
  }, []);

  // 1-second token polling
  useEffect(() => {
    fetchTokens();
    const id = setInterval(fetchTokens, 1000);
    return () => clearInterval(id);
  }, [fetchTokens]);

  // refetch immediately when controls change
  useEffect(() => { fetchTokens(); }, [timeframe, sort, search, fetchTokens]);

  // engine status + market overview
  useEffect(() => {
    const load = async () => {
      try { setStatus((await axios.get(`${API}/engine/status`)).data); } catch {}
    };
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    const load = async () => {
      try { setMarket((await axios.get(`${API}/market-overview`)).data); } catch {}
    };
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  const rowVirtualizer = useVirtualizer({
    count: tokens.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_H,
    overscan: 12,
  });

  const activeSortLabel = useMemo(
    () => SORTS.find((s) => s.key === sort)?.label || "Sort",
    [sort]
  );

  const connected = status.connected;

  return (
    <div className="flex h-screen w-full flex-col bg-white overflow-hidden">
      {/* Header */}
      <header className="flex items-center gap-4 border-b border-zinc-200 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <SiBinance size={22} color="#F0B90B" />
          <span className="font-heading text-[17px] font-800 tracking-tight text-zinc-900" style={{ fontWeight: 800 }}>
            TRADEPULSE
          </span>
          <span className="mono text-[10px] text-zinc-400 uppercase tracking-widest">Binance Scanner</span>
        </div>

        <div className="ml-2 flex items-center gap-2" data-testid="live-status">
          <span className={`h-2 w-2 rounded-full ${connected ? "bg-[#00C805] live-dot" : "bg-zinc-300"}`} />
          <span className="mono text-[11px] text-zinc-500">
            {connected ? "LIVE" : "CONNECTING"} · {status.pairsTracked || 0} pairs
          </span>
        </div>

        <div className="ml-auto relative w-64">
          <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-zinc-400" />
          <input
            data-testid="search-input"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search symbol e.g. BTC"
            className="mono w-full border-b-2 border-zinc-200 bg-transparent py-1.5 pl-7 pr-2 text-[13px] uppercase placeholder:normal-case placeholder:text-zinc-300 outline-none focus:border-zinc-900 transition-colors"
          />
        </div>
      </header>

      <MarketOverview data={market} />

      {/* Control bar */}
      <div className="flex items-center gap-3 border-b border-zinc-200 px-4 py-2">
        <div className="flex items-center gap-1 overflow-x-auto no-scrollbar" data-testid="timeframe-bar">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              data-testid={`timeframe-btn-${tf}`}
              aria-label={`timeframe ${tf}`}
              onClick={() => setTimeframe(tf)}
              className={`mono shrink-0 border px-2.5 py-1 text-[12px] font-medium transition-colors ${
                timeframe === tf
                  ? "border-zinc-900 bg-zinc-900 text-white"
                  : "border-zinc-200 bg-white text-zinc-500 hover:border-zinc-400 hover:text-zinc-900"
              }`}
            >
              {tf}
            </button>
          ))}
        </div>

        <div className="ml-auto flex items-center gap-3">
          {meta.approx && (
            <span data-testid="approx-badge" className="mono text-[10px] text-[#F5A623] border border-[#F5A623]/40 px-1.5 py-0.5">
              APPROX · warming history
            </span>
          )}
          <div className="relative">
            <button
              data-testid="sort-toggle"
              onClick={() => setSortOpen((o) => !o)}
              className="mono flex items-center gap-1 border border-zinc-200 px-2.5 py-1 text-[12px] text-zinc-700 hover:border-zinc-400"
            >
              {activeSortLabel} <ChevronDown size={13} />
            </button>
            {sortOpen && (
              <div className="absolute right-0 z-20 mt-1 w-36 border border-zinc-200 bg-white shadow-sm">
                {SORTS.map((s) => (
                  <button
                    key={s.key}
                    data-testid={`sort-${s.key}`}
                    onClick={() => { setSort(s.key); setSortOpen(false); }}
                    className={`mono block w-full px-3 py-1.5 text-left text-[12px] hover:bg-zinc-50 ${
                      sort === s.key ? "text-zinc-900 font-semibold" : "text-zinc-500"
                    }`}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Table header */}
      <div className="grid grid-cols-[44px_minmax(120px,1.4fr)_1fr_0.9fr_1.1fr_1fr_1.1fr] items-center border-b border-zinc-200 bg-white px-4 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
        <div>#</div>
        <div>Symbol</div>
        <div className="text-right">Price</div>
        <div className="text-right">24h %</div>
        <div className="text-right text-zinc-900">Trades · {timeframe}</div>
        <div className="text-right">24h Trades</div>
        <div className="text-right">24h Volume</div>
      </div>

      {/* Virtualized body */}
      <div ref={parentRef} className="scan-scroll flex-1 min-h-0 overflow-y-auto" data-testid="token-table">
        {tokens.length === 0 ? (
          <div className="flex h-40 items-center justify-center mono text-[13px] text-zinc-400" data-testid="warming-state">
            {connected ? "No tokens match your search." : "Connecting to Binance…"}
          </div>
        ) : (
          <div style={{ height: rowVirtualizer.getTotalSize(), position: "relative", width: "100%" }}>
            {rowVirtualizer.getVirtualItems().map((vi) => {
              const t = tokens[vi.index];
              const up = t.change >= 0;
              const flash = t.dir === 1 ? "flash-up" : t.dir === -1 ? "flash-down" : "";
              return (
                <div
                  key={t.symbol}
                  data-testid={`token-row-${t.symbol}`}
                  className="grid grid-cols-[44px_minmax(120px,1.4fr)_1fr_0.9fr_1.1fr_1fr_1.1fr] items-center border-b border-zinc-100 px-4 text-[13px] hover:bg-zinc-50"
                  style={{ position: "absolute", top: 0, left: 0, width: "100%", height: ROW_H, transform: `translateY(${vi.start}px)` }}
                >
                  <div className="mono tnum text-[11px] text-zinc-300">{vi.index + 1}</div>
                  <div className="flex items-baseline gap-1">
                    <span className="mono font-semibold text-zinc-900">{t.base}</span>
                    <span className="mono text-[10px] text-zinc-300">/USDT</span>
                  </div>
                  <div className="mono tnum text-right text-zinc-700">{fmtPrice(t.price)}</div>
                  <div className={`mono tnum text-right ${up ? "text-[#00C805]" : "text-[#FF3B30]"}`}>
                    {fmtPct(t.change)}
                  </div>
                  <div className={`mono tnum text-right font-semibold text-zinc-900 px-1 ${flash}`}>
                    {withCommas(t.trades)}
                  </div>
                  <div className="mono tnum text-right text-zinc-400">{withCommas(t.trades24h)}</div>
                  <div className="mono tnum text-right text-zinc-500">{compactUsd(t.quoteVol)}</div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Footer status */}
      <footer className="flex items-center gap-4 border-t border-zinc-200 bg-[#FAFAFA] px-4 py-1.5 mono text-[10px] text-zinc-400">
        <span data-testid="footer-total">
          {meta.total || 0} / {meta.totalPairs || 0} pairs
        </span>
        <span className="flex items-center gap-1">
          <ArrowUp size={10} className="text-[#00C805]" />/<ArrowDown size={10} className="text-[#FF3B30]" /> live tick
        </span>
        <span data-testid="footer-trades">
          Σ trades ({timeframe}): <b className="text-zinc-600">{withCommas(meta.totalTrades)}</b>
        </span>
        <span className="ml-auto">
          history depth {meta.historyDepthSec ? Math.round(meta.historyDepthSec) + "s" : "0s"} · refresh 1s · src data-api.binance.vision
        </span>
      </footer>
    </div>
  );
}
