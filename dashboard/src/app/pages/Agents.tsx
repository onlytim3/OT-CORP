import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Bot, CheckCircle2, Clock, XCircle, MessageSquare, Activity, Shield, TrendingUp, Brain, Search, BarChart3 } from "lucide-react";
import { useState } from "react";
import { api, usePolling, type AgentsResponse, type Recommendation, type AgentStats } from "../config/api";

export function Agents() {
  const { data: agents } = usePolling<AgentsResponse>(api.agents, 15000);
  const [selectedRec, setSelectedRec] = useState<Recommendation | null>(null);

  const pending = agents?.pending || [];
  const recent = agents?.recent || [];
  const activity = agents?.activity || [];
  const agentStats = agents?.agent_stats || [];

  const agentMeta: Record<string, { icon: typeof Bot; color: string; label: string }> = {
    performance_agent: { icon: TrendingUp, color: '#00d4aa', label: 'Performance' },
    research_agent: { icon: Search, color: '#4a9eff', label: 'Research' },
    risk_agent: { icon: Shield, color: '#ff4466', label: 'Risk' },
    regime_agent: { icon: Activity, color: '#ffa500', label: 'Regime' },
    learning_agent: { icon: Brain, color: '#c084fc', label: 'Learning' },
    backtest_agent: { icon: BarChart3, color: '#56d4dd', label: 'Backtest' },
  };

  return (
    <div className="p-6 space-y-6">
      <div>
        <h2 className="text-3xl font-bold text-[#e8e8e8]">Agent Management</h2>
        <p className="text-[#888888] mt-1">Monitor AI agent recommendations and autonomous actions</p>
      </div>

      {/* Agent Performance Tiles */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 sm:gap-4">
        {agentStats.map((agent) => {
          const meta = agentMeta[agent.name] || { icon: Bot, color: '#888888', label: agent.name.replace('_agent', '') };
          const Icon = meta.icon;
          const successRate = agent.total > 0 ? Math.round((agent.applied / agent.total) * 100) : 0;
          return (
            <Card key={agent.name} className="hover:shadow-lg transition-shadow">
              <CardContent className="p-4">
                <div className="flex items-center gap-2 mb-3">
                  <div className="p-1.5 rounded-md" style={{ backgroundColor: `${meta.color}15`, border: `1px solid ${meta.color}30` }}>
                    <Icon className="size-4" style={{ color: meta.color }} />
                  </div>
                  <p className="text-xs font-medium text-[#e8e8e8] uppercase tracking-wider truncate">{meta.label}</p>
                </div>
                <p className="text-2xl font-bold text-[#e8e8e8] tabular-nums">{agent.total}</p>
                <p className="text-xs text-[#888888] mb-2">recommendations</p>
                <div className="flex items-center gap-2 text-xs">
                  <span className="text-[#00d4aa]">{agent.applied} applied</span>
                  <span className="text-[#888888]">·</span>
                  <span className="text-[#888888]">{successRate}%</span>
                </div>
                {agent.last_active && (
                  <p className="text-[10px] text-[#666666] mt-2 truncate">
                    Last: {new Date(agent.last_active).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
                  </p>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* Summary Stats */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card>
          <CardContent className="p-4 sm:p-6">
            <div className="flex items-center gap-4">
              <div className="p-3 rounded-lg bg-[#ffa500]/10 border border-[#ffa500]/30">
                <Clock className="size-5 sm:size-6 text-[#ffa500]" />
              </div>
              <div>
                <p className="text-sm text-[#888888]">Pending Review</p>
                <p className="text-2xl font-bold text-[#e8e8e8]">{pending.length}</p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 sm:p-6">
            <div className="flex items-center gap-4">
              <div className="p-3 rounded-lg bg-[#00d4aa]/10 border border-[#00d4aa]/30">
                <CheckCircle2 className="size-5 sm:size-6 text-[#00d4aa]" />
              </div>
              <div>
                <p className="text-sm text-[#888888]">Recent Actions</p>
                <p className="text-2xl font-bold text-[#e8e8e8]">{recent.length}</p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 sm:p-6">
            <div className="flex items-center gap-4">
              <div className="p-3 rounded-lg bg-[#4a9eff]/10 border border-[#4a9eff]/30">
                <MessageSquare className="size-5 sm:size-6 text-[#4a9eff]" />
              </div>
              <div>
                <p className="text-sm text-[#888888]">Activity Log</p>
                <p className="text-2xl font-bold text-[#e8e8e8]">{activity.length}</p>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Pending Recommendations */}
      {pending.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-[#ffa500]">
              <Clock className="size-5" />
              Pending Recommendations
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {pending.map((rec) => (
                <div key={rec.id} onClick={() => setSelectedRec(rec)}
                  className="p-4 border border-[#ffa500]/20 rounded-lg hover:border-[#ffa500]/40 hover:bg-white/5 transition-colors cursor-pointer">
                  <div className="flex items-start justify-between mb-2">
                    <div className="flex items-center gap-3">
                      <Bot className="size-5 text-[#ffa500]" />
                      <div>
                        <p className="font-semibold text-[#e8e8e8]">{rec.action}</p>
                        <p className="text-sm text-[#888888]">From: {rec.from_agent} | Target: {rec.target}</p>
                      </div>
                    </div>
                    <Badge variant="secondary" className="bg-[#ffa500]/10 text-[#ffa500] border-[#ffa500]/30">Pending</Badge>
                  </div>
                  <p className="text-sm text-[#c0c0c0] ml-8">{rec.reasoning}</p>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Recent Recommendations */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Bot className="size-5" />
            Recent Agent Recommendations
          </CardTitle>
        </CardHeader>
        <CardContent>
          {recent.length === 0 ? (
            <p className="text-[#888888] text-center py-8">No recent recommendations</p>
          ) : (
            <div className="space-y-3">
              {recent.map((rec) => {
                const resolved = rec.resolution || rec.status;
                const isApplied = resolved === 'applied' || resolved === 'accepted';
                const isRejected = resolved === 'rejected';
                return (
                  <div key={rec.id} onClick={() => setSelectedRec(rec)}
                    className="p-4 border border-white/10 rounded-lg hover:border-white/20 hover:bg-white/5 transition-colors cursor-pointer">
                    <div className="flex items-start justify-between mb-2">
                      <div className="flex items-center gap-3">
                        {isApplied ? (
                          <CheckCircle2 className="size-5 text-[#00d4aa]" />
                        ) : isRejected ? (
                          <XCircle className="size-5 text-[#ff4466]" />
                        ) : (
                          <Clock className="size-5 text-[#ffa500]" />
                        )}
                        <div>
                          <p className="font-medium text-[#e8e8e8]">{rec.action}</p>
                          <p className="text-xs text-[#888888]">{rec.from_agent} → {rec.target}</p>
                        </div>
                      </div>
                      <div className="text-right">
                        <Badge variant={isApplied ? 'default' : isRejected ? 'destructive' : 'secondary'}>
                          {isApplied ? 'applied' : resolved}
                        </Badge>
                        <p className="text-xs text-[#888888] mt-1">{new Date(rec.timestamp).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}</p>
                      </div>
                    </div>
                    <p className="text-sm text-[#c0c0c0] ml-8 line-clamp-2">{rec.reasoning}</p>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Activity Log */}
      <Card>
        <CardHeader><CardTitle>Agent Activity Log</CardTitle></CardHeader>
        <CardContent>
          {activity.length === 0 ? (
            <p className="text-[#888888] text-center py-8">No agent activity</p>
          ) : (
            <div className="space-y-2 max-h-[50vh] overflow-y-auto">
              {activity.map((a) => (
                <div key={a.id} className="flex items-center justify-between py-2 border-b border-white/5 last:border-0">
                  <div className="flex items-center gap-3">
                    <div className="size-2 rounded-full bg-[#4a9eff]" />
                    <div>
                      <p className="text-sm font-medium text-[#e8e8e8]">{a.action}</p>
                      {a.details && <p className="text-xs text-[#888888]">{a.details}</p>}
                    </div>
                  </div>
                  <p className="text-xs text-[#888888]">{new Date(a.timestamp).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}</p>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Recommendation Detail Modal */}
      <Dialog open={!!selectedRec} onOpenChange={() => setSelectedRec(null)}>
        <DialogContent className="bg-[#0a0a0a] border-white/8 text-[#e8e8e8] sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-3">
              <Bot className="size-5 sm:size-6 text-[#4a9eff]" />
              Agent Recommendation
            </DialogTitle>
          </DialogHeader>
          {selectedRec && (
            <div className="space-y-3 sm:space-y-4 mt-2 sm:mt-4">
              <div className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                <p className="text-xs sm:text-sm text-[#888888] mb-1">Action</p>
                <p className="text-base sm:text-lg font-bold">{selectedRec.action}</p>
              </div>
              <div className="grid grid-cols-2 gap-2 sm:gap-4">
                <div className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-xs sm:text-sm text-[#888888] mb-1">From Agent</p>
                  <p className="text-sm sm:text-base font-medium truncate">{selectedRec.from_agent}</p>
                </div>
                <div className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-xs sm:text-sm text-[#888888] mb-1">Target</p>
                  <p className="text-sm sm:text-base font-medium truncate">{selectedRec.target}</p>
                </div>
                <div className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-xs sm:text-sm text-[#888888] mb-1">Status</p>
                  {(() => {
                    const res = selectedRec.resolution || selectedRec.status;
                    const applied = res === 'applied' || res === 'accepted';
                    return (
                      <Badge variant={applied ? 'default' : res === 'rejected' ? 'destructive' : 'secondary'}>
                        {applied ? 'applied' : res}
                      </Badge>
                    );
                  })()}
                </div>
                <div className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-xs sm:text-sm text-[#888888] mb-1">Time</p>
                  <p className="text-xs sm:text-sm">{new Date(selectedRec.timestamp).toLocaleString()}</p>
                </div>
              </div>
              <div className="p-3 sm:p-4 rounded-lg bg-[#4a9eff]/10 border border-[#4a9eff]/20">
                <p className="text-xs sm:text-sm text-[#888888] mb-2">Reasoning</p>
                <p className="text-sm sm:text-base text-[#c0c0c0]">{selectedRec.reasoning}</p>
              </div>
              {selectedRec.data && (
                <div className="p-3 sm:p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-xs sm:text-sm text-[#888888] mb-1">Data</p>
                  <pre className="text-xs text-[#c0c0c0] overflow-x-auto max-h-40 whitespace-pre-wrap break-all">{JSON.stringify(selectedRec.data, null, 2)}</pre>
                </div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
