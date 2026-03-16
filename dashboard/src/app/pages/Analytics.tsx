import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import {
  BarChart, Bar, LineChart, Line, AreaChart, Area,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell, ReferenceLine,
} from "recharts";
import { Brain, TrendingUp, Shield, Gauge, Calendar, DollarSign, Activity, Filter, Target, Clock, Crosshair, BarChart3 } from "lucide-react";
import { useState } from "react";
import {
  api, usePolling,
  type Intelligence, type Strategy, type StatusResponse, type PnlHistory,
  type FillQuality, type StrategyAttribution, type FunnelData, type TimePnl,
} from "../config/api";

function FearGreedGauge({ value, label }: { value: number; label: string }) {
  const color = value <= 25 ? '#ff4466' : value <= 45 ? '#ffa500' : value <= 55 ? '#c0c0c0' : value <= 75 ? '#00d4aa' : '#00d4aa';
  return (
    <div className="flex flex-col items-center gap-3">
      <div className="relative size-32">
        <svg viewBox="0 0 100 60" className="w-full">
          <path d="M 10 50 A 40 40 0 0 1 90 50" fill="none" stroke="rgba(255,255,255,0.05)" strokeWidth="8" strokeLinecap="round" />
          <path d="M 10 50 A 40 40 0 0 1 90 50" fill="none" stroke={color} strokeWidth="8" strokeLinecap="round"
            strokeDasharray={`${(value / 100) * 126} 126`} />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-end pb-1">
          <p className="text-3xl font-bold text-[#e8e8e8]">{value}</p>
        </div>
      </div>
      <p className="text-sm font-medium" style={{ color }}>{label}</p>
    </div>
  );
}

const tooltipStyle = { backgroundColor: '#1a1a1a', border: '1px solid rgba(192,192,192,0.2)', borderRadius: '8px', color: '#e8e8e8' };

