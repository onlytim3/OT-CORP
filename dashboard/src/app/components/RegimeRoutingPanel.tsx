import { api, usePolling } from "../config/api";
import { Gauge } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Badge } from "./ui/badge";

interface RoutingDecision {
  timestamp: string;
  symbol: string;
  strategy: string;
  action: string;
  original_strength: number | null;
  adjusted_strength: number | null;
  tag: string;
  regime: string;
  strat_type: string;
  multiplier: number | null;
}

interface RegimeRoutingResponse {
  current_regime: string;
  current_score: number;
  recent_blocks: RoutingDecision[];
}

function regimeColor(regime: string) {
  if (regime.includes("strongly bullish")) return "text-[#00d4aa] border-[#00d4aa]/30 bg-[#00d4aa]/10";
  if (regime === "bullish") return "text-[#00d4aa]/70 border-[#00d4aa]/20 bg-[#00d4aa]/5";
  if (regime === "bearish") return "text-[#ff4466]/70 border-[#ff4466]/20 bg-[#ff4466]/5";
  if (regime.includes("strongly bearish")) return "text-[#ff4466] border-[#ff4466]/30 bg-[#ff4466]/10";
  return "text-[#888] border-white/10 bg-white/5";
}

function tagColor(tag: string) {
  return tag === "BOOST" ? "text-[#00d4aa]" : tag === "DAMPEN" ? "text-[#ffa500]" : "text-[#666]";
}

export function RegimeRoutingPanel() {
  const { data } = usePolling<RegimeRoutingResponse>(api.regimeRoutingLog, 30000);
  const regime = data?.current_regime ?? "unknown";
  const score = data?.current_score ?? 0;
  const decisions = data?.recent_blocks ?? [];

  return (
    <Card className="bg-[#0a0a0a] border-white/[0.08]">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Gauge className="size-4 text-[#888]" />
          <CardTitle className="text-sm text-[#e8e8e8]">Regime Routing Log</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Current regime badge */}
        <div className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-full border text-sm font-medium ${regimeColor(regime)}`}>
          <span className="uppercase tracking-wide text-xs">{regime}</span>
          <span className="text-[10px] opacity-70">score {score >= 0 ? "+" : ""}{score.toFixed(3)}</span>
        </div>

        {/* Recent routing decisions */}
        {decisions.length === 0 ? (
          <p className="text-xs text-[#555] text-center py-4">No routing decisions in last 24h</p>
        ) : (
          <div className="space-y-1">
            <p className="text-[10px] text-[#555] uppercase tracking-wider">Last 24h Adjustments</p>
            <div className="space-y-1 max-h-48 overflow-y-auto">
              {decisions.slice(0, 12).map((d, i) => {
                const orig = d.original_strength;
                const adj = d.adjusted_strength;
                return (
                  <div key={i} className="flex items-center justify-between py-1.5 px-2 rounded bg-white/[0.03] text-[11px]">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className={`font-bold shrink-0 ${tagColor(d.tag)}`}>{d.tag || "—"}</span>
                      <span className="font-mono text-[#e8e8e8] truncate">{d.symbol}</span>
                      <span className="text-[#555] shrink-0">{d.action}</span>
                    </div>
                    <div className="flex items-center gap-2 shrink-0 ml-2">
                      {orig !== null && adj !== null && (
                        <span className="text-[#555] font-mono text-[10px]">
                          {orig.toFixed(2)} → <span className={tagColor(d.tag)}>{adj.toFixed(2)}</span>
                        </span>
                      )}
                      {d.multiplier !== null && (
                        <Badge variant="outline" className="text-[9px] text-[#666] border-white/10 hidden sm:inline-flex">
                          {d.multiplier.toFixed(2)}x
                        </Badge>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
