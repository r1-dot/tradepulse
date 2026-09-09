import { useEffect, useState, useCallback } from "react";
import axios from "axios";
import { X, Zap } from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const SLIDERS = [
  { key: "sl_percent", label: "SL %", min: 0.3, max: 1.5, step: 0.1 },
  { key: "tp_percent", label: "TP %", min: 0.8, max: 4.0, step: 0.1 },
  { key: "min_power_1m", label: "Min Power 1m", min: 0.4, max: 1.0, step: 0.05 },
  { key: "max_power_1m", label: "Max Power 1m", min: 1.2, max: 2.5, step: 0.1 },
  { key: "burst_power", label: "Burst 1s", min: 0.03, max: 0.15, step: 0.01 },
];

export const HybridPanel = ({ open, onClose }) => {
  const [data, setData] = useState(null);
  const [cfg, setCfg] = useState(null);

  const poll = useCallback(async () => {
    try {
      const { data } = await axios.get(`${API}/hybrid`);
      setData(data);
      setCfg((c) => c || data.config);
    } catch (e) { /* ignore */ }
  }, []);

  useEffect(() => {
    if (!open) return;
    poll();
    const t = setInterval(poll, 1000);
    return () => clearInterval(t);
  }, [open, poll]);

  const patch = useCallback(async (upd) => {
    setCfg((c) => ({ ...c, ...upd }));
    try { await axios.post(`${API}/bot/config`, upd); } catch (e) { /* ignore */ }
  }, []);

  if (!open) return null;
  const rows = data?.rows || [];
  const c = cfg || data?.config || {};

  return (
    <div className="fixed inset-y-0 right-0 z-50 flex w-full max-w-[560px] flex-col border-l border-zinc-200 bg-white shadow-2xl" data-testid="hybrid-panel">
      <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-3">
        <div className="flex items-center gap-2">
          <Zap size={18} className="text-[#7C3AED]" />
          <span className="font-heading text-[15px] font-bold tracking-tight text-zinc-900">HYBRID POWER SYSTEM</span>
        </div>
        <div className="flex items-center gap-3">
          <button
            data-testid="hybrid_toggle"
            onClick={() => patch({ hybrid_toggle: !c.hybrid_toggle })}
            className={`mono flex items-center gap-2 border px-3 py-1 text-[11px] font-semibold transition-colors ${
              c.hybrid_toggle ? "border-[#00A004] bg-[#00C805] text-white" : "border-zinc-300 text-zinc-500"
            }`}
          >
            Hybrid {c.hybrid_toggle ? "ON" : "OFF"}
          </button>
          <button data-testid="hybrid-close" onClick={onClose} className="text-zinc-400 hover:text-zinc-900"><X size={18} /></button>
        </div>
      </div>

      {!c.hybrid_toggle && (
        <div className="mono border-b border-zinc-200 bg-amber-50 px-4 py-1.5 text-[10px] text-[#B26A00]">
          Hybrid is OFF — no signals are generated or executed.
        </div>
      )}

      {/* sliders */}
      <div className="grid grid-cols-1 gap-2.5 border-b border-zinc-200 px-4 py-3">
        {SLIDERS.map((s) => (
          <div key={s.key} className="flex items-center gap-3" data-testid={`hybrid-slider-row-${s.key}`}>
            <span className="mono w-28 shrink-0 text-[11px] text-zinc-600">{s.label}</span>
            <input
              type="range" min={s.min} max={s.max} step={s.step}
              value={c[s.key] ?? s.min}
              data-testid={`hybrid-${s.key}`}
              onChange={(e) => patch({ [s.key]: parseFloat(e.target.value) })}
              className="h-1 flex-1 cursor-pointer accent-[#7C3AED]"
            />
            <span data-testid={`hybrid-val-${s.key}`} className="mono tnum w-12 shrink-0 text-right text-[12px] font-bold text-[#7C3AED]">
              {Number(c[s.key] ?? s.min).toFixed(2)}
            </span>
          </div>
        ))}
      </div>

      {/* table */}
      <div className="scan-scroll flex-1 overflow-y-auto">
        <table className="w-full border-collapse">
          <thead className="sticky top-0 bg-zinc-50">
            <tr className="mono text-[9px] uppercase tracking-wider text-zinc-400">
              {["Coin", "pwr 1m", "pwr 1s", "pwr 5s", "avg $", "buy %", "SIGNAL"].map((h) => (
                <th key={h} className="border-b border-zinc-200 px-2 py-1.5 text-right first:text-left">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.coin} data-testid={`hybrid-row-${r.coin}`} className="mono tnum text-[11px] text-zinc-700 hover:bg-zinc-50">
                <td className="border-b border-zinc-100 px-2 py-1.5 text-left font-bold text-zinc-900">{r.coin}</td>
                <td className="border-b border-zinc-100 px-2 py-1.5 text-right">{r.power_1m}</td>
                <td className="border-b border-zinc-100 px-2 py-1.5 text-right">{r.power_1s}</td>
                <td className="border-b border-zinc-100 px-2 py-1.5 text-right">{r.power_5s}</td>
                <td className="border-b border-zinc-100 px-2 py-1.5 text-right">{r.avg}</td>
                <td className={`border-b border-zinc-100 px-2 py-1.5 text-right ${r.buy >= 60 ? "text-[#00A004]" : r.buy <= 40 ? "text-[#FF3B30]" : ""}`}>{r.buy}</td>
                <td className="border-b border-zinc-100 px-2 py-1.5 text-right">
                  {r.signal ? (
                    <span className={`rounded-sm px-1.5 py-0.5 text-[9px] font-bold text-white ${r.signal === "LONG" ? "bg-[#00A004]" : "bg-[#FF3B30]"}`}>{r.signal}</span>
                  ) : <span className="text-zinc-300">—</span>}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan={7} className="mono px-4 py-6 text-center text-[11px] text-zinc-400">warming up…</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};