export function Analytics() {
  const { data: intelligence } = usePolling<Intelligence>(api.intelligence, 30000);
  const { data: strategies } = usePolling<Strategy[]>(api.strategies, 30000);
  const { data: status } = usePolling<StatusResponse>(api.status, 15000);
  const { data: pnlHistory } = usePolling<PnlHistory>(api.pnlHistory, 60000);
  const { data: corrData } = usePolling<{matrix: Record<string, Record<string, number>>, alerts: string[]}>(api.correlationMatrix, 60000);
  const { data: funnelData } = usePolling<FunnelData[]>(api.funnel, 30000);
  const { data: timePnl } = usePolling<TimePnl>(api.timePnl, 60000);
  const { data: fills } = usePolling<FillQuality[]>(api.fillAnalysis, 30000);
  const { data: attribution } = usePolling<StrategyAttribution[]>(api.attribution, 60000);
  const [periodTab, setPeriodTab] = useState<'daily' | 'weekly' | 'monthly'>('daily');

  const fearGreed = intelligence?.fear_greed;
  const briefings = intelligence?.briefings || [];
  const regimeSignals = intelligence?.regime_signals || [];

  // Build strategy chart data
  const strategyChartData = (strategies || [])
    .filter(s => s.trades > 0)
    .sort((a, b) => b.total_pnl - a.total_pnl)
    .slice(0, 10)
    .map(s => ({
      name: s.name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
      shortName: s.name.length > 12 ? s.name.slice(0, 12) + '...' : s.name,
      pnl: Number(s.total_pnl.toFixed(2)),
      trades: s.trades,
      winRate: s.win_rate || 0,
    }));

  // Portfolio stats
  const totalPnl = (strategies || []).reduce((s, st) => s + st.total_pnl, 0);
  const totalTrades = (strategies || []).reduce((s, st) => s + st.trades, 0);
  const withWinRate = (strategies || []).filter(s => s.win_rate !== null && s.win_rate !== undefined);
  const avgWinRate = withWinRate.length > 0
    ? withWinRate.reduce((s, st) => s + (st.win_rate || 0), 0) / withWinRate.length
    : 0;

  // P&L chart data
  const daily = pnlHistory?.daily || [];
  const weekly = pnlHistory?.weekly || [];
  const monthly = pnlHistory?.monthly || [];

  const portfolioChartData = daily.map(d => ({
    date: d.date.slice(5), // MM-DD
    value: Number((d.portfolio_value || 0).toFixed(2)),
    return_pct: Number(((d.daily_return || 0) * 100).toFixed(2)),
    cumulative: Number(((d.cumulative_return || 0) * 100).toFixed(2)),
  }));

  const dailyReturnData = daily.map(d => ({
    date: d.date.slice(5),
    return_pct: Number(((d.daily_return || 0) * 100).toFixed(2)),
  }));

  const weeklyData = weekly.map(w => ({
    period: w.period.replace(/^\d{4}-/, ''),
    return_pct: w.return_pct,
    trades: w.trades,
  }));

  const monthlyData = monthly.map(m => ({
    period: m.period.slice(2), // YY-MM
    return_pct: m.return_pct,
    trades: m.trades,
  }));

  // Compute period summary stats
  const currentPeriodData = periodTab === 'daily' ? dailyReturnData : periodTab === 'weekly' ? weeklyData : monthlyData;
  const positiveCount = currentPeriodData.filter(d => d.return_pct > 0).length;
  const negativeCount = currentPeriodData.filter(d => d.return_pct < 0).length;
  const bestReturn = currentPeriodData.length > 0 ? Math.max(...currentPeriodData.map(d => d.return_pct)) : 0;
  const worstReturn = currentPeriodData.length > 0 ? Math.min(...currentPeriodData.map(d => d.return_pct)) : 0;

  return (
    <div className="p-6 space-y-6">
      <div>
        <h2 className="text-3xl font-bold text-[#e8e8e8]">Analytics</h2>
        <p className="text-[#888888] mt-1">Performance insights, market intelligence, and risk metrics</p>
      </div>

      {/* Key Metrics */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
        <Card>
          <CardContent className="p-4 sm:p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs sm:text-sm text-[#888888]">Total P&L</p>
                <p className={`text-xl sm:text-2xl font-bold mt-1 ${totalPnl >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]'}`}>
                  {totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}
                </p>
              </div>
              <div className="hidden sm:block p-3 rounded-lg bg-[#00d4aa]/10 border border-[#00d4aa]/30">
                <TrendingUp className="size-6 text-[#00d4aa]" />
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 sm:p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs sm:text-sm text-[#888888]">Total Trades</p>
                <p className="text-xl sm:text-2xl font-bold text-[#e8e8e8] mt-1">{totalTrades}</p>
              </div>
              <div className="hidden sm:block p-3 rounded-lg bg-[#4a9eff]/10 border border-[#4a9eff]/30">
                <Gauge className="size-6 text-[#4a9eff]" />
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 sm:p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs sm:text-sm text-[#888888]">Avg Win Rate</p>
                <p className="text-xl sm:text-2xl font-bold text-[#e8e8e8] mt-1">{avgWinRate.toFixed(1)}%</p>
              </div>
              <div className="hidden sm:block p-3 rounded-lg bg-[#c0c0c0]/10 border border-[#c0c0c0]/30">
                <Shield className="size-6 text-[#c0c0c0]" />
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 sm:p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs sm:text-sm text-[#888888]">Fear & Greed</p>
                {fearGreed ? (
                  <p className="text-xl sm:text-2xl font-bold text-[#e8e8e8] mt-1">{fearGreed.value} <span className="text-xs sm:text-sm font-normal text-[#888888]">{fearGreed.classification}</span></p>
                ) : (
                  <p className="text-xl sm:text-2xl font-bold text-[#888888] mt-1">N/A</p>
                )}
              </div>
              <div className="hidden sm:block p-3 rounded-lg bg-[#ffa500]/10 border border-[#ffa500]/30">
                <Brain className="size-6 text-[#ffa500]" />
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      <Tabs defaultValue="performance" className="space-y-6">
        <TabsList className="flex-wrap">
          <TabsTrigger value="performance">P&L Performance</TabsTrigger>
          <TabsTrigger value="strategies">Strategies</TabsTrigger>
          <TabsTrigger value="intelligence">Intelligence</TabsTrigger>
          <TabsTrigger value="regime">Regime Signals</TabsTrigger>
          <TabsTrigger value="funnel">Signal Funnel</TabsTrigger>
          <TabsTrigger value="fills">Fill Quality</TabsTrigger>
          <TabsTrigger value="correlation">Correlation</TabsTrigger>
        </TabsList>

        {/* P&L Performance Tab */}
        <TabsContent value="performance" className="space-y-6">
          {/* Portfolio Value Over Time */}
          {portfolioChartData.length > 1 ? (
            <>
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <DollarSign className="size-5 text-[#4a9eff]" />
                    Portfolio Value
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <ResponsiveContainer width="100%" height={300}>
                    <AreaChart data={portfolioChartData} margin={{ top: 5, right: 20, bottom: 5, left: 10 }}>
                      <defs>
                        <linearGradient id="valueGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor="#4a9eff" stopOpacity={0.3} />
                          <stop offset="95%" stopColor="#4a9eff" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgba(192,192,192,0.08)" />
                      <XAxis dataKey="date" stroke="#888888" tick={{ fontSize: 11 }} />
                      <YAxis stroke="#888888" tick={{ fontSize: 11 }} tickFormatter={(v) => `$${v}`} />
                      <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => [`$${v.toFixed(2)}`, 'Portfolio']} />
                      <Area type="monotone" dataKey="value" stroke="#4a9eff" fill="url(#valueGrad)" strokeWidth={2} />
                    </AreaChart>
                  </ResponsiveContainer>
                </CardContent>
              </Card>

              {/* Cumulative Return */}
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <TrendingUp className="size-5 text-[#00d4aa]" />
                    Cumulative Return
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <ResponsiveContainer width="100%" height={250}>
                    <LineChart data={portfolioChartData} margin={{ top: 5, right: 20, bottom: 5, left: 10 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgba(192,192,192,0.08)" />
                      <XAxis dataKey="date" stroke="#888888" tick={{ fontSize: 11 }} />
                      <YAxis stroke="#888888" tick={{ fontSize: 11 }} tickFormatter={(v) => `${v}%`} />
                      <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => [`${v.toFixed(2)}%`, 'Cumulative']} />
                      <ReferenceLine y={0} stroke="rgba(192,192,192,0.2)" />
                      <Line type="monotone" dataKey="cumulative" stroke="#00d4aa" strokeWidth={2} dot={false} />
                    </LineChart>
                  </ResponsiveContainer>
                </CardContent>
              </Card>

              {/* Period Returns */}
              <Card>
                <CardHeader>
                  <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                    <CardTitle className="flex items-center gap-2">
                      <Calendar className="size-5 text-[#ffa500]" />
                      Period Returns
                    </CardTitle>
                    <div className="flex gap-1 bg-white/5 rounded-lg p-1">
                      {(['daily', 'weekly', 'monthly'] as const).map(p => (
                        <button key={p} onClick={() => setPeriodTab(p)}
                          className={`px-3 py-1 text-xs rounded-md transition-colors ${periodTab === p ? 'bg-[#4a9eff]/20 text-[#4a9eff]' : 'text-[#888888] hover:text-[#e8e8e8]'}`}>
                          {p.charAt(0).toUpperCase() + p.slice(1)}
                        </button>
                      ))}
                    </div>
                  </div>
                </CardHeader>
                <CardContent>
                  {/* Period summary stats */}
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10">
                      <p className="text-xs text-[#888888]">Periods</p>
                      <p className="text-lg font-bold text-[#e8e8e8]">{currentPeriodData.length}</p>
                    </div>
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10">
                      <p className="text-xs text-[#888888]">Win / Loss</p>
                      <p className="text-lg font-bold"><span className="text-[#00d4aa]">{positiveCount}</span> <span className="text-[#888888]">/</span> <span className="text-[#ff4466]">{negativeCount}</span></p>
                    </div>
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10">
                      <p className="text-xs text-[#888888]">Best</p>
                      <p className="text-lg font-bold text-[#00d4aa]">+{bestReturn.toFixed(2)}%</p>
                    </div>
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10">
                      <p className="text-xs text-[#888888]">Worst</p>
                      <p className="text-lg font-bold text-[#ff4466]">{worstReturn.toFixed(2)}%</p>
                    </div>
                  </div>

                  <ResponsiveContainer width="100%" height={250}>
                    <BarChart data={currentPeriodData} margin={{ top: 5, right: 20, bottom: 5, left: 10 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgba(192,192,192,0.08)" />
                      <XAxis dataKey={periodTab === 'daily' ? 'date' : 'period'} stroke="#888888" tick={{ fontSize: 10 }} />
                      <YAxis stroke="#888888" tick={{ fontSize: 11 }} tickFormatter={(v) => `${v}%`} />
                      <Tooltip contentStyle={tooltipStyle}
                        formatter={(v: number, name: string) => [
                          name === 'return_pct' ? `${v.toFixed(2)}%` : v,
                          name === 'return_pct' ? 'Return' : 'Trades'
                        ]} />
                      <ReferenceLine y={0} stroke="rgba(192,192,192,0.3)" />
                      <Bar dataKey="return_pct" radius={[4, 4, 0, 0]}>
                        {currentPeriodData.map((entry, i) => (
                          <Cell key={i} fill={entry.return_pct >= 0 ? '#00d4aa' : '#ff4466'} fillOpacity={0.8} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </CardContent>
              </Card>
            </>
          ) : (
            <Card>
              <CardContent className="p-12 text-center">
                <Activity className="size-12 text-[#888888]/30 mx-auto mb-4" />
                <p className="text-[#888888]">P&L history will appear here once the system has daily data</p>
                <p className="text-xs text-[#666666] mt-1">Portfolio snapshots are recorded at the end of each trading cycle</p>
              </CardContent>
            </Card>
          )}
        </TabsContent>

        {/* Strategy Performance Tab */}
        <TabsContent value="strategies" className="space-y-6">
          {strategyChartData.length > 0 && (
            <Card>
              <CardHeader><CardTitle>Strategy P&L Comparison</CardTitle></CardHeader>
              <CardContent>
                <ResponsiveContainer width="100%" height={350}>
                  <BarChart data={strategyChartData} layout="vertical" margin={{ left: 20, right: 20 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(192,192,192,0.1)" />
                    <XAxis type="number" stroke="#888888" />
                    <YAxis type="category" dataKey="shortName" stroke="#888888" width={100} tick={{ fontSize: 12 }} />
                    <Tooltip contentStyle={tooltipStyle}
                      formatter={(value: number) => [`$${value.toFixed(2)}`, 'P&L']} />
                    <Bar dataKey="pnl" radius={[0, 8, 8, 0]}>
                      {strategyChartData.map((entry, i) => (
                        <Cell key={i} fill={entry.pnl >= 0 ? '#00d4aa' : '#ff4466'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>
          )}

          {fearGreed && (
            <Card>
              <CardHeader><CardTitle>Market Sentiment</CardTitle></CardHeader>
              <CardContent className="flex justify-center py-6">
                <FearGreedGauge value={fearGreed.value} label={fearGreed.classification} />
              </CardContent>
            </Card>
          )}
        </TabsContent>

        <TabsContent value="intelligence" className="space-y-6">
          <Card>
            <CardHeader><CardTitle>AI Briefings</CardTitle></CardHeader>
            <CardContent>
              {briefings.length === 0 ? (
                <p className="text-[#888888] text-center py-8">No briefings available</p>
              ) : (
                <div className="space-y-3">
                  {briefings.slice(0, 15).map((b) => (
                    <div key={b.id} className="p-4 rounded-lg bg-white/5 border border-white/10">
                      <div className="flex items-center justify-between mb-2">
                        <p className="font-medium text-[#e8e8e8]">{b.action}</p>
                        <p className="text-xs text-[#888888]">{new Date(b.timestamp).toLocaleString()}</p>
                      </div>
                      <p className="text-sm text-[#c0c0c0]">{b.details}</p>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="regime" className="space-y-6">
          <Card>
            <CardHeader><CardTitle>Regime Signals</CardTitle></CardHeader>
            <CardContent>
              {regimeSignals.length === 0 ? (
                <p className="text-[#888888] text-center py-8">No regime signals</p>
              ) : (
                <div className="space-y-3">
                  {regimeSignals.slice(0, 15).map((s) => {
                    const regime = s.data && typeof s.data === 'object'
                      ? ((s.data as Record<string, unknown>).regime || (s.data as Record<string, unknown>).current_regime || (s.data as Record<string, unknown>).market_regime) as string | undefined
                      : undefined;
                    return (
                      <div key={s.id} className="flex items-center justify-between p-4 rounded-lg bg-white/5 border border-white/10">
                        <div className="flex items-center gap-3">
                          <div className={`size-3 rounded-full ${s.signal === 'buy' ? 'bg-[#00d4aa]' : s.signal === 'sell' ? 'bg-[#ff4466]' : 'bg-[#ffa500]'}`} />
                          <div>
                            <p className="font-medium text-[#e8e8e8]">{s.strategy.replace(/_/g, ' ')}</p>
                            <div className="flex items-center gap-2">
                              <p className="text-xs text-[#888888]">{new Date(s.timestamp).toLocaleString()}</p>
                              {regime && <Badge variant="secondary" className="text-[10px] px-1.5 py-0">{regime}</Badge>}
                            </div>
                          </div>
                        </div>
                        <div className="flex items-center gap-3">
                          <div className="text-right">
                            <p className="text-xs text-[#888888]">Confidence</p>
                            <p className="font-medium text-[#e8e8e8]">{((s.strength || 0) * 100).toFixed(0)}%</p>
                          </div>
                          <Badge variant={s.signal === 'buy' ? 'default' : s.signal === 'sell' ? 'destructive' : 'secondary'}>
                            {s.signal.toUpperCase()}
                          </Badge>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Signal-to-Execution Funnel Tab */}
        <TabsContent value="funnel" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Filter className="size-5 text-[#4a9eff]" />
                Signal-to-Execution Funnel
              </CardTitle>
            </CardHeader>
            <CardContent>
              {(() => {
                const latest = funnelData && funnelData.length > 0 ? funnelData[funnelData.length - 1] : null;
                if (!latest) {
                  return <p className="text-[#888888] text-center py-8">No funnel data available</p>;
                }
                const stages: { label: string; key: keyof FunnelData; color: string }[] = [
                  { label: 'Generated', key: 'generated', color: '#4a9eff' },
                  { label: 'Actionable', key: 'actionable', color: '#6bb5ff' },
                  { label: 'Deduped', key: 'deduped', color: '#ffa500' },
                  { label: 'Risk Passed', key: 'risk_passed', color: '#c0c0c0' },
                  { label: 'Executed', key: 'executed', color: '#00d4aa' },
                  { label: 'Filled', key: 'filled', color: '#00b894' },
                ];
                const maxVal = Math.max(latest.generated || 1, 1);
                return (
                  <div className="space-y-3">
                    {stages.map(({ label, key, color }) => {
                      const val = Number(latest[key]) || 0;
                      const pct = (val / maxVal) * 100;
                      return (
                        <div key={key} className="flex items-center gap-4">
                          <span className="text-sm text-[#888888] w-28 text-right shrink-0">{label}</span>
                          <div className="flex-1 h-8 bg-white/5 rounded-lg overflow-hidden relative">
                            <div className="h-full rounded-lg transition-all" style={{ width: `${Math.max(pct, 2)}%`, backgroundColor: color, opacity: 0.8 }} />
                            <span className="absolute inset-y-0 right-3 flex items-center text-sm font-medium text-[#e8e8e8]">{val}</span>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                );
              })()}
            </CardContent>
          </Card>

          {/* Time-of-Day P&L */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Clock className="size-5 text-[#ffa500]" />
                Time-of-Day P&L
              </CardTitle>
            </CardHeader>
            <CardContent>
              {(() => {
                const hourly = timePnl?.hourly;
                if (!hourly || Object.keys(hourly).length === 0) {
                  return <p className="text-[#888888] text-center py-8">No hourly P&L data yet</p>;
                }
                const chartData = Object.entries(hourly)
                  .map(([h, pnl]) => ({ hour: `${String(h).padStart(2, '0')}:00`, pnl: Number(pnl) }))
                  .sort((a, b) => a.hour.localeCompare(b.hour));
                return (
                  <ResponsiveContainer width="100%" height={280}>
                    <BarChart data={chartData} margin={{ top: 5, right: 20, bottom: 5, left: 10 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgba(192,192,192,0.08)" />
                      <XAxis dataKey="hour" stroke="#888888" tick={{ fontSize: 10 }} />
                      <YAxis stroke="#888888" tick={{ fontSize: 11 }} tickFormatter={(v) => `$${v}`} />
                      <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => [`$${v.toFixed(2)}`, 'P&L']} />
                      <ReferenceLine y={0} stroke="rgba(192,192,192,0.3)" />
                      <Bar dataKey="pnl" radius={[4, 4, 0, 0]}>
                        {chartData.map((entry, i) => (
                          <Cell key={i} fill={entry.pnl >= 0 ? '#00d4aa' : '#ff4466'} fillOpacity={0.8} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                );
              })()}
            </CardContent>
          </Card>

          {/* Strategy Attribution */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <BarChart3 className="size-5 text-[#c0c0c0]" />
                Strategy Attribution
              </CardTitle>
            </CardHeader>
            <CardContent>
              {(!attribution || attribution.length === 0) ? (
                <p className="text-[#888888] text-center py-8">No attribution data available</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full">
                    <thead>
                      <tr className="border-b border-white/5">
                        <th className="text-left py-3 px-4 text-sm font-medium text-[#888888]">Strategy</th>
                        <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">Total P&L</th>
                        <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">Trades</th>
                        <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">Avg Weight</th>
                      </tr>
                    </thead>
                    <tbody>
                      {attribution.sort((a, b) => b.total_pnl - a.total_pnl).map((a) => (
                        <tr key={a.strategy} className="border-b border-white/5 hover:bg-white/5 transition-colors">
                          <td className="py-3 px-4 font-medium text-[#e8e8e8]">{a.strategy}</td>
                          <td className={`text-right py-3 px-4 font-medium ${a.total_pnl >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]'}`}>
                            {a.total_pnl >= 0 ? '+' : ''}${a.total_pnl.toFixed(2)}
                          </td>
                          <td className="text-right py-3 px-4 text-[#c0c0c0]">{a.trade_count}</td>
                          <td className="text-right py-3 px-4 text-[#c0c0c0]">{(a.avg_weight * 100).toFixed(1)}%</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Fill Quality Tab */}
        <TabsContent value="fills" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Crosshair className="size-5 text-[#ff4466]" />
                Fill Quality Analysis
              </CardTitle>
            </CardHeader>
            <CardContent>
              {(!fills || fills.length === 0) ? (
                <p className="text-[#888888] text-center py-8">No fill data available</p>
              ) : (
                <>
                  {/* Summary metrics */}
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10">
                      <p className="text-xs text-[#888888]">Total Fills</p>
                      <p className="text-lg font-bold text-[#e8e8e8]">{fills.length}</p>
                    </div>
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10">
                      <p className="text-xs text-[#888888]">Avg Slippage</p>
                      <p className="text-lg font-bold text-[#ffa500]">
                        {(fills.reduce((s, f) => s + f.slippage_bps, 0) / fills.length).toFixed(1)} bps
                      </p>
                    </div>
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10">
                      <p className="text-xs text-[#888888]">Worst Slippage</p>
                      <p className="text-lg font-bold text-[#ff4466]">
                        {Math.max(...fills.map(f => f.slippage_bps)).toFixed(1)} bps
                      </p>
                    </div>
                    <div className="p-3 rounded-lg bg-white/5 border border-white/10">
                      <p className="text-xs text-[#888888]">Best Slippage</p>
                      <p className="text-lg font-bold text-[#00d4aa]">
                        {Math.min(...fills.map(f => f.slippage_bps)).toFixed(1)} bps
                      </p>
                    </div>
                  </div>

                  {/* Worst fills table */}
                  <h4 className="text-sm font-medium text-[#888888] mb-3">Recent Fills (sorted by slippage)</h4>
                  <div className="overflow-x-auto">
                    <table className="w-full">
                      <thead>
                        <tr className="border-b border-white/5">
                          <th className="text-left py-3 px-4 text-sm font-medium text-[#888888]">Symbol</th>
                          <th className="text-center py-3 px-4 text-sm font-medium text-[#888888]">Side</th>
                          <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">Mid Price</th>
                          <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">Fill Price</th>
                          <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">Slippage</th>
                          <th className="text-right py-3 px-4 text-sm font-medium text-[#888888]">Notional</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...fills].sort((a, b) => b.slippage_bps - a.slippage_bps).slice(0, 15).map((f) => (
                          <tr key={f.id} className="border-b border-white/5 hover:bg-white/5 transition-colors">
                            <td className="py-3 px-4 font-medium text-[#e8e8e8]">{f.symbol}</td>
                            <td className="py-3 px-4 text-center">
                              <Badge variant={f.side === 'buy' ? 'default' : 'destructive'}>{f.side.toUpperCase()}</Badge>
                            </td>
                            <td className="text-right py-3 px-4 text-[#c0c0c0]">${f.mid_price.toFixed(2)}</td>
                            <td className="text-right py-3 px-4 text-[#c0c0c0]">${f.fill_price.toFixed(2)}</td>
                            <td className={`text-right py-3 px-4 font-medium ${f.slippage_bps > 10 ? 'text-[#ff4466]' : f.slippage_bps > 5 ? 'text-[#ffa500]' : 'text-[#00d4aa]'}`}>
                              {f.slippage_bps.toFixed(1)} bps
                            </td>
                            <td className="text-right py-3 px-4 text-[#c0c0c0]">${f.notional.toFixed(2)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Correlation Heatmap Tab */}
        <TabsContent value="correlation" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Target className="size-5 text-[#ffa500]" />
                Strategy Correlation Heatmap
              </CardTitle>
            </CardHeader>
            <CardContent>
              {(() => {
                const matrix = corrData?.matrix;
                const alerts = corrData?.alerts || [];
                if (!matrix || Object.keys(matrix).length === 0) {
                  return <p className="text-[#888888] text-center py-8">No correlation data available</p>;
                }
                const strats = Object.keys(matrix);
                const corrColor = (v: number): string => {
                  const abs = Math.abs(v);
                  if (abs > 0.8) return 'bg-[#ff4466]/60 text-[#ff4466]';
                  if (abs > 0.5) return 'bg-[#ffa500]/30 text-[#ffa500]';
                  return 'bg-[#00d4aa]/20 text-[#00d4aa]';
                };
                return (
                  <div className="space-y-4">
                    {alerts.length > 0 && (
                      <div className="space-y-2">
                        {alerts.map((a, i) => (
                          <div key={i} className="p-2 rounded-lg bg-[#ff4466]/10 border border-[#ff4466]/30 text-sm text-[#ff4466]">{a}</div>
                        ))}
                      </div>
                    )}
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead>
                          <tr>
                            <th className="py-2 px-3 text-left text-[#888888]" />
                            {strats.map(s => (
                              <th key={s} className="py-2 px-3 text-center text-[#888888] font-medium">{s.length > 10 ? s.slice(0, 10) + '..' : s}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {strats.map(row => (
                            <tr key={row} className="border-b border-white/5">
                              <td className="py-2 px-3 font-medium text-[#e8e8e8]">{row.length > 10 ? row.slice(0, 10) + '..' : row}</td>
                              {strats.map(col => {
                                const val = matrix[row]?.[col] ?? 0;
                                return (
                                  <td key={col} className="py-2 px-3 text-center">
                                    <span className={`inline-block px-2 py-1 rounded text-xs font-medium ${row === col ? 'text-[#888888]' : corrColor(val)}`}>
                                      {row === col ? '-' : val.toFixed(2)}
                                    </span>
                                  </td>
                                );
                              })}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                );
              })()}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
