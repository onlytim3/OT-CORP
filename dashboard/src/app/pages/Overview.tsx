import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Badge } from "../components/ui/badge";
import { MetricCard } from "../components/MetricCard";
import { DollarSign, TrendingUp, Activity, Layers, AlertCircle, TrendingDown, RefreshCw, Gauge, PieChart } from "lucide-react";
import { useState, useEffect } from "react";
import { api, usePolling, isUsingMockData, fetchAPI, type StatusResponse, type ActionItem, type ActionDetail, type AggregatedLeverage, type SectorExposure } from "../config/api";

function formatQty(qty: number): string {
  if (qty === 0) return '0';
  const abs = Math.abs(qty);
  if (abs >= 100) return qty.toLocaleString('en-US', { maximumFractionDigits: 2 });
  if (abs >= 1) return qty.toLocaleString('en-US', { maximumFractionDigits: 4 });
  return Number(qty.toPrecision(6)).toString();
}

/** Convert raw system details into human-readable text */
function humanizeDetails(action: string, details: string): string {
  if (!details) return '';

  // Funnel pattern: Gen:X Con:Y Act:Z Exec:N
  const funnel = details.match(/Gen:(\d+)\s*Con:(\d+)\s*Act:(\d+)\s*Exec:(\d+)/);
  if (funnel) {
    const [, gen, con, , exec] = funnel;
    return `${gen} signals → ${con} consolidated → ${exec} executed`;
  }

  // Sync positions: "Synced X positions"
  const sync = details.match(/[Ss]ynced?\s+(\d+)\s+positions?/);
  if (sync) return `Synchronized ${sync[1]} positions with exchange`;

  // Pair trades: "Paired X trades"
  const pair = details.match(/[Pp]aired?\s+(\d+)\s+trades?/);
  if (pair) return `Matched ${pair[1]} trade pairs for P&L`;

  // Cycle complete: "Raw: X | Consolidated: Y | Executed: Z"
  const cycle = details.match(/Raw:\s*(\d+)\s*\|\s*Consolidated:\s*(\d+)\s*\|\s*Executed?:\s*(\d+)/i);
  if (cycle) {
    return `${cycle[1]} signals → ${cycle[2]} consolidated → ${cycle[3]} executed`;
  }

  // Dollar amounts
  if (/^\$[\d.]+$/.test(details.trim())) return `Trade value: ${details.trim()}`;

  return details;
}

