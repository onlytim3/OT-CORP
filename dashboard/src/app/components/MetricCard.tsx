import { LucideIcon } from "lucide-react";
import { Card, CardContent } from "./ui/card";
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
    <Card className="hover:border-white/15 transition-colors">
      <CardContent className="p-6">
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
          <div className={`p-3 rounded-lg bg-white/5 border border-white/8 ${iconColor}`}>
            <Icon className="size-6" />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}