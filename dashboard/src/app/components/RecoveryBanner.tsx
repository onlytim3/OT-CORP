import { useState } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";
import { Button } from "./ui/button";
import { api, fetchAPI, usePolling, type RecoveryStatus } from "../config/api";

export function RecoveryBanner() {
  const { data, refresh } = usePolling<RecoveryStatus>(api.recoveryStatus, 30000);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  if (!data) return null;

  const handleResume = async () => {
    if (!window.confirm(
      "Resume trading? The system will enter Conservative Recovery Mode " +
      "(50% position sizes, 2-strategy minimum) until the portfolio recovers to 80% of peak."
    )) return;

    setSubmitting(true);
    setErr(null);
    try {
      const res = await fetchAPI<{ active?: boolean; error?: string }>(api.resumeTrading, {
        method: "POST",
        body: JSON.stringify({ reason: "Manual resume from dashboard" }),
      });
      if (res?.error) throw new Error(res.error);
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Resume failed");
    } finally {
      setSubmitting(false);
    }
  };

  if (data.halted) {
    return (
      <div className="bg-[#ff4466]/10 border border-[#ff4466]/20 rounded-xl p-4 backdrop-blur-sm flex items-center justify-between gap-4">
        <div className="flex items-start gap-3">
          <AlertTriangle className="size-5 text-[#ff4466] mt-0.5 shrink-0" />
          <div>
            <p className="text-[#e8e8e8] font-medium">System Drawdown Halt</p>
            <p className="text-[#c0c0c0] text-sm mt-1">
              Drawdown threshold exceeded. All opening trades are disabled.
              {data.halt_date && <span className="text-[#888888]"> (halted {data.halt_date})</span>}
            </p>
            {err && <p className="text-[#ff4466] text-sm mt-2">{err}</p>}
          </div>
        </div>
        <Button
          onClick={handleResume}
          disabled={submitting}
          className="bg-[#ff4466] hover:bg-[#ff4466]/80 text-white shrink-0"
        >
          {submitting ? "Resuming…" : "Resume (Conservative Mode)"}
        </Button>
      </div>
    );
  }

  if (data.active) {
    const scalePct = Math.round((data.position_scale ?? 0.5) * 100);
    const targetPct = Math.round((data.recovery_target_pct ?? 0.8) * 100);
    const progressPct = data.progress_pct ?? 0;
    return (
      <div className="bg-[#4a9eff]/10 border border-[#4a9eff]/20 rounded-xl p-4 backdrop-blur-sm flex items-center justify-between gap-4">
        <div className="flex items-start gap-3">
          <RotateCcw className="size-5 text-[#4a9eff] mt-0.5 shrink-0" />
          <div>
            <p className="text-[#e8e8e8] font-medium">Conservative Mode Active</p>
            <p className="text-[#c0c0c0] text-sm mt-1">
              Trading resumed at {scalePct}% position sizing, {data.min_strategies ?? 2}+ strategies required per trade.
              {" "}Progress: {progressPct}% / {targetPct}% to graduate.
            </p>
          </div>
        </div>
        <span className="text-[#888888] text-xs shrink-0">Auto-graduates at target</span>
      </div>
    );
  }

  return null;
}
