import { LucideIcon } from "lucide-react";
import { cn } from "./ui/utils";
import { TrendingUp, TrendingDown } from "lucide-react";

interface MetricCardProps {
  title: string;
  value: string | number;
  change?: number;
  icon: LucideIcon;
  iconColor?: string;
}

export function MetricCard({ title, value, change, icon: Icon, iconColor }: MetricCardProps) {
  return (
    <div className="group relative rounded-xl bg-white/[0.03] backdrop-blur-xl border border-white/[0.08] shadow-lg shadow-black/20 transition-all duration-300 hover:bg-white/[0.06] hover:border-white/[0.15] hover:shadow-xl hover:shadow-[#4a9eff]/5 overflow-hidden">
      {/* Subtle gradient overlay */}
      <div className="absolute inset-0 bg-gradient-to-br from-white/[0.02] to-transparent pointer-events-none" />
      <div className="relative p-6">
        <div className="flex items-center justify-between">
          <div className="flex-1">
            <p className="text-xs text-[#666666] uppercase tracking-wider mb-2">{title}</p>
            <p className="text-2xl font-bold text-[#e8e8e8] mb-2 tabular-nums">{value}</p>
            {change !== undefined && (
              <div className={`flex items-center gap-1 ${change >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]'}`}>
                {change >= 0 ? (
                  <TrendingUp className="size-4" />
                ) : (
                  <TrendingDown className="size-4" />
                )}
                <span className="text-sm font-medium">
                  {change >= 0 ? '+' : ''}{change}%
                </span>
              </div>
            )}
          </div>
          <div className={`p-3 rounded-xl bg-white/[0.05] border border-white/[0.08] backdrop-blur-sm ${iconColor} transition-all duration-300 group-hover:bg-white/[0.08] group-hover:border-white/[0.15]`}>
            <Icon className="size-6" />
          </div>
        </div>
      </div>
    </div>
  );
}
