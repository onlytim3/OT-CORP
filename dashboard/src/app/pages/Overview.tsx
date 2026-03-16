import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Badge } from "../components/ui/badge";
import { MetricCard } from "../components/MetricCard";
import { DollarSign, TrendingUp, Activity, Layers, AlertCircle, TrendingDown, RefreshCw } from "lucide-react";
import { useState } from "react";
import { api, usePolling, isUsingMockData, type StatusResponse, type ActionItem } from "../config/api";

export function Overview() {
  const { data: status, loading } = usePolling<StatusResponse>(api.status, 10000);
  const { data: actions } = usePolling<ActionItem[]>(api.actions, 15000);
  const [selectedPosition, setSelectedPosition] = useState<StatusResponse['positions'][0] | null>(null);
  const [selectedActivity, setSelectedActivity] = useState<ActionItem | null>(null);

  const account = status?.account;
  const positions = status?.positions || [];
  const summary = status?.summary;
  const mode = status?.mode || 'paper';
  const positionPnl = positions.reduce((sum, p) => sum + (p.unrealized_pnl || 0), 0);
  const pnlSnapshot = (status as Record<string, unknown>)?.pnl_snapshot as { portfolio_value?: number; daily_return?: number; cumulative_return?: number } | undefined;
  // Use position P&L if available, otherwise fall back to daily_pnl cumulative return
  const totalPnl = positionPnl !== 0 ? positionPnl
    : pnlSnapshot?.cumulative_return && account?.portfolio_value
      ? pnlSnapshot.cumulative_return * account.portfolio_value
      : 0;
  const pnlPct = positionPnl !== 0
    ? (account?.portfolio_value ? (positionPnl / account.portfolio_value) * 100 : 0)
    : (pnlSnapshot?.cumulative_return ? pnlSnapshot.cumulative_return * 100 : 0);
  const isMock = isUsingMockData();

  if (loading && !status) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="size-8 text-[#4a9eff] animate-spin" />
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      {isMock && (
        <div className="bg-[#4a9eff]/10 border border-[#4a9eff]/30 rounded-lg p-4">
          <div className="flex items-center gap-2">
            <AlertCircle className="size-5 text-[#4a9eff]" />
            <div>
              <p className="text-[#e8e8e8] font-medium">Using Demo Data</p>
              <p className="text-[#c0c0c0] text-sm mt-1">
                Start your backend: <code className="bg-[#1a1a1a] px-2 py-1 rounded border border-white/10">python -m trading.main dashboard</code>
              </p>
            </div>
          </div>
        </div>
      )}

      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-3xl font-bold text-[#e8e8e8]">Dashboard Overview</h2>
          <p className="text-[#888888] mt-1">Monitor your trading operations and strategy performance</p>
        </div>
        <div className={`px-4 py-2 rounded-lg text-sm font-medium backdrop-blur-sm border ${
          mode === 'paper'
            ? 'bg-[#4a9eff]/10 text-[#4a9eff] border-[#4a9eff]/30'
            : 'bg-[#00d4aa]/10 text-[#00d4aa] border-[#00d4aa]/30'
        }`}>
          {mode.toUpperCase()} MODE
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        <MetricCard
          title="Portfolio Value"
          value={`$${(account?.portfolio_value || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
          icon={DollarSign}
          iconColor="text-[#00d4aa]"
        />
        <MetricCard
          title="Unrealized P&L"
          value={`${totalPnl >= 0 ? '+' : ''}$${totalPnl.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
          change={pnlPct !== 0 ? Number(pnlPct.toFixed(2)) : undefined}
          icon={totalPnl >= 0 ? TrendingUp : TrendingDown}
          iconColor={totalPnl >= 0 ? "text-[#00d4aa]" : "text-[#ff4466]"}
        />
        <MetricCard title="Open Positions" value={summary?.open_positions ?? positions.length} icon={Activity} iconColor="text-[#4a9eff]" />
        <MetricCard title="Active Strategies" value={summary?.active_strategies ?? 0} icon={Layers} iconColor="text-[#c0c0c0]" />
      </div>

      {/* Positions Table */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between">
            Open Positions
            <span className="text-sm font-normal text-[#888888]">{positions.length} positions</span>
          </CardTitle>
        </CardHeader>
        <CardContent>
          {positions.length === 0 ? (
            <p className="text-[#888888] text-center py-8">No open positions</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-white/5">
                    {['Symbol', 'Qty', 'Entry', 'Current', 'P&L', 'P&L %', 'Age'].map(h => (
                      <th key={h} className={`${h === 'Symbol' ? 'text-left' : 'text-right'} py-3 px-4 text-sm font-medium text-[#888888]`}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {positions.map((pos, idx) => (
                    <tr key={idx} onClick={() => setSelectedPosition(pos)} className="border-b border-white/5 hover:bg-white/5 transition-colors cursor-pointer">
                      <td className="py-3 px-4 font-medium text-[#e8e8e8]">{pos.symbol}</td>
                      <td className="text-right py-3 px-4 text-[#c0c0c0]">{pos.qty}</td>
                      <td className="text-right py-3 px-4 text-[#c0c0c0]">${(pos.avg_cost || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                      <td className="text-right py-3 px-4 text-[#c0c0c0]">${(pos.current_price || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</td>
                      <td className={`text-right py-3 px-4 font-medium ${(pos.unrealized_pnl || 0) >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]'}`}>
                        {(pos.unrealized_pnl || 0) >= 0 ? '+' : ''}${(pos.unrealized_pnl || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}
                      </td>
                      <td className={`text-right py-3 px-4 ${(pos.unrealized_pnl_pct || 0) >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]'}`}>
                        {(pos.unrealized_pnl_pct || 0) >= 0 ? '+' : ''}{(pos.unrealized_pnl_pct || 0).toFixed(2)}%
                      </td>
                      <td className="text-right py-3 px-4 text-[#888888] text-sm">{pos.age || '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Position Detail Modal */}
      <Dialog open={!!selectedPosition} onOpenChange={() => setSelectedPosition(null)}>
        <DialogContent className="bg-[#0a0a0a] border-white/8 text-[#e8e8e8] sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="text-xl sm:text-2xl flex items-center gap-3">
              <div className="p-2 rounded-lg bg-[#4a9eff]/15 border border-[#4a9eff]/30">
                <TrendingUp className="size-5 sm:size-6 text-[#4a9eff]" />
              </div>
              {selectedPosition?.symbol}
            </DialogTitle>
          </DialogHeader>
          {selectedPosition && (
            <div className="grid grid-cols-2 gap-2 sm:gap-4 mt-2 sm:mt-4">
              {[
                ['Quantity', selectedPosition.qty],
                ['Entry', `$${(selectedPosition.avg_cost || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}`],
                ['Current', `$${(selectedPosition.current_price || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}`],
                ['Market Value', `$${(selectedPosition.market_value || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}`],
                ['P&L', `${(selectedPosition.unrealized_pnl || 0) >= 0 ? '+' : ''}$${(selectedPosition.unrealized_pnl || 0).toFixed(2)}`],
                ['P&L %', `${(selectedPosition.unrealized_pnl_pct || 0) >= 0 ? '+' : ''}${(selectedPosition.unrealized_pnl_pct || 0).toFixed(2)}%`],
                ['Age', selectedPosition.age || 'N/A'],
              ].map(([label, value]) => (
                <div key={String(label)} className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-xs sm:text-sm text-[#888888] mb-1">{label}</p>
                  <p className="text-base sm:text-lg font-bold">{value}</p>
                </div>
              ))}
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* Recent Activity */}
      <Card>
        <CardHeader><CardTitle>Recent Activity</CardTitle></CardHeader>
        <CardContent>
          {(!actions || actions.length === 0) ? (
            <p className="text-[#888888] text-center py-8">No recent activity</p>
          ) : (
            <div className="space-y-1">
              {actions.slice(0, 15).map((a) => (
                <div key={a.id} onClick={() => setSelectedActivity(a)}
                  className="flex items-center justify-between py-3 border-b border-white/5 last:border-0 hover:bg-white/5 transition-colors cursor-pointer rounded px-2">
                  <div className="flex items-center gap-4">
                    <div className={`size-2 rounded-full ${
                      a.category === 'trade' ? 'bg-[#00d4aa]' : a.category === 'error' ? 'bg-[#ff4466]' : 'bg-[#4a9eff]'
                    }`} />
                    <div>
                      <p className="font-medium text-[#e8e8e8] text-sm">{a.action}</p>
                      {a.details && <p className="text-xs text-[#888888] line-clamp-1">{a.details}</p>}
                    </div>
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    <Badge variant="secondary" className="text-xs">{a.category}</Badge>
                    <p className="text-xs text-[#888888]">{new Date(a.timestamp).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Activity Detail Modal */}
      <Dialog open={!!selectedActivity} onOpenChange={() => setSelectedActivity(null)}>
        <DialogContent className="bg-[#0a0a0a] border-white/8 text-[#e8e8e8] sm:max-w-2xl">
          <DialogHeader><DialogTitle>{selectedActivity?.action}</DialogTitle></DialogHeader>
          {selectedActivity && (
            <div className="space-y-3 sm:space-y-4 mt-2 sm:mt-4">
              {selectedActivity.details && (
                <div className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-xs sm:text-sm text-[#888888] mb-1">Details</p>
                  <p className="text-sm sm:text-base text-[#c0c0c0]">{selectedActivity.details}</p>
                </div>
              )}
              <div className="grid grid-cols-2 gap-2 sm:gap-4">
                <div className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-xs sm:text-sm text-[#888888] mb-1">Category</p>
                  <Badge>{selectedActivity.category}</Badge>
                </div>
                <div className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-xs sm:text-sm text-[#888888] mb-1">Time</p>
                  <p className="text-xs sm:text-sm">{new Date(selectedActivity.timestamp).toLocaleString()}</p>
                </div>
              </div>
              {selectedActivity.data && (
                <div className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-xs sm:text-sm text-[#888888] mb-1">Data</p>
                  <pre className="text-xs text-[#c0c0c0] overflow-x-auto max-h-40 whitespace-pre-wrap break-all">{JSON.stringify(selectedActivity.data, null, 2)}</pre>
                </div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
