import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from "recharts";
import { Brain, TrendingUp, Shield, Gauge } from "lucide-react";
import { api, usePolling, type Intelligence, type Strategy, type StatusResponse } from "../config/api";

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

export function Analytics() {
  const { data: intelligence } = usePolling<Intelligence>(api.intelligence, 30000);
  const { data: strategies } = usePolling<Strategy[]>(api.strategies, 30000);
  const { data: status } = usePolling<StatusResponse>(api.status, 15000);

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
  const avgWinRate = strategies?.length
    ? (strategies.filter(s => s.win_rate !== null).reduce((s, st) => s + (st.win_rate || 0), 0) / strategies.filter(s => s.win_rate !== null).length)
    : 0;

  return (
    <div className="p-6 space-y-6">
      <div>
        <h2 className="text-3xl font-bold text-[#e8e8e8]">Analytics</h2>
        <p className="text-[#888888] mt-1">Performance insights, market intelligence, and risk metrics</p>
      </div>

      {/* Key Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        <Card>
          <CardContent className="p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-[#888888]">Total P&L</p>
                <p className={`text-2xl font-bold mt-1 ${totalPnl >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]'}`}>
                  {totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}
                </p>
              </div>
              <div className="p-3 rounded-lg bg-[#00d4aa]/10 border border-[#00d4aa]/30">
                <TrendingUp className="size-6 text-[#00d4aa]" />
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-[#888888]">Total Trades</p>
                <p className="text-2xl font-bold text-[#e8e8e8] mt-1">{totalTrades}</p>
              </div>
              <div className="p-3 rounded-lg bg-[#4a9eff]/10 border border-[#4a9eff]/30">
                <Gauge className="size-6 text-[#4a9eff]" />
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-[#888888]">Avg Win Rate</p>
                <p className="text-2xl font-bold text-[#e8e8e8] mt-1">{avgWinRate.toFixed(1)}%</p>
              </div>
              <div className="p-3 rounded-lg bg-[#c0c0c0]/10 border border-[#c0c0c0]/30">
                <Shield className="size-6 text-[#c0c0c0]" />
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-[#888888]">Fear & Greed</p>
                {fearGreed ? (
                  <p className="text-2xl font-bold text-[#e8e8e8] mt-1">{fearGreed.value} <span className="text-sm font-normal text-[#888888]">{fearGreed.classification}</span></p>
                ) : (
                  <p className="text-2xl font-bold text-[#888888] mt-1">N/A</p>
                )}
              </div>
              <div className="p-3 rounded-lg bg-[#ffa500]/10 border border-[#ffa500]/30">
                <Brain className="size-6 text-[#ffa500]" />
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      <Tabs defaultValue="performance" className="space-y-6">
        <TabsList>
          <TabsTrigger value="performance">Strategy Performance</TabsTrigger>
          <TabsTrigger value="intelligence">Market Intelligence</TabsTrigger>
          <TabsTrigger value="regime">Regime Signals</TabsTrigger>
        </TabsList>

        <TabsContent value="performance" className="space-y-6">
          {/* Strategy P&L Chart */}
          {strategyChartData.length > 0 && (
            <Card>
              <CardHeader><CardTitle>Strategy P&L Comparison</CardTitle></CardHeader>
              <CardContent>
                <ResponsiveContainer width="100%" height={350}>
                  <BarChart data={strategyChartData} layout="vertical" margin={{ left: 20, right: 20 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(192,192,192,0.1)" />
                    <XAxis type="number" stroke="#888888" />
                    <YAxis type="category" dataKey="shortName" stroke="#888888" width={100} tick={{ fontSize: 12 }} />
                    <Tooltip
                      contentStyle={{ backgroundColor: '#1a1a1a', border: '1px solid rgba(192,192,192,0.2)', borderRadius: '8px', color: '#e8e8e8' }}
                      formatter={(value: number) => [`$${value.toFixed(2)}`, 'P&L']}
                    />
                    <Bar dataKey="pnl" radius={[0, 8, 8, 0]} fill="#4a9eff">
                      {strategyChartData.map((entry, i) => (
                        <rect key={i} fill={entry.pnl >= 0 ? '#00d4aa' : '#ff4466'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>
          )}

          {/* Fear & Greed Gauge */}
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
                  {regimeSignals.slice(0, 15).map((s) => (
                    <div key={s.id} className="flex items-center justify-between p-4 rounded-lg bg-white/5 border border-white/10">
                      <div className="flex items-center gap-3">
                        <div className={`size-3 rounded-full ${s.signal === 'buy' ? 'bg-[#00d4aa]' : s.signal === 'sell' ? 'bg-[#ff4466]' : 'bg-[#c0c0c0]'}`} />
                        <div>
                          <p className="font-medium text-[#e8e8e8]">{s.strategy}</p>
                          <p className="text-xs text-[#888888]">{new Date(s.timestamp).toLocaleString()}</p>
                        </div>
                      </div>
                      <div className="flex items-center gap-3">
                        <div className="text-right">
                          <p className="text-sm text-[#888888]">Strength</p>
                          <p className="font-medium text-[#e8e8e8]">{(s.strength * 100).toFixed(0)}%</p>
                        </div>
                        <Badge variant={s.signal === 'buy' ? 'default' : s.signal === 'sell' ? 'destructive' : 'secondary'}>
                          {s.signal.toUpperCase()}
                        </Badge>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
