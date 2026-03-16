import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { TrendingUp, TrendingDown, DollarSign, Volume2, RefreshCw, ArrowUpDown, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { api, usePolling, type StatusResponse, type Trade, type VolumeAnalysis, type MarginHealth } from "../config/api";

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

export function Trading() {
  const { data: status } = usePolling<StatusResponse>(api.status, 10000);
  const { data: trades } = usePolling<Trade[]>(api.trades, 15000);
  const { data: volumes } = usePolling<VolumeAnalysis[]>(api.volume, 30000);
  const { data: marginData } = usePolling<MarginHealth[]>(api.margin, 15000);
  const [selectedTrade, setSelectedTrade] = useState<Trade | null>(null);

  const positions = status?.positions || [];

  return (
    <div className="p-6 space-y-6">
      <div>
        <h2 className="text-3xl font-bold text-[#e8e8e8]">Trading Dashboard</h2>
        <p className="text-[#888888] mt-1">Real-time positions, trades, and volume analysis</p>
      </div>

      {/* Position Cards with Volume */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {positions.length === 0 ? (
          <Card className="col-span-full"><CardContent className="p-8 text-center text-[#888888]">No open positions</CardContent></Card>
        ) : positions.map((pos, idx) => {
          const vol = volumes?.find(v => v.symbol === pos.symbol || v.aster_symbol === pos.symbol);
          return (
            <Card key={idx} className="hover:shadow-xl hover:shadow-[#4a9eff]/10 transition-all duration-300">
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
                    <p className="text-[#e8e8e8] font-medium">{pos.qty}</p>
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
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-white/5">
                    <th className="text-left py-3 px-4 text-sm font-medium text-[#888888]">Symbol</th>
                    <th className="text-center py-3 px-4 text-sm font-medium text-[#888888]">Side</th>
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
                  {trades.slice(0, 25).map((t) => (
                    <tr key={t.id} onClick={() => setSelectedTrade(t)} className="border-b border-white/5 hover:bg-white/5 transition-colors cursor-pointer">
                      <td className="py-3 px-4 font-medium text-[#e8e8e8]">{t.symbol}</td>
                      <td className="py-3 px-4 text-center">
                        <Badge variant={t.side === 'buy' ? 'default' : 'destructive'}>{t.side.toUpperCase()}</Badge>
                      </td>
                      <td className="text-right py-3 px-4 text-[#c0c0c0]">{t.qty}</td>
                      <td className="text-right py-3 px-4 text-[#c0c0c0]">${t.price.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                      <td className="text-right py-3 px-4 text-[#c0c0c0]">${t.total.toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                      <td className="py-3 px-4 text-[#888888] text-sm">{t.strategy}</td>
                      <td className={`text-right py-3 px-4 font-medium ${t.pnl !== null ? (t.pnl >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]') : 'text-[#888888]'}`}>
                        {t.pnl !== null ? `${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}` : '-'}
                      </td>
                      <td className={`text-right py-3 px-4 font-medium ${t.pnl_pct !== null ? (t.pnl_pct >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]') : 'text-[#888888]'}`}>
                        {t.pnl_pct !== null ? `${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(2)}%` : '-'}
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

      {/* Trade Detail Modal */}
      <Dialog open={!!selectedTrade} onOpenChange={() => setSelectedTrade(null)}>
        <DialogContent className="bg-[#0a0a0a] border-white/8 text-[#e8e8e8] sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-3">
              <Badge variant={selectedTrade?.side === 'buy' ? 'default' : 'destructive'}>{selectedTrade?.side.toUpperCase()}</Badge>
              {selectedTrade?.symbol}
            </DialogTitle>
          </DialogHeader>
          {selectedTrade && (
            <div className="grid grid-cols-2 gap-2 sm:gap-4 mt-2 sm:mt-4">
              {[
                ['Strategy', selectedTrade.strategy],
                ['Quantity', selectedTrade.qty],
                ['Price', `$${selectedTrade.price.toFixed(2)}`],
                ['Total', `$${selectedTrade.total.toFixed(2)}`],
                ['P&L', selectedTrade.pnl !== null ? `${selectedTrade.pnl >= 0 ? '+' : ''}$${selectedTrade.pnl.toFixed(2)}` : 'Open'],
                ['P&L %', selectedTrade.pnl_pct !== null ? `${selectedTrade.pnl_pct.toFixed(2)}%` : '-'],
                ['Time', new Date(selectedTrade.timestamp).toLocaleString()],
                ['Closed', selectedTrade.closed_at ? new Date(selectedTrade.closed_at).toLocaleString() : 'Open'],
              ].map(([label, value]) => (
                <div key={String(label)} className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-xs sm:text-sm text-[#888888] mb-1">{label}</p>
                  <p className="text-base sm:text-lg font-bold truncate">{value}</p>
                </div>
              ))}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
