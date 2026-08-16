import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import axios from "axios";
import { useVirtualizer } from "@tanstack/react-virtual";
import { SiBinance } from "react-icons/si";
import { Search, ArrowDown, ArrowUp, ChevronDown, Bell, BellRing, Volume2, VolumeX, History, X, Trash2, Cpu } from "lucide-react";
import { toast, Toaster } from "sonner";
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
const THRESHOLDS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1300, 1500, 2000];
const TF_SECONDS = { "1s": 1, "5s": 5, "15s": 15, "30s": 30, "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400, "4d": 345600, "7d": 604800, "10d": 864000 };
// Algorithmic-trade footprint: high trade rate + small average order size + a burst
// above the token's normal 24h rate. Sensitivity loosens/tightens the thresholds.
const ALGO_SENS = {
  high: { label: "High", rate: 1, burst: 2.5, avgSize: 2000, minTrades: 3 },
  med: { label: "Med", rate: 2, burst: 4, avgSize: 1000, minTrades: 5 },
  low: { label: "Low", rate: 4, burst: 6, avgSize: 500, minTrades: 8 },
};
const ROW_H = 34;

const fmtClock = (ts) => {
  const d = new Date(ts);
  return d.toLocaleTimeString("en-US", { hour12: false });
};

let _audioCtx = null;
function beep() {
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const ctx = _audioCtx;
    if (ctx.state === "suspended") ctx.resume();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = "sine";
    o.frequency.value = 880;
    g.gain.setValueAtTime(0.0001, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.22);
    o.connect(g);
    g.connect(ctx.destination);
    o.start();
    o.stop(ctx.currentTime + 0.24);
  } catch (e) { /* ignore */ }
}

