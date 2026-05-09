// Dynamic-precision number formatters for prices, PnL, and percentages.
// Crypto contracts span ~8 orders of magnitude in price ($0.00001 → $100k+),
// so a fixed 2-decimal rule loses real moves on sub-cent assets.

export function formatPrice(p: number | null | undefined): string {
  if (p === null || p === undefined || !isFinite(p)) return "—";
  const abs = Math.abs(p);
  const decimals =
    abs >= 100 ? 2 :
    abs >= 1   ? 4 :
    abs >= 0.01 ? 6 : 8;
  return p.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: decimals,
  });
}

export function formatPnl(p: number | null | undefined): string {
  return formatPrice(p);
}

export function formatPct(p: number | null | undefined): string {
  if (p === null || p === undefined || !isFinite(p)) return "—";
  const abs = Math.abs(p);
  const decimals = abs >= 10 ? 2 : abs >= 1 ? 3 : 4;
  return p.toFixed(decimals);
}
