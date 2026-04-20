import { api, usePolling } from "../config/api";
import { Brain } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Badge } from "./ui/badge";

interface StrategyScore {
  strategy: string;
  score: number;
  wins: number;
  losses: number;
  win_rate: number;
  budget_mult: number;
  tier: string;
  rank: number;
}

interface ThompsonResponse {
  scores: StrategyScore[];
  updated_at: string;
}

function tierColor(tier: string) {
  if (tier === "top") return "text-[#00d4aa]";
  if (tier === "bottom") return "text-[#ff4466]";
  return "text-[#888]";
}

function tierBg(tier: string) {
  if (tier === "top") return "bg-[#00d4aa]/10 border-[#00d4aa]/20";
  if (tier === "bottom") return "bg-[#ff4466]/10 border-[#ff4466]/20";
  return "bg-white/5 border-white/10";
}

export function ThompsonScoresPanel() {
  const { data } = usePolling<ThompsonResponse>(api.thompsonScores, 60000);
  const scores = data?.scores ?? [];

  return (
    <Card className="bg-[#0a0a0a] border-white/[0.08]">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Brain className="size-4 text-[#00d4aa]" />
          <CardTitle className="text-sm text-[#e8e8e8]">Thompson Sampling — Strategy Rankings</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        {scores.length === 0 ? (
          <p className="text-xs text-[#555] text-center py-4">No scores yet — need 10+ trades per strategy</p>
        ) : (
          <div className="space-y-2">
            {scores.map((s) => {
              const barWidth = Math.round(s.score * 100);
              return (
                <div key={s.strategy} className={`p-2 rounded-lg border ${tierBg(s.tier)}`}>
                  <div className="flex items-center justify-between mb-1">
                    <div className="flex items-center gap-2">
                      <span className="text-[#666] text-xs w-4">#{s.rank}</span>
                      <span className="text-xs text-[#e8e8e8] font-mono">
                        {s.strategy.replace(/_/g, ' ')}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className={`text-xs font-bold ${tierColor(s.tier)}`}>
                        {s.budget_mult.toFixed(1)}x
                      </span>
                      <Badge variant="outline" className={`text-[10px] ${tierColor(s.tier)} border-current`}>
                        {s.tier.toUpperCase()}
                      </Badge>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="flex-1 h-1.5 bg-white/5 rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full ${s.tier === "top" ? "bg-[#00d4aa]" : s.tier === "bottom" ? "bg-[#ff4466]" : "bg-[#888]"}`}
                        style={{ width: `${barWidth}%` }}
                      />
                    </div>
                    <span className="text-[10px] text-[#555] w-24 text-right">
                      {s.wins}W/{s.losses}L ({(s.win_rate * 100).toFixed(0)}%)
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
