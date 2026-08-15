import { FaBitcoin, FaEthereum } from "react-icons/fa";
import { SiSolana } from "react-icons/si";
import { Activity, Layers, PieChart, Fuel, Boxes } from "lucide-react";
import { compactUsd, fmtPrice } from "@/lib/format";

const Stat = ({ icon, label, value, sub, testid, accent }) => (
  <div
    data-testid={testid}
    className="flex items-center gap-3 border-r border-zinc-200 px-4 py-2.5 last:border-r-0 min-w-[170px]"
  >
    <div className="text-zinc-400" style={accent ? { color: accent } : {}}>{icon}</div>
    <div className="leading-tight">
      <div className="text-[10px] uppercase tracking-wider text-zinc-400 font-medium">{label}</div>
      <div className="mono tnum text-[15px] font-semibold text-zinc-900">{value}</div>
      {sub && <div className="text-[10px] text-zinc-400">{sub}</div>}
    </div>
  </div>
);

export default function MarketOverview({ data }) {
  const m = data?.metrics || {};
  return (
    <div
      data-testid="market-overview"
      className="flex items-stretch overflow-x-auto no-scrollbar border-b border-zinc-200 bg-[#FAFAFA]"
    >
      <Stat testid="mo-btc" icon={<FaBitcoin size={20} />} accent="#F7931A"
        label="BTC / USD" value={m.btc ? "$" + fmtPrice(m.btc) : "—"} sub="Coinbase" />
      <Stat testid="mo-eth" icon={<FaEthereum size={20} />} accent="#627EEA"
        label="ETH / USD" value={m.eth ? "$" + fmtPrice(m.eth) : "—"} sub="Coinbase" />
      <Stat testid="mo-sol" icon={<SiSolana size={16} />} accent="#14F195"
        label="SOL / USD" value={m.sol ? "$" + fmtPrice(m.sol) : "—"} sub="Coinbase" />
      <Stat testid="mo-mcap" icon={<PieChart size={18} />}
        label="Total Mkt Cap" value={compactUsd(m.totalMarketCap)}
        sub={m.btcDominance ? `BTC ${m.btcDominance.toFixed(1)}%` : "CoinGecko"} />
      <Stat testid="mo-vol" icon={<Layers size={18} />}
        label="24h Volume" value={compactUsd(m.totalVolume24h)} sub="Global" />
      <Stat testid="mo-gas" icon={<Fuel size={18} />}
        label="ETH Gas" value={m.ethGasGwei != null ? m.ethGasGwei + " gwei" : "—"} sub="Etherscan" />
      <Stat testid="mo-tx" icon={<Activity size={18} />}
        label="BTC Tx (24h)" value={m.btcTxCount24h ? (m.btcTxCount24h / 1000).toFixed(0) + "K" : "—"} sub="Blockchain.com" />
      <Stat testid="mo-assets" icon={<Boxes size={18} />}
        label="Active Coins" value={m.activeCryptos ? m.activeCryptos.toLocaleString() : "—"} sub="CoinGecko" />
    </div>
  );
}
