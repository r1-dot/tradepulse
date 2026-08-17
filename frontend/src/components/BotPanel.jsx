import { useEffect, useState, useCallback } from "react";
import axios from "axios";
import { X, Power, ShieldAlert, Bot, Trash2, RotateCcw, TriangleAlert, Cpu } from "lucide-react";
import { compactUsd } from "@/lib/format";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const VOL_1S = [100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000];
const VOL_24H = [1e6, 5e6, 1e7, 5e7, 1e8, 2.5e8, 5e8, 1e9];
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("en-US", { hour12: false });

const NumField = ({ label, value, onChange, step = "0.1", suffix, testid }) => (
  <label className="block">
    <span className="mono text-[10px] uppercase tracking-wider text-zinc-400">{label}</span>
    <div className="flex items-center border border-zinc-200 focus-within:border-zinc-900 transition-colors">
      <input
        data-testid={testid}
        type="number"
        step={step}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mono w-full bg-transparent px-2 py-1.5 text-[13px] outline-none"
      />
      {suffix && <span className="mono px-2 text-[11px] text-zinc-400">{suffix}</span>}
    </div>
  </label>
);

export default function BotPanel({ open, onClose }) {
  const [st, setSt] = useState(null);
  const [form, setForm] = useState(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const { data } = await axios.get(`${API}/bot/status`);
      setSt(data);
      setForm((f) => f || { ...data.config });
    } catch (e) { /* keep */ }
  }, []);

  useEffect(() => {
    if (!open) return;
    load();
    const id = setInterval(load, 2000);
    return () => clearInterval(id);
  }, [open, load]);

  const patch = async (body) => {
    setSaving(true);
    try {
      const { data } = await axios.post(`${API}/bot/config`, body);
      setSt(data);
      setForm({ ...data.config });
    } finally { setSaving(false); }
  };

  const toggleRun = () => patch({ enabled: !st.config.enabled });

  const toggleMode = async () => {
    const goingLive = st.config.dryRun; // currently sim -> going live
    if (goingLive) {
      const ok = window.confirm(
        "Switch to LIVE trading with REAL money?\n\nThe bot will place real market orders on your Binance account using your API keys. Only proceed if you understand the risk. (Note: api.binance.com is geo-blocked in this preview, so live orders execute only when deployed to a reachable server.)"
      );
      if (!ok) return;
    }
    patch({ dryRun: !st.config.dryRun });
  };

  const saveSettings = () => patch({
    slPct: parseFloat(form.slPct),
    tpPct: parseFloat(form.tpPct),
    maxPositionUsdt: parseFloat(form.maxPositionUsdt),
    dailyLossLimit: parseFloat(form.dailyLossLimit),
    streak: parseInt(form.streak, 10),
    maxOpenPositions: parseInt(form.maxOpenPositions, 10),
    cooldownSec: parseInt(form.cooldownSec, 10),
  });

  const closeAll = async () => { await axios.post(`${API}/bot/close-all`); load(); };
  const resetDaily = async () => { await axios.post(`${API}/bot/reset-daily`); load(); };

  if (!open) return null;
  const running = st?.config?.enabled;
  const live = st && !st.config.dryRun;
  const halted = st?.stopped;

  return (
    <div className="fixed inset-0 z-50" data-testid="bot-panel">
      <div className="absolute inset-0 bg-black/25" onClick={onClose} />
      <div className="absolute right-0 top-0 flex h-full w-[460px] max-w-[95vw] flex-col border-l border-zinc-200 bg-white shadow-2xl">
        {/* header */}
        <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-3">
          <div className="flex items-center gap-2">
            <Bot size={16} className={running ? "text-[#00C805]" : "text-zinc-400"} />
            <span className="font-heading text-[15px] font-bold tracking-tight text-zinc-900" style={{ fontWeight: 700 }}>
              Auto-Trade Bot
            </span>
            <span className={`mono rounded-sm px-1.5 py-0.5 text-[9px] font-semibold uppercase ${live ? "bg-[#FF3B30] text-white" : "bg-zinc-900 text-white"}`}>
              {live ? "LIVE" : "SIM"}
            </span>
          </div>
          <button data-testid="bot-close" onClick={onClose} className="border border-zinc-200 p-1 text-zinc-600 hover:border-zinc-400"><X size={14} /></button>
        </div>

        <div className="scan-scroll flex-1 overflow-y-auto p-4 space-y-4">
          {!st ? (
            <div className="mono text-[12px] text-zinc-400">Loading…</div>
          ) : (
            <>
              {/* risk banner */}
              <div className={`flex items-start gap-2 border p-2.5 ${live ? "border-[#FF3B30]/40 bg-[#FF3B30]/[0.05]" : "border-[#F5A623]/40 bg-[#F5A623]/[0.06]"}`}>
                <TriangleAlert size={15} className={live ? "text-[#FF3B30] mt-0.5" : "text-[#B26A00] mt-0.5"} />
                <p className="mono text-[10px] leading-relaxed text-zinc-600">
                  {live
                    ? "LIVE mode places REAL market orders on your Binance account. Trading is risky. api.binance.com is geo-blocked in this preview — live orders run only once deployed to a reachable server."
                    : "SIMULATION mode: virtual fills at live market price. No real orders, no real money. Flip to LIVE only when deployed with your Binance keys."}
                </p>
              </div>

              {/* main controls */}
              <div className="flex gap-2">
                <button
                  data-testid="bot-power"
                  onClick={toggleRun}
                  className={`mono flex flex-1 items-center justify-center gap-2 border px-3 py-2 text-[13px] font-semibold transition-colors ${
                    running ? "border-[#FF3B30] bg-[#FF3B30] text-white" : "border-[#00C805] bg-[#00C805] text-white"
                  }`}
                >
                  <Power size={14} /> {running ? "STOP BOT" : "START BOT"}
                </button>
                <button
                  data-testid="bot-mode"
                  onClick={toggleMode}
                  className={`mono flex items-center justify-center gap-1 border px-3 py-2 text-[12px] font-medium transition-colors ${
                    live ? "border-[#FF3B30] text-[#FF3B30]" : "border-zinc-300 text-zinc-700 hover:border-zinc-900"
                  }`}
                  title="Toggle Simulated / Live"
                >
                  <ShieldAlert size={13} /> {live ? "Live" : "Sim"}
                </button>
              </div>

              {/* status grid */}
              <div className="grid grid-cols-2 gap-2">
                <Stat label="State" value={halted ? "HALTED" : running ? "RUNNING" : "IDLE"}
                  color={halted ? "#FF3B30" : running ? "#00C805" : "#71717A"} testid="bot-state" />
                <Stat label="Today P&L" value={`${st.dailyPnl >= 0 ? "+" : ""}${st.dailyPnl.toFixed(4)} USDT`}
                  color={st.dailyPnl >= 0 ? "#00A004" : "#FF3B30"} testid="bot-pnl" />
                <Stat label="Unrealized" value={`${st.unrealizedPnl >= 0 ? "+" : ""}${st.unrealizedPnl.toFixed(4)}`}
                  color={st.unrealizedPnl >= 0 ? "#00A004" : "#FF3B30"} />
                <Stat label="Watching (vol)" value={`${st.watching} pairs`} />
                <Stat label="Open positions" value={`${st.openPositions.length} / ${st.config.maxOpenPositions}`} />
                <Stat label="API keys" value={st.keysConfigured ? "configured" : "missing"}
                  color={st.keysConfigured ? "#00A004" : "#B26A00"} />
              </div>

              {halted && (
                <div className="flex items-center justify-between border border-[#FF3B30]/40 bg-[#FF3B30]/[0.05] p-2">
                  <span className="mono text-[10px] text-[#FF3B30]">Daily loss limit hit — bot halted.</span>
                  <button data-testid="bot-reset-daily" onClick={resetDaily} className="mono flex items-center gap-1 border border-zinc-300 px-2 py-1 text-[10px] hover:border-zinc-900">
                    <RotateCcw size={11} /> Reset
                  </button>
                </div>
              )}

              {/* settings */}
              {form && (
                <div className="space-y-3 border-t border-zinc-100 pt-3">
                  <div className="mono text-[10px] uppercase tracking-wider text-zinc-400">Trigger</div>
                  <label className="block">
                    <span className="mono text-[10px] uppercase tracking-wider text-zinc-400">Volume window</span>
                    <div className="flex border border-zinc-200" data-testid="bot-vol-window">
                      {["1s", "24h"].map((w) => (
                        <button
                          key={w}
                          data-testid={`bot-vol-window-${w}`}
                          onClick={() => patch({ volWindow: w })}
                          className={`mono flex-1 py-1.5 text-[12px] font-medium transition-colors ${w !== "1s" ? "border-l border-zinc-200" : ""} ${
                            st.config.volWindow === w ? "bg-zinc-900 text-white" : "text-zinc-500 hover:bg-zinc-50"
                          }`}
                        >
                          {w === "1s" ? "Per second" : "24 hours"}
                        </button>
                      ))}
                    </div>
                  </label>
                  <label className="block">
                    <span className="mono text-[10px] uppercase tracking-wider text-zinc-400">
                      Volume alert level ({st.config.volWindow} volume)
                    </span>
                    <select
                      data-testid="bot-vol-threshold"
                      value={st.config.volThresholdUsd}
                      onChange={(e) => patch({ volThresholdUsd: parseFloat(e.target.value) })}
                      className="mono w-full border border-zinc-200 bg-transparent px-2 py-1.5 text-[13px] outline-none focus:border-zinc-900"
                    >
                      {(st.config.volWindow === "24h" ? VOL_24H : VOL_1S).map((amt) => (
                        <option key={amt} value={amt}>{`≥ ${compactUsd(amt)} / ${st.config.volWindow}`}</option>
                      ))}
                    </select>
                    <span className="mono text-[9px] text-zinc-400">
                      {st.config.volWindow === "1s"
                        ? "Per-second volume is small (busiest pairs ~$5k/s). Use a low level so it can trigger."
                        : "Rolling 24h quote volume."}
                    </span>
                  </label>
                  <div className="grid grid-cols-2 gap-2">
                    <NumField testid="bot-streak" label="Repeat count" step="1" value={form.streak} onChange={(v) => setForm({ ...form, streak: v })} suffix="× / 1s" />
                    <NumField testid="bot-cooldown" label="Cooldown" step="1" value={form.cooldownSec} onChange={(v) => setForm({ ...form, cooldownSec: v })} suffix="sec" />
                  </div>

                  <div className="mono text-[10px] uppercase tracking-wider text-zinc-400 pt-1">Risk</div>
                  <div className="grid grid-cols-2 gap-2">
                    <NumField testid="bot-tp" label="Take profit" value={form.tpPct} onChange={(v) => setForm({ ...form, tpPct: v })} suffix="%" />
                    <NumField testid="bot-sl" label="Stop loss" value={form.slPct} onChange={(v) => setForm({ ...form, slPct: v })} suffix="%" />
                    <NumField testid="bot-max-pos" label="Max position" value={form.maxPositionUsdt} onChange={(v) => setForm({ ...form, maxPositionUsdt: v })} suffix="USDT" />
                    <NumField testid="bot-daily-loss" label="Daily loss limit" value={form.dailyLossLimit} onChange={(v) => setForm({ ...form, dailyLossLimit: v })} suffix="USDT" />
                    <NumField testid="bot-max-open" label="Max open positions" step="1" value={form.maxOpenPositions} onChange={(v) => setForm({ ...form, maxOpenPositions: v })} />
                  </div>
                  <button
                    data-testid="bot-save"
                    onClick={saveSettings}
                    disabled={saving}
                    className="mono w-full border border-zinc-900 bg-zinc-900 py-2 text-[12px] font-semibold text-white hover:bg-zinc-700 disabled:opacity-50"
                  >
                    {saving ? "Saving…" : "Save settings"}
                  </button>
                </div>
              )}

              {/* open positions */}
              <div className="border-t border-zinc-100 pt-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="mono text-[10px] uppercase tracking-wider text-zinc-400">Open positions</span>
                  {st.openPositions.length > 0 && (
                    <button data-testid="bot-close-all" onClick={closeAll} className="mono flex items-center gap-1 border border-zinc-200 px-2 py-1 text-[10px] text-[#FF3B30] hover:border-[#FF3B30]">
                      <Trash2 size={11} /> Close all
                    </button>
                  )}
                </div>
                {st.openPositions.length === 0 ? (
                  <div className="mono text-[11px] text-zinc-300">None</div>
                ) : (
                  <div className="space-y-1">
                    {st.openPositions.map((p) => (
                      <div key={p.symbol} data-testid={`bot-pos-${p.symbol}`} className="flex items-center justify-between border border-zinc-100 px-2 py-1.5">
                        <div className="mono text-[12px] font-semibold text-zinc-900">{p.base}<span className="text-[9px] text-zinc-300">/USDT</span></div>
                        <div className="mono text-[10px] text-zinc-400">e {p.entryPrice.toPrecision(5)} · tp {p.tpPrice.toPrecision(5)} · sl {p.slPrice.toPrecision(5)}</div>
                        <div className={`mono text-[11px] font-semibold ${p.uPnl >= 0 ? "text-[#00A004]" : "text-[#FF3B30]"}`}>{p.uPnl >= 0 ? "+" : ""}{p.uPnl.toFixed(4)}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* journal */}
              <div className="border-t border-zinc-100 pt-3">
                <span className="mono text-[10px] uppercase tracking-wider text-zinc-400">Activity log</span>
                <div className="mt-2 space-y-1" data-testid="bot-journal">
                  {st.journal.length === 0 ? (
                    <div className="mono text-[11px] text-zinc-300">No activity yet</div>
                  ) : st.journal.map((j, i) => (
                    <div key={i} className="mono flex items-baseline gap-2 text-[10px]">
                      <span className="text-zinc-300 w-14 shrink-0">{clock(j.ts)}</span>
                      <JournalLine j={j} />
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

const Stat = ({ label, value, color, testid }) => (
  <div className="border border-zinc-100 px-2.5 py-1.5">
    <div className="mono text-[9px] uppercase tracking-wider text-zinc-400">{label}</div>
    <div data-testid={testid} className="mono text-[13px] font-semibold" style={{ color: color || "#18181B" }}>{value}</div>
  </div>
);

const JournalLine = ({ j }) => {
  if (j.kind === "entry") return <span className="text-[#00A004]">BUY {j.base} @ {Number(j.price).toPrecision(5)} · {j.mode}</span>;
  if (j.kind === "exit") return <span className={j.pnl >= 0 ? "text-[#00A004]" : "text-[#FF3B30]"}>SELL {j.base} @ {Number(j.price).toPrecision(5)} · {j.reason} · pnl {j.pnl >= 0 ? "+" : ""}{Number(j.pnl).toFixed(4)}</span>;
  if (j.kind === "halt") return <span className="text-[#FF3B30] font-semibold">⛔ {j.message}</span>;
  if (j.kind === "power") return <span className="text-zinc-700">{j.message} ({j.mode})</span>;
  if (j.kind === "error") return <span className="text-[#FF3B30]">ERR {j.symbol} {j.action}: {String(j.message).slice(0, 60)}</span>;
  if (j.kind === "kill_switch") return <span className="text-[#FF3B30]">{j.message}</span>;
  return <span className="text-zinc-400">{j.message || j.kind}</span>;
};
