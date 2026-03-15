import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Bot, CheckCircle2, Clock, XCircle, MessageSquare } from "lucide-react";
import { useState } from "react";
import { api, usePolling, type AgentsResponse, type Recommendation } from "../config/api";

export function Agents() {
  const { data: agents } = usePolling<AgentsResponse>(api.agents, 15000);
  const [selectedRec, setSelectedRec] = useState<Recommendation | null>(null);

  const pending = agents?.pending || [];
  const recent = agents?.recent || [];
  const activity = agents?.activity || [];

  return (
    <div className="p-6 space-y-6">
      <div>
        <h2 className="text-3xl font-bold text-[#e8e8e8]">Agent Management</h2>
        <p className="text-[#888888] mt-1">Monitor AI agent recommendations and autonomous actions</p>
      </div>

      {/* Status Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <Card>
          <CardContent className="p-6">
            <div className="flex items-center gap-4">
              <div className="p-3 rounded-lg bg-[#ffa500]/10 border border-[#ffa500]/30">
                <Clock className="size-6 text-[#ffa500]" />
              </div>
              <div>
                <p className="text-sm text-[#888888]">Pending Review</p>
                <p className="text-2xl font-bold text-[#e8e8e8]">{pending.length}</p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-6">
            <div className="flex items-center gap-4">
              <div className="p-3 rounded-lg bg-[#00d4aa]/10 border border-[#00d4aa]/30">
                <CheckCircle2 className="size-6 text-[#00d4aa]" />
              </div>
              <div>
                <p className="text-sm text-[#888888]">Recent Actions</p>
                <p className="text-2xl font-bold text-[#e8e8e8]">{recent.length}</p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-6">
            <div className="flex items-center gap-4">
              <div className="p-3 rounded-lg bg-[#4a9eff]/10 border border-[#4a9eff]/30">
                <MessageSquare className="size-6 text-[#4a9eff]" />
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
              {recent.map((rec) => (
                <div key={rec.id} onClick={() => setSelectedRec(rec)}
                  className="p-4 border border-white/10 rounded-lg hover:border-white/20 hover:bg-white/5 transition-colors cursor-pointer">
                  <div className="flex items-start justify-between mb-2">
                    <div className="flex items-center gap-3">
                      {rec.status === 'accepted' ? (
                        <CheckCircle2 className="size-5 text-[#00d4aa]" />
                      ) : rec.status === 'rejected' ? (
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
                      <Badge variant={rec.status === 'accepted' ? 'default' : rec.status === 'rejected' ? 'destructive' : 'secondary'}>
                        {rec.status}
                      </Badge>
                      <p className="text-xs text-[#888888] mt-1">{new Date(rec.timestamp).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}</p>
                    </div>
                  </div>
                  <p className="text-sm text-[#c0c0c0] ml-8 line-clamp-2">{rec.reasoning}</p>
                </div>
              ))}
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
            <div className="space-y-2">
              {activity.slice(0, 20).map((a) => (
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
        <DialogContent className="bg-[#0a0a0a] border-white/8 text-[#e8e8e8] max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-3">
              <Bot className="size-6 text-[#4a9eff]" />
              Agent Recommendation
            </DialogTitle>
          </DialogHeader>
          {selectedRec && (
            <div className="space-y-4 mt-4">
              <div className="p-4 rounded-lg bg-white/5 border border-white/10">
                <p className="text-sm text-[#888888] mb-1">Action</p>
                <p className="text-lg font-bold">{selectedRec.action}</p>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-sm text-[#888888] mb-1">From Agent</p>
                  <p className="font-medium">{selectedRec.from_agent}</p>
                </div>
                <div className="p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-sm text-[#888888] mb-1">Target</p>
                  <p className="font-medium">{selectedRec.target}</p>
                </div>
                <div className="p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-sm text-[#888888] mb-1">Status</p>
                  <Badge variant={selectedRec.status === 'accepted' ? 'default' : selectedRec.status === 'rejected' ? 'destructive' : 'secondary'}>
                    {selectedRec.status}
                  </Badge>
                </div>
                <div className="p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-sm text-[#888888] mb-1">Time</p>
                  <p className="text-sm">{new Date(selectedRec.timestamp).toLocaleString()}</p>
                </div>
              </div>
              <div className="p-4 rounded-lg bg-[#4a9eff]/10 border border-[#4a9eff]/20">
                <p className="text-sm text-[#888888] mb-2">Reasoning</p>
                <p className="text-[#c0c0c0]">{selectedRec.reasoning}</p>
              </div>
              {selectedRec.data && (
                <div className="p-4 rounded-lg bg-white/5 border border-white/10">
                  <p className="text-sm text-[#888888] mb-1">Data</p>
                  <pre className="text-xs text-[#c0c0c0] overflow-auto max-h-40">{JSON.stringify(selectedRec.data, null, 2)}</pre>
                </div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