export default function Scanner() {
  const [timeframe, setTimeframe] = useState("1s");
  const [sort, setSort] = useState("trades_desc");
  const [search, setSearch] = useState("");
  const [tokens, setTokens] = useState([]);
  const [meta, setMeta] = useState({});
  const [status, setStatus] = useState({});
  const [market, setMarket] = useState(null);
  const [sortOpen, setSortOpen] = useState(false);
  const [alertThreshold, setAlertThreshold] = useState(null);
  const [alertOpen, setAlertOpen] = useState(false);
  const [onlyAlerts, setOnlyAlerts] = useState(false);
  const [muted, setMuted] = useState(false);
  const [alertCount, setAlertCount] = useState(0);
  const [alertHistory, setAlertHistory] = useState([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [algoOn, setAlgoOn] = useState(false);
  const [algoSens, setAlgoSens] = useState("med");
  const [algoSensOpen, setAlgoSensOpen] = useState(false);
  const [onlyAlgo, setOnlyAlgo] = useState(false);
  const [algoCount, setAlgoCount] = useState(0);
  const [pressure, setPressure] = useState("all"); // all | buy | sell

  const prevTrades = useRef({});
  const parentRef = useRef(null);
  const alertingRef = useRef(new Set());
  const seedRef = useRef(false);
  const algoRef = useRef(new Set());
  const algoSeedRef = useRef(false);

  // stable refs for interval callback
  const cfg = useRef({ timeframe, sort, search, alertThreshold, muted, algoOn, algoSens });
  cfg.current = { timeframe, sort, search, alertThreshold, muted, algoOn, algoSens };

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

      // ---- trade alerts ----
      const th = cfg.current.alertThreshold;
      if (th) {
        const nowAlerting = new Set();
        for (const r of rows) if (r.trades >= th) nowAlerting.add(r.symbol);
        setAlertCount(nowAlerting.size);
        if (seedRef.current) {
          // first pass after enabling/changing — seed silently, no spam
          alertingRef.current = nowAlerting;
          seedRef.current = false;
        } else {
          const prevSet = alertingRef.current;
          const crossed = [...nowAlerting].filter((s) => !prevSet.has(s));
          alertingRef.current = nowAlerting;
          if (crossed.length) {
            if (!cfg.current.muted) beep();
            const now = Date.now();
            const entries = crossed.map((sym) => {
              const r = rows.find((x) => x.symbol === sym);
              return {
                id: `${sym}-${now}-${Math.random().toString(36).slice(2, 7)}`,
                kind: "threshold",
                symbol: sym, base: r.base, threshold: th, trades: r.trades,
                timeframe: cfg.current.timeframe, change: r.change, side: r.side,
                volume: r.quoteVol, ts: now,
              };
            });
            setAlertHistory((prev) => [...entries, ...prev].slice(0, 300));
            crossed.slice(0, 3).forEach((sym) => {
              const r = rows.find((x) => x.symbol === sym);
              toast(`${r.base}/USDT crossed ${th} trades`, {
                description: `${withCommas(r.trades)} trades in ${cfg.current.timeframe} · ${fmtPct(r.change)}`,
              });
            });
            if (crossed.length > 3) {
              toast(`+${crossed.length - 3} more tokens crossed ${th} trades`);
            }
          }
        }
      } else {
        alertingRef.current = new Set();
        setAlertCount(0);
      }

      // ---- algorithmic-trade detection ----
      if (cfg.current.algoOn) {
        const winSec = TF_SECONDS[cfg.current.timeframe] || 1;
        const s = ALGO_SENS[cfg.current.algoSens] || ALGO_SENS.med;
        for (const r of rows) {
          const rate = r.trades / winSec;                 // trades per second (window)
          const baseline = r.trades24h / 86400;            // avg trades/sec over 24h
          const burst = baseline > 0 ? rate / baseline : (rate > 0 ? 99 : 0);
          const avgSize = r.trades24h > 0 ? r.quoteVol / r.trades24h : 0; // $ per trade
          r.algo = rate >= s.rate && burst >= s.burst && avgSize > 0 && avgSize <= s.avgSize && r.trades >= s.minTrades;
          if (r.algo) { r.algoBurst = burst; r.algoAvg = avgSize; r.algoRate = rate; }
        }
        const nowAlgo = new Set();
        for (const r of rows) if (r.algo) nowAlgo.add(r.symbol);
        setAlgoCount(nowAlgo.size);
        if (algoSeedRef.current) {
          algoRef.current = nowAlgo;
          algoSeedRef.current = false;
        } else {
          const prevAlgo = algoRef.current;
          const newly = [...nowAlgo].filter((x) => !prevAlgo.has(x));
          algoRef.current = nowAlgo;
          if (newly.length) {
            if (!cfg.current.muted) beep();
            const now = Date.now();
            const entries = newly.map((sym) => {
              const r = rows.find((x) => x.symbol === sym);
              return {
                id: `algo-${sym}-${now}-${Math.random().toString(36).slice(2, 7)}`,
                kind: "algo",
                symbol: sym, base: r.base, trades: r.trades,
                timeframe: cfg.current.timeframe, change: r.change, side: r.side,
                volume: r.quoteVol, ts: now,
                reason: `${r.algoBurst >= 99 ? "99+" : r.algoBurst.toFixed(1)}× normal rate · ~$${Math.round(r.algoAvg)}/trade`,
              };
            });
            setAlertHistory((prev) => [...entries, ...prev].slice(0, 300));
            newly.slice(0, 3).forEach((sym) => {
              const r = rows.find((x) => x.symbol === sym);
              toast(`${r.base}/USDT — algo activity`, {
                description: `${withCommas(r.trades)} trades in ${cfg.current.timeframe} · ${r.algoBurst >= 99 ? "99+" : r.algoBurst.toFixed(1)}× normal`,
              });
            });
            if (newly.length > 3) toast(`+${newly.length - 3} more tokens flagged as algo`);
          }
        }
      } else {
        algoRef.current = new Set();
        setAlgoCount(0);
      }
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

  // refetch immediately when controls change (and seed alerts to avoid spam)
  useEffect(() => {
    seedRef.current = true;
    algoSeedRef.current = true;
    fetchTokens();
  }, [timeframe, sort, search, alertThreshold, algoOn, algoSens, fetchTokens]);

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

  const displayTokens = useMemo(() => {
    let list = tokens;
    if (pressure !== "all") list = list.filter((t) => t.side === pressure);
    if (onlyAlerts && alertThreshold) list = list.filter((t) => t.trades >= alertThreshold);
    if (onlyAlgo && algoOn) list = list.filter((t) => t.algo);
    return list;
  }, [tokens, pressure, onlyAlerts, alertThreshold, onlyAlgo, algoOn]);

  const rowVirtualizer = useVirtualizer({
    count: displayTokens.length,
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
          {/* Pressure filter */}
          <div className="flex items-center border border-zinc-200" data-testid="pressure-filter">
            {[
              { k: "all", label: "All", cls: "text-zinc-700" },
              { k: "buy", label: "Buying", cls: "text-[#00C805]" },
              { k: "sell", label: "Selling", cls: "text-[#FF3B30]" },
            ].map((p, idx) => (
              <button
                key={p.k}
                data-testid={`pressure-${p.k}`}
                onClick={() => setPressure(p.k)}
                className={`mono px-2.5 py-1 text-[11px] font-medium transition-colors ${idx > 0 ? "border-l border-zinc-200" : ""} ${
                  pressure === p.k ? "bg-zinc-900 text-white" : `${p.cls} hover:bg-zinc-50`
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>

          {meta.approx && (
            <span data-testid="approx-badge" className="mono text-[10px] text-[#F5A623] border border-[#F5A623]/40 px-1.5 py-0.5">
              APPROX · warming history
            </span>
          )}

          {/* Alert controls */}
          <div className="flex items-center gap-1.5" data-testid="alert-controls">
            <div className="relative">
              <button
                data-testid="alert-toggle"
                onClick={() => setAlertOpen((o) => !o)}
                className={`mono flex items-center gap-1 border px-2.5 py-1 text-[12px] transition-colors ${
                  alertThreshold
                    ? "border-[#002FA7] bg-[#002FA7] text-white"
                    : "border-zinc-200 text-zinc-700 hover:border-zinc-400"
                }`}
              >
                <Bell size={12} />
                {alertThreshold ? `≥ ${alertThreshold}` : "Alerts"}
                {alertThreshold ? (
                  <span data-testid="alert-count" className="ml-1 rounded-sm bg-white/25 px-1 text-[10px]">{alertCount}</span>
                ) : (
                  <ChevronDown size={13} />
                )}
              </button>
              {alertOpen && (
                <div className="absolute right-0 z-30 mt-1 w-40 border border-zinc-200 bg-white shadow-sm">
                  <button
                    data-testid="alert-off"
                    onClick={() => { setAlertThreshold(null); setOnlyAlerts(false); setAlertOpen(false); }}
                    className={`mono block w-full px-3 py-1.5 text-left text-[12px] hover:bg-zinc-50 ${!alertThreshold ? "text-zinc-900 font-semibold" : "text-zinc-500"}`}
                  >
                    Off
                  </button>
                  <div className="max-h-56 overflow-y-auto scan-scroll">
                    {THRESHOLDS.map((th) => (
                      <button
                        key={th}
                        data-testid={`alert-threshold-${th}`}
                        onClick={() => { setAlertThreshold(th); setAlertOpen(false); }}
                        className={`mono block w-full px-3 py-1.5 text-left text-[12px] hover:bg-zinc-50 ${alertThreshold === th ? "text-[#002FA7] font-semibold" : "text-zinc-600"}`}
                      >
                        ≥ {th} trades
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {alertThreshold && (
              <>
                <button
                  data-testid="only-alerts-toggle"
                  onClick={() => setOnlyAlerts((o) => !o)}
                  className={`mono border px-2 py-1 text-[11px] transition-colors ${
                    onlyAlerts ? "border-[#002FA7] text-[#002FA7]" : "border-zinc-200 text-zinc-500 hover:border-zinc-400"
                  }`}
                >
                  Only alerts
                </button>
                <button
                  data-testid="mute-toggle"
                  aria-label="toggle alert sound"
                  onClick={() => setMuted((m) => !m)}
                  className="border border-zinc-200 p-1 text-zinc-500 hover:border-zinc-400"
                >
                  {muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
                </button>
              </>
            )}
          </div>

          {/* Algo detection */}
          <div className="flex items-center gap-1.5" data-testid="algo-controls">
            <button
              data-testid="algo-toggle"
              onClick={() => setAlgoOn((o) => { if (o) setOnlyAlgo(false); return !o; })}
              className={`mono flex items-center gap-1 border px-2.5 py-1 text-[12px] transition-colors ${
                algoOn ? "border-[#F5A623] bg-[#F5A623] text-white" : "border-zinc-200 text-zinc-700 hover:border-zinc-400"
              }`}
            >
              <Cpu size={12} /> Algo
              {algoOn ? (
                <span data-testid="algo-count" className="ml-0.5 rounded-sm bg-white/25 px-1 text-[10px]">{algoCount}</span>
              ) : null}
            </button>
            {algoOn && (
              <>
                <div className="relative">
                  <button
                    data-testid="algo-sens-toggle"
                    onClick={() => setAlgoSensOpen((o) => !o)}
                    className="mono flex items-center gap-1 border border-zinc-200 px-2 py-1 text-[11px] text-zinc-600 hover:border-zinc-400"
                  >
                    {ALGO_SENS[algoSens].label} <ChevronDown size={12} />
                  </button>
                  {algoSensOpen && (
                    <div className="absolute right-0 z-30 mt-1 w-28 border border-zinc-200 bg-white shadow-sm">
                      {Object.entries(ALGO_SENS).map(([k, v]) => (
                        <button
                          key={k}
                          data-testid={`algo-sens-${k}`}
                          onClick={() => { setAlgoSens(k); setAlgoSensOpen(false); }}
                          className={`mono block w-full px-3 py-1.5 text-left text-[11px] hover:bg-zinc-50 ${algoSens === k ? "text-[#F5A623] font-semibold" : "text-zinc-600"}`}
                        >
                          {v.label} sensitivity
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                <button
                  data-testid="only-algo-toggle"
                  onClick={() => setOnlyAlgo((o) => !o)}
                  className={`mono border px-2 py-1 text-[11px] transition-colors ${
                    onlyAlgo ? "border-[#F5A623] text-[#B26A00]" : "border-zinc-200 text-zinc-500 hover:border-zinc-400"
                  }`}
                >
                  Only algo
                </button>
              </>
            )}
          </div>

          <button
            data-testid="history-toggle"
            onClick={() => setHistoryOpen(true)}
            className="mono flex items-center gap-1 border border-zinc-200 px-2.5 py-1 text-[12px] text-zinc-700 hover:border-zinc-400"
          >
            <History size={13} /> Log
            {alertHistory.length > 0 && (
              <span data-testid="history-badge" className="ml-0.5 rounded-sm bg-zinc-900 px-1 text-[10px] text-white">
                {alertHistory.length}
              </span>
            )}
          </button>

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
        {displayTokens.length === 0 ? (
          <div className="flex h-40 items-center justify-center mono text-[13px] text-zinc-400" data-testid="warming-state">
            {!connected ? "Connecting to Binance…" : (onlyAlerts && alertThreshold) ? `No tokens above ${alertThreshold} trades yet.` : "No tokens match your search."}
          </div>
        ) : (
          <div style={{ height: rowVirtualizer.getTotalSize(), position: "relative", width: "100%" }}>
            {rowVirtualizer.getVirtualItems().map((vi) => {
              const t = displayTokens[vi.index];
              const up = t.change >= 0;
              const flash = t.dir === 1 ? "flash-up" : t.dir === -1 ? "flash-down" : "";
              const isAlert = alertThreshold && t.trades >= alertThreshold;
              const isAlgo = algoOn && t.algo;
              return (
                <div
                  key={t.symbol}
                  data-testid={`token-row-${t.symbol}`}
                  className={`grid grid-cols-[44px_minmax(120px,1.4fr)_1fr_0.9fr_1.1fr_1fr_1.1fr] items-center border-b border-zinc-100 px-4 text-[13px] hover:bg-zinc-50 ${isAlert ? "bg-[#002FA7]/[0.04]" : ""}`}
                  style={{ position: "absolute", top: 0, left: 0, width: "100%", height: ROW_H, transform: `translateY(${vi.start}px)`, boxShadow: isAlert ? "inset 3px 0 0 #002FA7" : "none" }}
                >
                  <div className="mono tnum text-[11px] text-zinc-300">{vi.index + 1}</div>
                  <div className="flex items-baseline gap-1">
                    {isAlert && <BellRing size={11} className="text-[#002FA7]" data-testid={`alert-flag-${t.symbol}`} />}
                    <span className="mono font-semibold text-zinc-900">{t.base}</span>
                    <span className="mono text-[10px] text-zinc-300">/USDT</span>
                    {isAlgo && (
                      <span
                        data-testid={`algo-flag-${t.symbol}`}
                        title={`Algo: ${t.algoBurst >= 99 ? "99+" : t.algoBurst?.toFixed(1)}× normal rate, ~$${Math.round(t.algoAvg || 0)}/trade`}
                        className="ml-1 inline-flex items-center gap-0.5 self-center rounded-sm bg-[#F5A623]/15 px-1 py-0.5 text-[8px] font-semibold uppercase tracking-wide text-[#B26A00]"
                      >
                        <Cpu size={8} /> algo
                      </span>
                    )}
                  </div>
                  <div className="mono tnum text-right text-zinc-700">{fmtPrice(t.price)}</div>
                  <div className={`mono tnum text-right ${up ? "text-[#00C805]" : "text-[#FF3B30]"}`}>
                    {fmtPct(t.change)}
                  </div>
                  <div className={`text-right px-1 ${flash}`}>
                    <div className="flex items-center justify-end gap-1.5">
                      <span
                        data-testid={`side-${t.symbol}`}
                        className={`text-[9px] font-semibold uppercase tracking-wide ${t.side === "buy" ? "text-[#00C805]" : "text-[#FF3B30]"}`}
                      >
                        {t.side === "buy" ? "buying" : "selling"}
                      </span>
                      <span className={`mono tnum font-semibold ${isAlert ? "text-[#002FA7]" : "text-zinc-900"}`}>
                        {withCommas(t.trades)}
                      </span>
                    </div>
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
          {displayTokens.length} / {meta.totalPairs || 0} pairs
        </span>
        <span className="flex items-center gap-1">
          <ArrowUp size={10} className="text-[#00C805]" />/<ArrowDown size={10} className="text-[#FF3B30]" /> live tick
        </span>
        <span data-testid="footer-trades">
          Σ trades ({timeframe}): <b className="text-zinc-600">{withCommas(meta.totalTrades)}</b>
        </span>
        {alertThreshold && (
          <span data-testid="footer-alerts" className="flex items-center gap-1 text-[#002FA7]">
            <BellRing size={10} /> {alertCount} above {alertThreshold}
          </span>
        )}
        {algoOn && (
          <span data-testid="footer-algo" className="flex items-center gap-1 text-[#B26A00]">
            <Cpu size={10} /> {algoCount} algo
          </span>
        )}
        <span className="ml-auto">
          history depth {meta.historyDepthSec ? Math.round(meta.historyDepthSec) + "s" : "0s"} · refresh 1s · src data-api.binance.vision
        </span>
      </footer>
      <Toaster position="bottom-right" toastOptions={{ className: "mono" }} />

      {/* Alert history slide-over */}
      {historyOpen && (
        <div className="fixed inset-0 z-40" data-testid="alert-history-panel">
          <div className="absolute inset-0 bg-black/20" onClick={() => setHistoryOpen(false)} />
          <div className="absolute right-0 top-0 flex h-full w-[400px] max-w-[90vw] flex-col border-l border-zinc-200 bg-white shadow-xl">
            <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-3">
              <div className="flex items-center gap-2">
                <BellRing size={15} className="text-[#002FA7]" />
                <span className="font-heading text-[15px] font-bold tracking-tight text-zinc-900" style={{ fontWeight: 700 }}>
                  Alert History
                </span>
                <span className="mono text-[11px] text-zinc-400">{alertHistory.length}</span>
              </div>
              <div className="flex items-center gap-1">
                <button
                  data-testid="history-clear"
                  onClick={() => setAlertHistory([])}
                  className="mono flex items-center gap-1 border border-zinc-200 px-2 py-1 text-[11px] text-zinc-600 hover:border-zinc-400"
                >
                  <Trash2 size={12} /> Clear
                </button>
                <button
                  data-testid="history-close"
                  onClick={() => setHistoryOpen(false)}
                  className="border border-zinc-200 p-1 text-zinc-600 hover:border-zinc-400"
                >
                  <X size={14} />
                </button>
              </div>
            </div>
            <div className="scan-scroll flex-1 overflow-y-auto">
              {alertHistory.length === 0 ? (
                <div data-testid="history-empty" className="flex h-40 items-center justify-center px-6 text-center mono text-[12px] text-zinc-400">
                  No alerts logged yet. Arm a threshold from the Alerts menu; every token that crosses it will appear here.
                </div>
              ) : (
                alertHistory.map((h, i) => (
                  <div
                    key={h.id}
                    data-testid={`history-item-${i}`}
                    className="flex items-center gap-3 border-b border-zinc-100 px-4 py-2 hover:bg-zinc-50"
                  >
                    <div className="mono text-[10px] text-zinc-400 w-16 shrink-0">{fmtClock(h.ts)}</div>
                    <div className="flex-1 min-w-0">
                      <div className="mono text-[13px] font-semibold text-zinc-900 flex items-center gap-1">
                        {h.base}<span className="text-[10px] text-zinc-300">/USDT</span>
                        {h.kind === "algo" && (
                          <span className="inline-flex items-center gap-0.5 rounded-sm bg-[#F5A623]/15 px-1 text-[8px] font-semibold uppercase text-[#B26A00]"><Cpu size={8} /> algo</span>
                        )}
                      </div>
                      <div className="mono text-[10px] text-zinc-400 truncate">
                        {h.kind === "algo" ? h.reason : `crossed ≥ ${h.threshold}`} · {h.timeframe} · <span className={h.side === "buy" ? "text-[#00C805]" : "text-[#FF3B30]"}>{h.side === "buy" ? "buying" : "selling"}</span>
                      </div>
                    </div>
                    <div className="text-right">
                      <div className="mono tnum text-[13px] font-semibold text-[#002FA7]">{withCommas(h.trades)}</div>
                      <div className={`mono tnum text-[10px] ${h.change >= 0 ? "text-[#00C805]" : "text-[#FF3B30]"}`}>
                        {fmtPct(h.change)}
                      </div>
                      <div data-testid={`history-vol-${i}`} className="mono tnum text-[10px] text-zinc-400">
                        vol {compactUsd(h.volume)}
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