function ActivityDetailModal({ activity, onClose }: { activity: ActionItem | null; onClose: () => void }) {
  const [detail, setDetail] = useState<ActionDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [showRawData, setShowRawData] = useState(false);

  useEffect(() => {
    if (activity?.id) {
      setLoading(true);
      setDetail(null);
      fetchAPI<ActionDetail>(api.actionDetail(activity.id))
        .then(setDetail)
        .catch(() => setDetail(null))
        .finally(() => setLoading(false));
    } else {
      setDetail(null);
    }
  }, [activity]);

  const qualityColor = (score: number | null) => {
    if (score === null || score === undefined) return { border: 'border-white/10', bg: 'bg-white/5', label: '', text: 'text-[#888888]' };
    if (score > 0.7) return { border: 'border-[#00d4aa]/30', bg: 'bg-[#00d4aa]/10', label: 'Good Decision', text: 'text-[#00d4aa]' };
    if (score > 0.4) return { border: 'border-[#ffa500]/30', bg: 'bg-[#ffa500]/10', label: 'Neutral', text: 'text-[#ffa500]' };
    return { border: 'border-[#ff4466]/30', bg: 'bg-[#ff4466]/10', label: 'Needs Review', text: 'text-[#ff4466]' };
  };

  return (
    <Dialog open={!!activity} onOpenChange={() => onClose()}>
      <DialogContent className="bg-[#0a0a0a] border-white/8 text-[#e8e8e8] sm:max-w-3xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-3">
            {activity?.action}
            {activity && <Badge variant="secondary" className="text-xs">{activity.category}</Badge>}
          </DialogTitle>
        </DialogHeader>
        {activity && (
          <div className="space-y-4 mt-2 sm:mt-4">
            {/* Narrative Section */}
            {loading ? (
              <div className="p-4 rounded-lg bg-white/5 border border-white/10">
                <p className="text-sm text-[#888888] animate-pulse">Analyzing activity...</p>
              </div>
            ) : detail?.narrative ? (
              <div className="p-4 rounded-lg bg-white/5 border border-white/10">
                <p className="text-sm text-[#d4d4d4] leading-[1.7]">{detail.narrative}</p>
              </div>
            ) : activity.details ? (
              <div className="p-4 rounded-lg bg-white/5 border border-white/10">
                <p className="text-sm text-[#d4d4d4] leading-[1.7]">{humanizeDetails(activity.action, activity.details)}</p>
              </div>
            ) : null}

            {/* Assessment Card */}
            {detail?.interpretation?.assessment && (
              <div className={`p-4 rounded-lg ${qualityColor(detail.quality_score).bg} border ${qualityColor(detail.quality_score).border}`}>
                <div className="flex items-center justify-between mb-2">
                  <p className="text-xs text-[#888888] font-medium uppercase tracking-wider">Assessment</p>
                  {detail.quality_score !== null && (
                    <span className={`text-xs font-medium px-2 py-0.5 rounded ${qualityColor(detail.quality_score).text} ${qualityColor(detail.quality_score).bg}`}>
                      {qualityColor(detail.quality_score).label} ({(detail.quality_score * 100).toFixed(0)}%)
                    </span>
                  )}
                </div>
                <p className="text-sm text-[#d4d4d4] leading-relaxed">{detail.interpretation.assessment}</p>
                {detail.interpretation.impact && (
                  <p className="text-sm text-[#888888] mt-2 leading-relaxed"><span className="text-[#c0c0c0] font-medium">Impact:</span> {detail.interpretation.impact}</p>
                )}
              </div>
            )}

            {/* Lessons */}
            {detail?.lessons && detail.lessons.length > 0 && (
              <div>
                <p className="text-xs text-[#888888] font-medium uppercase tracking-wider mb-2">Lessons Learned</p>
                <div className="flex flex-wrap gap-2">
                  {detail.lessons.map((lesson, i) => (
                    <span key={i} className="text-xs px-3 py-1.5 rounded-full bg-[#4a9eff]/10 border border-[#4a9eff]/20 text-[#4a9eff]">
                      {lesson}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Meta Info */}
            <div className="grid grid-cols-2 gap-2">
              <div className="p-3 rounded-lg bg-white/5 border border-white/10">
                <p className="text-xs text-[#888888] mb-1">Category</p>
                <Badge>{activity.category}</Badge>
              </div>
              <div className="p-3 rounded-lg bg-white/5 border border-white/10">
                <p className="text-xs text-[#888888] mb-1">Time</p>
                <p className="text-xs">{new Date(activity.timestamp).toLocaleString()}</p>
              </div>
            </div>

            {/* Raw Data (Collapsible) */}
            {activity.data && (
              <div className="rounded-lg bg-white/5 border border-white/10 overflow-hidden">
                <button
                  onClick={() => setShowRawData(!showRawData)}
                  className="w-full flex items-center justify-between px-4 py-2.5 text-left hover:bg-white/5 transition-colors"
                >
                  <p className="text-xs text-[#888888] font-medium">Technical Details</p>
                  <span className="text-xs text-[#888888]">{showRawData ? '▼' : '▶'}</span>
                </button>
                {showRawData && (
                  <div className="px-4 pb-3">
                    <pre className="text-xs text-[#c0c0c0] overflow-x-auto max-h-40 whitespace-pre-wrap break-all">{JSON.stringify(activity.data, null, 2)}</pre>
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

export function Overview() {
  const { data: status, loading } = usePolling<StatusResponse>(api.status, 10000);
  const { data: actions } = usePolling<ActionItem[]>(api.actions, 15000);
  const { data: leverageData } = usePolling<AggregatedLeverage>(api.leverage, 15000);
  const { data: sectors } = usePolling<SectorExposure[]>(api.sectors, 30000);
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
        <div className="bg-[#4a9eff]/10 border border-[#4a9eff]/20 rounded-xl p-4 backdrop-blur-sm">
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

      {/* Aggregate Leverage & Sector Exposure */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Aggregate Leverage Gauge */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Gauge className="size-5 text-[#ffa500]" />
              Aggregate Leverage
            </CardTitle>
          </CardHeader>
          <CardContent>
            {(() => {
              const lev = leverageData?.aggregate_leverage ?? 0;
              const levColor = lev < 2 ? '#00d4aa' : lev < 4 ? '#ffa500' : '#ff4466';
              const levLabel = lev < 2 ? 'Conservative' : lev < 4 ? 'Moderate' : 'Aggressive';
              return (
                <div className="flex flex-col items-center gap-4 py-4">
                  <div className="relative size-32">
                    <svg viewBox="0 0 100 60" className="w-full">
                      <path d="M 10 50 A 40 40 0 0 1 90 50" fill="none" stroke="rgba(255,255,255,0.05)" strokeWidth="8" strokeLinecap="round" />
                      <path d="M 10 50 A 40 40 0 0 1 90 50" fill="none" stroke={levColor} strokeWidth="8" strokeLinecap="round"
                        strokeDasharray={`${Math.min((lev / 6) * 126, 126)} 126`} />
                    </svg>
                    <div className="absolute inset-0 flex flex-col items-center justify-end pb-1">
                      <p className="text-3xl font-bold text-[#e8e8e8]">{lev.toFixed(1)}x</p>
                    </div>
                  </div>
                  <p className="text-sm font-medium" style={{ color: levColor }}>{levLabel}</p>
                  <div className="grid grid-cols-2 gap-4 w-full text-sm">
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10 text-center">
                      <p className="text-xs text-[#888888]">Total Notional</p>
                      <p className="font-medium text-[#e8e8e8]">${(leverageData?.total_notional ?? 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</p>
                    </div>
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10 text-center">
                      <p className="text-xs text-[#888888]">Portfolio Value</p>
                      <p className="font-medium text-[#e8e8e8]">${(leverageData?.portfolio_value ?? 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</p>
                    </div>
                  </div>
                </div>
              );
            })()}
          </CardContent>
        </Card>

        {/* Sector Exposure */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <PieChart className="size-5 text-[#4a9eff]" />
              Sector Exposure
            </CardTitle>
          </CardHeader>
          <CardContent>
            {(!sectors || sectors.length === 0) ? (
              <p className="text-[#888888] text-center py-8">No sector data available</p>
            ) : (
              <div className="space-y-3">
                {sectors.map((s) => {
                  const isOverLimit = s.exposure_pct > s.limit_pct;
                  const barColor = isOverLimit ? '#ff4466' : s.exposure_pct > s.limit_pct * 0.8 ? '#ffa500' : '#4a9eff';
                  return (
                    <div key={s.sector} className="space-y-1">
                      <div className="flex items-center justify-between text-sm">
                        <span className="text-[#e8e8e8] font-medium">{s.sector}</span>
                        <div className="flex items-center gap-2">
                          <span style={{ color: barColor }} className="font-medium">{s.exposure_pct.toFixed(1)}%</span>
                          <span className="text-[#888888] text-xs">/ {s.limit_pct.toFixed(0)}% limit</span>
                          {isOverLimit && <Badge variant="destructive" className="text-[10px] px-1.5 py-0">OVER</Badge>}
                        </div>
                      </div>
                      <div className="h-2 bg-white/5 rounded-full overflow-hidden relative">
                        <div className="h-full rounded-full transition-all" style={{ width: `${Math.min(s.exposure_pct, 100)}%`, backgroundColor: barColor, opacity: 0.8 }} />
                        {s.limit_pct < 100 && (
                          <div className="absolute top-0 h-full w-0.5 bg-white/30" style={{ left: `${s.limit_pct}%` }} />
                        )}
                      </div>
                      <p className="text-xs text-[#888888]">{s.positions.join(', ')}</p>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
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
                      <td className="text-right py-3 px-4 text-[#c0c0c0]">{formatQty(pos.qty)}</td>
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
            <div className="space-y-1 max-h-[50vh] overflow-y-auto">
              {actions.map((a) => (
                <div key={a.id} onClick={() => setSelectedActivity(a)}
                  className="flex items-center justify-between py-3 border-b border-white/5 last:border-0 hover:bg-white/5 transition-colors cursor-pointer rounded px-2">
                  <div className="flex items-center gap-4">
                    <div className={`size-2 rounded-full ${
                      a.category === 'trade' ? 'bg-[#00d4aa]' : a.category === 'error' ? 'bg-[#ff4466]' : 'bg-[#4a9eff]'
                    }`} />
                    <div>
                      <p className="font-medium text-[#e8e8e8] text-sm">{a.action}</p>
                      {a.details && <p className="text-xs text-[#888888] line-clamp-1">{humanizeDetails(a.action, a.details)}</p>}
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
      <ActivityDetailModal activity={selectedActivity} onClose={() => setSelectedActivity(null)} />
    </div>
  );
}
