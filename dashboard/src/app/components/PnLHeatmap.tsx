import { useState, useEffect } from 'react';
import { fetchAPI } from '../config/api';

interface DayData {
  date: string;
  pnl: number;
  trades: number;
}

interface PnLHeatmapProps {
  pnlHistory?: Array<{ date: string; pnl: number; trade_count?: number }>;
}

function getColor(pnl: number, maxAbs: number): string {
  if (maxAbs === 0) return 'rgba(255, 255, 255, 0.05)';
  const intensity = Math.min(Math.abs(pnl) / maxAbs, 1);
  if (pnl > 0) {
    // Green: from faint to vibrant
    const r = Math.round(0 + (0) * intensity);
    const g = Math.round(80 + (132) * intensity);
    const a = Math.round(170 + (0) * intensity);
    return `rgba(${r}, ${g}, ${a}, ${0.2 + intensity * 0.8})`;
  } else if (pnl < 0) {
    // Red: from faint to vibrant
    return `rgba(255, ${Math.round(68 - 20 * intensity)}, ${Math.round(102 - 40 * intensity)}, ${0.2 + intensity * 0.8})`;
  }
  return 'rgba(255, 255, 255, 0.05)';
}

function getBorderColor(pnl: number): string {
  if (pnl > 0) return 'rgba(0, 212, 170, 0.4)';
  if (pnl < 0) return 'rgba(255, 68, 102, 0.4)';
  return 'rgba(255, 255, 255, 0.08)';
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export function PnLHeatmap({ pnlHistory }: PnLHeatmapProps) {
  const [tooltip, setTooltip] = useState<{ x: number; y: number; data: DayData } | null>(null);

  // Build 52-week grid from pnlHistory
  const today = new Date();
  const startDate = new Date(today);
  startDate.setFullYear(today.getFullYear() - 1);
  startDate.setDate(startDate.getDate() - startDate.getDay()); // align to Sunday

  // Create a map from date string -> data
  const dataMap = new Map<string, DayData>();
  if (pnlHistory) {
    for (const d of pnlHistory) {
      dataMap.set(d.date, {
        date: d.date,
        pnl: d.pnl,
        trades: d.trade_count ?? 0,
      });
    }
  }

  // Build 52 weeks of cells
  const weeks: Array<Array<{ date: string; data: DayData | null }>> = [];
  const cursor = new Date(startDate);

  for (let w = 0; w < 53; w++) {
    const week: Array<{ date: string; data: DayData | null }> = [];
    for (let d = 0; d < 7; d++) {
      const dateStr = cursor.toISOString().slice(0, 10);
      week.push({
        date: dateStr,
        data: dataMap.get(dateStr) ?? null,
      });
      cursor.setDate(cursor.getDate() + 1);
    }
    weeks.push(week);
  }

  // Compute max absolute PnL for color scaling
  const allPnls = Array.from(dataMap.values()).map(d => Math.abs(d.pnl));
  const maxAbs = allPnls.length > 0 ? Math.max(...allPnls) : 1;

  // Compute month labels
  const monthLabels: { label: string; col: number }[] = [];
  let lastMonth = -1;
  weeks.forEach((week, wi) => {
    const m = new Date(week[0].date).getMonth();
    if (m !== lastMonth) {
      monthLabels.push({ label: MONTHS[m], col: wi });
      lastMonth = m;
    }
  });

  // Summary stats
  const totalPnl = Array.from(dataMap.values()).reduce((s, d) => s + d.pnl, 0);
  const winDays = Array.from(dataMap.values()).filter(d => d.pnl > 0).length;
  const lossDays = Array.from(dataMap.values()).filter(d => d.pnl < 0).length;

  return (
    <div className="space-y-4">
      {/* Summary row */}
      <div className="flex flex-wrap gap-4 text-sm">
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-white/5 border border-white/8">
          <span className="text-[#888888]">Year P&L</span>
          <span className={`font-bold ${totalPnl >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]'}`}>
            {totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}
          </span>
        </div>
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-white/5 border border-white/8">
          <span className="w-2 h-2 rounded-sm bg-[#00d4aa]" />
          <span className="text-[#888888]">{winDays} Green Days</span>
        </div>
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-white/5 border border-white/8">
          <span className="w-2 h-2 rounded-sm bg-[#ff4466]" />
          <span className="text-[#888888]">{lossDays} Red Days</span>
        </div>
      </div>

      {/* Heatmap grid */}
      <div className="relative overflow-x-auto">
        {/* Month labels */}
        <div className="flex mb-1 ml-8" style={{ gap: '2px' }}>
          {weeks.map((_, wi) => {
            const label = monthLabels.find(m => m.col === wi);
            return (
              <div key={wi} className="text-[10px] text-[#555555] w-[13px] shrink-0 text-center">
                {label ? label.label : ''}
              </div>
            );
          })}
        </div>

        <div className="flex gap-1">
          {/* Day labels */}
          <div className="flex flex-col gap-[2px] mr-1">
            {DAYS.map((day, di) => (
              <div key={day} className="h-[13px] text-[10px] text-[#555555] flex items-center">
                {di % 2 === 1 ? day.slice(0, 3) : ''}
              </div>
            ))}
          </div>

          {/* Grid cells */}
          {weeks.map((week, wi) => (
            <div key={wi} className="flex flex-col gap-[2px]">
              {week.map((cell, di) => {
                const bg = cell.data ? getColor(cell.data.pnl, maxAbs) : 'rgba(255,255,255,0.04)';
                const border = cell.data ? getBorderColor(cell.data.pnl) : 'rgba(255,255,255,0.06)';
                const isInFuture = new Date(cell.date) > today;
                return (
                  <div
                    key={di}
                    className="w-[13px] h-[13px] rounded-[2px] cursor-pointer transition-all duration-150 hover:scale-125 hover:z-10 relative"
                    style={{
                      backgroundColor: isInFuture ? 'rgba(255,255,255,0.02)' : bg,
                      border: `1px solid ${isInFuture ? 'rgba(255,255,255,0.04)' : border}`,
                    }}
                    onMouseEnter={(e) => {
                      if (cell.data) {
                        const rect = e.currentTarget.getBoundingClientRect();
                        setTooltip({ x: rect.left, y: rect.top, data: cell.data });
                      }
                    }}
                    onMouseLeave={() => setTooltip(null)}
                  />
                );
              })}
            </div>
          ))}
        </div>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-2 text-[10px] text-[#555555]">
        <span>Less</span>
        {[-1, -0.5, 0, 0.5, 1].map((v, i) => (
          <div
            key={i}
            className="w-3 h-3 rounded-[2px]"
            style={{ backgroundColor: v === 0 ? 'rgba(255,255,255,0.05)' : getColor(v * maxAbs || 50, maxAbs || 50) }}
          />
        ))}
        <span>More</span>
      </div>

      {/* Tooltip */}
      {tooltip && (
        <div
          className="fixed z-[200] pointer-events-none px-3 py-2 rounded-lg bg-[#0a0a0a] border border-white/10 shadow-2xl text-sm"
          style={{ left: tooltip.x + 16, top: tooltip.y - 60 }}
        >
          <p className="text-[#888888] text-xs mb-1">{tooltip.data.date}</p>
          <p className={`font-bold ${tooltip.data.pnl >= 0 ? 'text-[#00d4aa]' : 'text-[#ff4466]'}`}>
            {tooltip.data.pnl >= 0 ? '+' : ''}${tooltip.data.pnl.toFixed(2)}
          </p>
          <p className="text-[#666666] text-xs">{tooltip.data.trades} trade{tooltip.data.trades !== 1 ? 's' : ''}</p>
        </div>
      )}
    </div>
  );
}
