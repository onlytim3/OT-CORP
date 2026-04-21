import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { TrendingUp, TrendingDown, DollarSign, Volume2, RefreshCw, ArrowUpDown, ShieldAlert } from "lucide-react";
import { useState, useEffect } from "react";
import { api, usePolling, fetchAPI, type StatusResponse, type Trade, type TradeAnalysis, type VolumeAnalysis, type MarginHealth } from "../config/api";

interface OhlcvCandle { time: number; open: number; high: number; low: number; close: number; volume: number; }
import { Chart } from "../components/Chart";

function VolumeBar({ ratio, label }: { ratio: number; label: string }) {
  const pct = Math.min(ratio * 100, 200);
  const color = ratio >= 0.8 ? '#00d4aa' : ratio >= 0.3 ? '#ffa500' : '#ff4466';
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span className="text-[#888888]">{label}</span>
        <span style={{ color }}>{(ratio * 100).toFixed(0)}%</span>
      </div>
      <div className="h-1.5 bg-white/5 rounded-full overflow-hidden">
        <div className="h-full rounded-full transition-all" style={{ width: `${Math.min(pct, 100)}%`, backgroundColor: color }} />
      </div>
    </div>
  );
}

/** Smart qty formatting — avoids floating point display garbage */
function formatQty(qty: number): string {
  if (qty === 0) return '0';
  const abs = Math.abs(qty);
  // Large quantities: no decimals
  if (abs >= 100) return qty.toLocaleString('en-US', { maximumFractionDigits: 2 });
  // Medium quantities: up to 4 decimals
  if (abs >= 1) return qty.toLocaleString('en-US', { maximumFractionDigits: 4 });
  // Small quantities: up to 6 significant digits
  return Number(qty.toPrecision(6)).toString();
}

/** Parse entry_reasoning into styled prose segments */
function renderReasoning(text: string) {
  // Split on pipe delimiter (legacy format) or double newlines (new prose format)
  const segments = text.includes(' | ')
    ? text.split(' | ').map(s => s.trim()).filter(Boolean)
    : text.split(/\n\n+/).map(s => s.trim()).filter(Boolean);

  return segments.map((segment, i) => {
    // Highlight dollar amounts, percentages, and strategy names
    const highlighted = segment
      .replace(/(\$[\d,.]+)/g, '<span class="text-[#00d4aa] font-medium">$1</span>')
      .replace(/([\d.]+%)/g, '<span class="text-[#4a9eff] font-medium">$1</span>')
      .replace(/\b(strength:?\s*[\d.]+)\b/gi, '<span class="text-[#ffa500]">$1</span>');

    return (
      <p
        key={i}
        className={`text-sm leading-relaxed ${i === 0 ? 'text-[#e8e8e8]' : 'text-[#c0c0c0]'}`}
        dangerouslySetInnerHTML={{ __html: highlighted }}
      />
    );
  });
}

