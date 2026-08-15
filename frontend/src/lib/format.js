export const compactUsd = (n) => {
  if (n === null || n === undefined || isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e12) return "$" + (n / 1e12).toFixed(2) + "T";
  if (abs >= 1e9) return "$" + (n / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return "$" + (n / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return "$" + (n / 1e3).toFixed(2) + "K";
  return "$" + n.toFixed(2);
};

export const compactNum = (n) => {
  if (n === null || n === undefined || isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
};

export const withCommas = (n) => {
  if (n === null || n === undefined || isNaN(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
};

export const fmtPrice = (p) => {
  if (p === null || p === undefined || isNaN(p)) return "—";
  if (p >= 1000) return p.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (p >= 1) return p.toFixed(3);
  if (p >= 0.01) return p.toFixed(5);
  return p.toPrecision(4);
};

export const fmtPct = (p) => {
  if (p === null || p === undefined || isNaN(p)) return "—";
  return (p >= 0 ? "+" : "") + p.toFixed(2) + "%";
};
