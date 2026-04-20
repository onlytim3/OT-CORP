import { api, usePolling } from "../config/api";
import { Filter } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Badge } from "./ui/badge";

interface CounterfactualRow {
  id: number;
  timestamp: string;
  symbol: string;
  strategy: string;
  action: string;
  signal_strength: number;
  block_reason: string;
  hypothetical_pnl_pct: number | null;
}

interface CounterfactualResponse {
  summary: { total: number; by_reason: Record<string, { count: number; avg_pnl: number; positive: number }> };
  recent: CounterfactualRow[];
  accuracy: Record<string, number>;
}

function reasonLabel(reason: string) {
  return reason.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

export function CounterfactualPanel() {
  const { data } = usePolling<CounterfactualResponse>(api.counterfactualAnalysis, 60000);
  const summary = data?.summary ?? { total: 0, by_reason: {} };
  const recent = data?.recent ?? [];
  const accuracy = data?.accuracy ?? {};

  const correctBlocks = Object.values(accuracy).length > 0
    ? (Object.values(accuracy).reduce((a, b) => a + b, 0) / Object.values(accuracy).length * 100).toFixed(0)
    : null;

  return (
    <Card className="bg-[#0a0a0a] border-white/[0.08]">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Filter className="size-4 text-[#ffa500]" />
          <CardTitle className="text-sm text-[#e8e8e8]">Counterfactual Gate Accuracy</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Summary header */}
        <div className="flex items-center justify-between p-3 rounded-lg bg-white/5 border border-white/[0.08]">
          <div>
            <p className="text-xs text-[#666]">Signals blocked (30d)</p>
            <p className="text-2xl font-bold text-[#e8e8e8]">{summary.total}</p>
          </div>
          {correctBlocks !== null && (
            <div className="text-right">
              <p className="text-xs text-[#666]">Avg gate accuracy</p>
              <p className="text-2xl font-bold text-[#00d4aa]">{correctBlocks}%</p>
            </div>
          )}
        </div>

        {/* Accuracy by gate */}
        {Object.keys(accuracy).length > 0 && (
          <div className="space-y-2">
            <p className="text-[10px] text-[#555] uppercase tracking-wider">Gate Accuracy</p>
            {Object.entries(accuracy).map(([reason, acc]) => (
              <div key={reason} className="flex items-center justify-between">
                <span className="text-xs text-[#888]">{reasonLabel(reason)}</span>
                <div className="flex items-center gap-2">
                  <div className="w-20 h-1.5 bg-white/5 rounded-full overflow-hidden">
                    <div className="h-full bg-[#00d4aa] rounded-full" style={{ width: `${acc * 100}%` }} />
                  </div>
                  <span className="text-xs text-[#e8e8e8] w-10 text-right">{(acc * 100).toFixed(0)}%</span>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Recent blocked signals */}
        {recent.length > 0 && (
          <div className="space-y-1">
            <p className="text-[10px] text-[#555] uppercase tracking-wider">Recent Blocks</p>
            <div className="space-y-1 max-h-40 overflow-y-auto">
              {recent.slice(0, 8).map((row) => {
                const pnl = row.hypothetical_pnl_pct;
                const wasCorrect = pnl !== null && pnl < 0;
                return (
                  <div key={row.id} className="flex items-center justify-between py-1 px-2 rounded bg-white/[0.03]">
                    <div>
                      <span className="text-[10px] font-mono text-[#e8e8e8]">{row.symbol}</span>
                      <span className="text-[10px] text-[#555] ml-1">{row.action}</span>
                    </div>
                    <div className="flex items-center gap-1">
                      <Badge variant="outline" className="text-[9px] text-[#888] border-white/10">
                        {reasonLabel(row.block_reason)}
                      </Badge>
                      {pnl !== null && (
                        <span className={`text-[10px] font-mono ${wasCorrect ? "text-[#00d4aa]" : "text-[#ff4466]"}`}>
                          {pnl >= 0 ? "+" : ""}{(pnl * 100).toFixed(1)}%
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {summary.total === 0 && (
          <p className="text-xs text-[#555] text-center py-2">No blocked signals with outcomes yet</p>
        )}
      </CardContent>
    </Card>
  );
}