function TradeDetailModal({ trade, onClose, volumes, marginData }: { trade: Trade | null; onClose: () => void; volumes?: VolumeAnalysis[]; marginData?: MarginHealth[] }) {
  const [analyses, setAnalyses] = useState<TradeAnalysis[]>([]);
  const [loadingAnalyses, setLoadingAnalyses] = useState(false);
  const [showReasoning, setShowReasoning] = useState(true);

  useEffect(() => {
    if (trade) {
      setLoadingAnalyses(true);
      fetchAPI<TradeAnalysis[]>(api.tradeAnalyses(trade.id))
        .then(setAnalyses)
        .catch(() => setAnalyses([]))
        .finally(() => setLoadingAnalyses(false));
    } else {
      setAnalyses([]);
    }
  }, [trade]);

  return (
    <Dialog open={!!trade} onOpenChange={() => onClose()}>
      <DialogContent className="bg-[#0a0a0a] border-white/8 text-[#e8e8e8] sm:max-w-3xl max-h-[92vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-3">
            <Badge variant={trade?.side === 'buy' ? 'default' : 'destructive'}>{trade?.side?.toUpperCase()}</Badge>
            {trade?.symbol}
            {trade?.leverage && trade.leverage > 1 && (
              <span className="text-sm font-normal text-[#ffa500] bg-[#ffa500]/10 px-2 py-0.5 rounded-md">{trade.leverage}x Leverage</span>
            )}
          </DialogTitle>
        </DialogHeader>
        {trade && (
          <div className="space-y-4 mt-2 sm:mt-4">
            {/* Core Info Grid */}
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 sm:gap-3">
              {[
                ['Strategy', trade.strategy],
                ['Quantity', formatQty(trade.qty)],
                ['Price', `$${trade.price.toFixed(2)}`],
                ['Total', `$${trade.total.toFixed(2)}`],
                ['P&L', trade.pnl !== null && !trade.is_open ? `${trade.pnl >= 0 ? '+' : ''}$${trade.pnl.toFixed(2)}` : 'Open'],
                ['P&L %', trade.pnl_pct !== null ? `${trade.pnl_pct >= 0 ? '+' : ''}${trade.pnl_pct.toFixed(2)}%` : '-'],
                ['Leverage', trade.leverage ? `${trade.leverage}x` : '1x'],
                ['Take Profit', trade.take_profit_price ? `$${trade.take_profit_price.toFixed(2)}` : 'None'],
                ['Stop Loss', trade.stop_loss_price ? `$${trade.stop_loss_price.toFixed(2)}` : 'None'],
                ['Time', new Date(trade.timestamp).toLocaleString()],
                ['Status', trade.is_open ? 'Open' : 'Closed'],
                ['Closed', trade.closed_at ? new Date(trade.closed_at).toLocaleString() : trade.is_open ? '-' : 'Pending sync'],
              ].map(([label, value]) => (
                <div key={String(label)} className="p-2.5 sm:p-3 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-[10px] sm:text-xs text-[#888888] mb-1">{label}</p>
                  <p className="text-sm sm:text-base font-bold truncate">{value}</p>
                </div>
              ))}
            </div>

            {/* TP/SL Visual Bar */}
            {(trade.stop_loss_price || trade.take_profit_price) && (
              <div className="p-3 rounded-lg bg-white/5 border border-white/10">
                <p className="text-xs text-[#888888] mb-2">Risk/Reward Range</p>
                <div className="relative h-6 bg-white/5 rounded-full overflow-hidden">
                  {trade.stop_loss_price && trade.take_profit_price && (
                    <>
                      <div className="absolute left-0 top-0 h-full bg-[#ff4466]/20 rounded-l-full" style={{ width: '30%' }} />
                      <div className="absolute right-0 top-0 h-full bg-[#00d4aa]/20 rounded-r-full" style={{ width: '30%' }} />
                      <div className="absolute left-1/2 top-0 h-full w-0.5 bg-[#4a9eff] -translate-x-1/2" />
                    </>
                  )}
                  <div className="absolute inset-0 flex items-center justify-between px-3 text-[10px] font-medium">
                    <span className="text-[#ff4466]">{trade.stop_loss_price ? `SL $${trade.stop_loss_price.toFixed(2)}` : ''}</span>
                    <span className="text-[#4a9eff]">Entry ${trade.price.toFixed(2)}</span>
                    <span className="text-[#00d4aa]">{trade.take_profit_price ? `TP $${trade.take_profit_price.toFixed(2)}` : ''}</span>
                  </div>
                </div>
              </div>
            )}

            {/* Volume & Margin Analysis — only for open trades */}
            {trade.is_open && (() => {
              const vol = volumes?.find(v => v.symbol === trade.symbol || v.aster_symbol === trade.symbol);
              const mg = marginData?.find(m => m.symbol === trade.symbol);
              if (!vol && !mg) return null;
              return (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {vol && (
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10 space-y-2">
                      <div className="flex items-center gap-1 text-xs text-[#888888]">
                        <Volume2 className="size-3" />
                        Volume Analysis
                      </div>
                      <VolumeBar ratio={vol.ratio} label="Volume vs 7d avg" />
                      <div className="flex justify-between text-xs">
                        <span className="text-[#888888]">Trend: <span className={vol.trend > 0 ? 'text-[#00d4aa]' : vol.trend < -0.2 ? 'text-[#ff4466]' : 'text-[#c0c0c0]'}>
                          {vol.trend > 0 ? 'Building' : vol.trend < -0.2 ? 'Fading' : 'Stable'}
                        </span></span>
                        <span className="text-[#888888]">Spread: <span className={vol.spread_bps > 30 ? 'text-[#ffa500]' : 'text-[#c0c0c0]'}>{vol.spread_bps.toFixed(0)} bps</span></span>
                      </div>
                      <div className="flex justify-between text-xs">
                        <span className="text-[#888888]">Size mult: <span className="text-[#c0c0c0]">{vol.sizing_multiplier.toFixed(2)}x</span></span>
                        <span className="text-[#888888]">Quote vol: <span className="text-[#c0c0c0]">${vol.recent_quote_volume >= 1e6 ? `${(vol.recent_quote_volume / 1e6).toFixed(1)}M` : `${(vol.recent_quote_volume / 1e3).toFixed(0)}K`}</span></span>
                      </div>
                    </div>
                  )}
                  {mg && (
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10 space-y-2">
                      <div className="flex items-center gap-1 text-xs text-[#888888]">
                        <ShieldAlert className="size-3" />
                        Leverage & Margin
                      </div>
                      <div className="flex items-center justify-between">
                        <span className="text-xs text-[#888888]">Leverage</span>
                        <span className="text-sm font-medium text-[#e8e8e8]">{mg.leverage.toFixed(1)}x</span>
                      </div>
                      <div className="flex items-center justify-between">
                        <span className="text-xs text-[#888888]">Margin Distance</span>
                        <span className={`text-xs px-2 py-0.5 rounded-full border font-medium ${
                          mg.margin_distance > 0.2 ? 'bg-[#00d4aa]/15 text-[#00d4aa] border-[#00d4aa]/30'
                          : mg.margin_distance > 0.1 ? 'bg-[#ffa500]/15 text-[#ffa500] border-[#ffa500]/30'
                          : 'bg-[#ff4466]/15 text-[#ff4466] border-[#ff4466]/30'
                        }`}>
                          {(mg.margin_distance * 100).toFixed(1)}%
                        </span>
                      </div>
                      {mg.liq_price > 0 && (
                        <div className="flex items-center justify-between">
                          <span className="text-xs text-[#888888]">Liq. Price</span>
                          <span className="text-xs text-[#ff4466]">${mg.liq_price.toLocaleString('en-US', { minimumFractionDigits: 2 })}</span>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })()}

            {/* Trade Rationale */}
            {trade.entry_reasoning && (
              <div className="rounded-lg bg-white/5 border border-white/10 overflow-hidden">
                <div className="px-4 py-2.5 border-b border-white/5">
                  <p className="text-xs text-[#888888] font-medium uppercase tracking-wider">Trade Rationale</p>
                </div>
                <div className="px-4 py-3 space-y-2">
                  {renderReasoning(trade.entry_reasoning)}
                </div>
              </div>
            )}

            {/* Live Analysis Updates */}
            {(analyses.length > 0 || loadingAnalyses) && (
              <div className="rounded-lg bg-white/5 border border-white/10 p-4">
                <p className="text-xs text-[#888888] font-medium uppercase tracking-wider mb-3">Live Analysis Updates</p>
                {loadingAnalyses ? (
                  <p className="text-sm text-[#888888] animate-pulse">Loading analysis...</p>
                ) : (
                  <div className="space-y-2.5 max-h-48 overflow-y-auto">
                    {analyses.map(a => (
                      <div key={a.id} className="p-3 rounded-lg bg-black/30 border border-white/5">
                        <div className="flex items-center justify-between mb-1.5">
                          <p className="text-[10px] text-[#888888]">
                            {new Date(a.timestamp).toLocaleString()}
                          </p>
                          <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                            a.source === 'position_review_agent' ? 'bg-[#c084fc]/10 text-[#c084fc]' :
                            a.source === '12h_cycle' ? 'bg-[#4a9eff]/10 text-[#4a9eff]' :
                            a.source === 'static_fallback' ? 'bg-[#888888]/10 text-[#888888]' :
                            'bg-[#00d4aa]/10 text-[#00d4aa]'
                          }`}>
                            {a.source === 'position_review_agent' ? 'Agent Review' :
                             a.source === '12h_cycle' ? 'AI Analysis' :
                             a.source === 'static_fallback' ? 'Auto' : 'LLM'}
                          </span>
                        </div>
                        <p className="text-sm text-[#c0c0c0] leading-relaxed">{a.analysis}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function Trading() {
  const { data: status } = usePolling<StatusResponse>(api.status, 10000);
  const { data: trades } = usePolling<Trade[]>(api.trades, 15000);
  const { data: volumes } = usePolling<VolumeAnalysis[]>(api.volume, 30000);
  const { data: marginData } = usePolling<MarginHealth[]>(api.margin, 15000);
  const { data: chartData } = usePolling<OhlcvCandle[]>(api.priceChart('BTCUSDT', '1h', 48), 60000);
  const [selectedTrade, setSelectedTrade] = useState<Trade | null>(null);

  const positions = status?.positions || [];

  return (
    <div className="p-6 space-y-6">
      <div>
        <h2 className="text-3xl font-bold text-[#e8e8e8]">Trading Dashboard</h2>
        <p className="text-[#888888] mt-1">Real-time positions, trades, and volume analysis</p>
      </div>

      {/* Interactive Chart Section */}
      <Card className="border-white/5 bg-[#0a0a0a]">
        <CardHeader className="pb-4">
          <CardTitle className="text-lg font-bold text-[#e8e8e8]">Market Chart (BTC/USD)</CardTitle>
        </CardHeader>
        <CardContent className="h-[400px] p-0 relative">
          <Chart
            type="candlestick"
            data={(chartData && chartData.length > 0 ? chartData : []) as any}
            height={400}
          />
        </CardContent>
      </Card>

      {/* Recent Trades */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ArrowUpDown className="size-5" />
            Recent Trades
          </CardTitle>
        </CardHeader>
        <CardContent>
          {(!trades || trades.length === 0) ? (
            <p className="text-[#888888] text-center py-8">No trades yet</p>
          ) : (
            <div className="overflow-x-auto max-h-[60vh] overflow-y-auto">
              <table className="w-full">
                <thead className="sticky top-0 bg-[#0a0a0a] z-10">
                  <tr className="border-b border-white/5">
                    <th className="text-left py-3 px-4 text-sm font-medium text-[#888888]">Symbol</th>
                    <th className="text-center py-3 px-4 text-sm font-medium text-[#888888]">Status</th>
                    <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">Qty</th>
                    <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">Price</th>
                    <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">Total</th>
                    <th className="text-left py-3 px-4 text-sm font-medium text-[#888888]">Strategy</th>
                    <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">P&L</th>
                    <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">P&L %</th>
                    <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t) => (
                    <tr key={t.id} onClick={() => setSelectedTrade(t)} className="border-b border-white/5 hover:bg-white/5 transition-colors cursor-pointer">
                      <td className="py-3 px-4">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-[#e8e8e8]">{t.symbol}</span>
                          <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold uppercase tracking-wide ${
                            t.side === 'sell' || t.side === 'short'
                              ? 'bg-[#ff4466]/20 text-[#ff4466] border border-[#ff4466]/30'
                              : 'bg-[#00d4aa]/20 text-[#00d4aa] border border-[#00d4aa]/30'
                          }`}>{t.side === 'sell' || t.side === 'short' ? 'SELL' : 'BUY'}</span>
                          {t.leverage && t.leverage > 1 && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded font-bold bg-[#ffa500]/20 text-[#ffa500] border border-[#ffa500]/30">
                              {t.leverage}x
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="text-center py-3 px-4">
                        <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
                          t.is_open
                            ? 'bg-[#4a9eff]/15 text-[#4a9eff] border border-[#4a9eff]/30'
                            : 'bg-white/5 text-[#888888] border border-white/10'
                        }`}>
                          {t.is_open ? 'OPEN' : 'CLOSED'}
                        </span>
                      </td>
                      <td className="text-right py-3 px-4 text-[#c0c0c0]">{formatQty(t.qty)}</td>
                      <td className="text-right py-3 px-4 text-[#c0c0c0]">${t.price.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                      <td className="text-right py-3 px-4 text-[#c0c0c0]">${t.total.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                      <td className="py-3 px-4 text-[#888888] text-sm">{t.strategy}</td>
                      <td className={`text-right py-3 px-4 font-medium ${t.pnl !== null && !t.is_open ? (t.pnl >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]') : 'text-[#888888]'}`}>
                        {t.pnl !== null && !t.is_open ? `${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}` : t.is_open ? 'Active' : '-'}
                      </td>
                      <td className={`text-right py-3 px-4 font-medium ${t.pnl_pct !== null && !t.is_open ? (t.pnl_pct >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]') : 'text-[#888888]'}`}>
                        {t.pnl_pct !== null && !t.is_open ? `${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(2)}%` : '-'}
                      </td>
                      <td className="text-right py-3 px-4 text-[#888888] text-sm">{new Date(t.timestamp).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Position Cards with Volume */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {positions.length === 0 ? (
          <Card className="col-span-full"><CardContent className="p-8 text-center text-[#888888]">No open positions</CardContent></Card>
        ) : positions.map((pos, idx) => {
          const vol = volumes?.find(v => v.symbol === pos.symbol || v.aster_symbol === pos.symbol);
          return (
            <Card key={idx} className="hover:shadow-xl hover:shadow-[#4a9eff]/10 transition-all duration-300 cursor-pointer" onClick={() => {
              const match = trades?.find(t => t.symbol === pos.symbol && !t.closed_at);
              if (match) setSelectedTrade(match);
            }}>
              <CardContent className="p-6">
                <div className="flex items-center justify-between mb-4">
                  <div>
                    <p className="text-lg font-bold text-[#e8e8e8]">{pos.symbol}</p>
                    <p className="text-sm text-[#888888]">{pos.age || ''}</p>
                  </div>
                  <div className={`text-right ${(pos.unrealized_pnl || 0) >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]'}`}>
                    <p className="text-lg font-bold">{(pos.unrealized_pnl || 0) >= 0 ? '+' : ''}${(pos.unrealized_pnl || 0).toFixed(2)}</p>
                    <p className="text-sm">{(pos.unrealized_pnl_pct || 0) >= 0 ? '+' : ''}{(pos.unrealized_pnl_pct || 0).toFixed(2)}%</p>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3 text-sm mb-4">
                  <div>
                    <p className="text-[#888888]">Qty</p>
                    <p className="text-[#e8e8e8] font-medium">{formatQty(pos.qty)}</p>
                  </div>
                  <div>
                    <p className="text-[#888888]">Current</p>
                    <p className="text-[#e8e8e8] font-medium">${(pos.current_price || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</p>
                  </div>
                </div>
                {vol && (
                  <div className="border-t border-white/5 pt-3 space-y-2">
                    <div className="flex items-center gap-1 text-xs text-[#888888]">
                      <Volume2 className="size-3" />
                      Volume Analysis
                    </div>
                    <VolumeBar ratio={vol.ratio} label="Volume vs 7d avg" />
                    <div className="flex justify-between text-xs">
                      <span className="text-[#888888]">Trend: <span className={vol.trend > 0 ? 'text-[#00d4aa]' : vol.trend < -0.2 ? 'text-[#ff4466]' : 'text-[#c0c0c0]'}>
                        {vol.trend > 0 ? 'Building' : vol.trend < -0.2 ? 'Fading' : 'Stable'}
                      </span></span>
                      <span className="text-[#888888]">Spread: <span className={vol.spread_bps > 30 ? 'text-[#ffa500]' : 'text-[#c0c0c0]'}>{vol.spread_bps.toFixed(0)} bps</span></span>
                    </div>
                    <div className="flex justify-between text-xs">
                      <span className="text-[#888888]">Size mult: <span className="text-[#c0c0c0]">{vol.sizing_multiplier.toFixed(2)}x</span></span>
                      <span className="text-[#888888]">Quote vol: <span className="text-[#c0c0c0]">${vol.recent_quote_volume >= 1e6 ? `${(vol.recent_quote_volume / 1e6).toFixed(1)}M` : `${(vol.recent_quote_volume / 1e3).toFixed(0)}K`}</span></span>
                    </div>
                  </div>
                )}
                {(() => {
                  const mg = marginData?.find(m => m.symbol === pos.symbol);
                  if (!mg) return null;
                  const marginColor = mg.margin_distance > 0.2 ? 'bg-[#00d4aa]/15 text-[#00d4aa] border-[#00d4aa]/30'
                    : mg.margin_distance > 0.1 ? 'bg-[#ffa500]/15 text-[#ffa500] border-[#ffa500]/30'
                    : 'bg-[#ff4466]/15 text-[#ff4466] border-[#ff4466]/30';
                  const statusIcon = mg.status === 'critical' || mg.status === 'danger' ? '!' : mg.status === 'warning' ? '~' : '';
                  return (
                    <div className="border-t border-white/5 pt-3 space-y-2">
                      <div className="flex items-center gap-1 text-xs text-[#888888]">
                        <ShieldAlert className="size-3" />
                        Leverage &amp; Margin
                      </div>
                      <div className="flex items-center justify-between">
                        <span className="text-xs text-[#888888]">Leverage</span>
                        <span className="text-sm font-medium text-[#e8e8e8]">{mg.leverage.toFixed(1)}x</span>
                      </div>
                      <div className="flex items-center justify-between">
                        <span className="text-xs text-[#888888]">Margin Distance</span>
                        <span className={`text-xs px-2 py-0.5 rounded-full border font-medium ${marginColor}`}>
                          {statusIcon && <span className="mr-1">{statusIcon}</span>}
                          {(mg.margin_distance * 100).toFixed(1)}%
                        </span>
                      </div>
                      {mg.liq_price > 0 && (
                        <div className="flex items-center justify-between">
                          <span className="text-xs text-[#888888]">Liq. Price</span>
                          <span className="text-xs text-[#ff4466]">${mg.liq_price.toLocaleString('en-US', { minimumFractionDigits: 2 })}</span>
                        </div>
                      )}
                    </div>
                  );
                })()}
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* Trade Detail Modal */}
      <TradeDetailModal trade={selectedTrade} onClose={() => setSelectedTrade(null)} volumes={volumes || undefined} marginData={marginData || undefined} />
    </div>
  );
}
