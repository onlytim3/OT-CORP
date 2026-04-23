import { Card, CardContent } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { BookOpen, Calendar, ChevronLeft, ChevronRight, Sparkles, Clock, TrendingUp, TrendingDown, Brain, RefreshCw, BookMarked, FileText, BarChart2, FlaskConical, Loader2 } from "lucide-react";
import { useState, useEffect, useCallback } from "react";
import { api, fetchAPI, usePolling } from "../config/api";
import { useIsMobile } from "../components/ui/use-mobile";
import { PnLHeatmap } from "../components/PnLHeatmap";

// --- Types ---

interface JournalEntry {
  id: number;
  trade_id: number | null;
  timestamp: string;
  rationale: string | null;
  market_context: Record<string, unknown> | null;
  outcome: string | null;
  pnl: number | null;
  lesson: string | null;
  tags: string | null;
  symbol?: string;
  side?: string;
  qty?: number;
  price?: number;
  strategy?: string;
}

interface DailyJournal {
  id: number;
  timestamp: string;
  content: string;
  type: "daily";
}

interface WeeklyReview {
  id: number;
  timestamp: string;
  content: string;
  type: "weekly";
}

// --- Helpers ---

function formatTime(ts: string): string {
  const d = new Date(ts);
  return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: true });
}

function formatDateFull(ts: string): string {
  const d = new Date(ts);
  return d.toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" });
}

function formatDateShort(ts: string): string {
  const d = new Date(ts);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function timeAgo(ts: string): string {
  const diff = Date.now() - new Date(ts).getTime();
  const hours = Math.floor(diff / 3600000);
  if (hours < 1) return "Just now";
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "Yesterday";
  return `${days}d ago`;
}

function groupByDate(entries: JournalEntry[]): Map<string, JournalEntry[]> {
  const map = new Map<string, JournalEntry[]>();
  for (const e of entries) {
    const dateKey = e.timestamp.slice(0, 10);
    if (!map.has(dateKey)) map.set(dateKey, []);
    map.get(dateKey)!.push(e);
  }
  return map;
}

/** Render inline bold (**text**) within a string */
function renderMarkdownInline(text: string): React.ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={i} className="text-[#e8e8e8] font-semibold">{part.slice(2, -2)}</strong>;
    }
    return part;
  });
}

function renderMarkdownContent(text: string, accentColor: string = "#4a9eff") {
  return text.split("\n").map((line, i) => {
    if (!line.trim()) return <div key={i} className="h-4" />;
    if (line.startsWith("# ")) {
      return (
        <h2 key={i} className="text-base font-semibold text-[#e8e8e8] mt-6 mb-3 tracking-wide">
          {line.replace(/^#+\s*/, "")}
        </h2>
      );
    }
    if (line.startsWith("## ") || line.startsWith("### ")) {
      return (
        <h3 key={i} className="text-sm font-semibold text-[#c0c0c0] mt-5 mb-2 tracking-wide">
          {line.replace(/^#+\s*/, "")}
        </h3>
      );
    }
    if (line.startsWith("**") && line.endsWith("**")) {
      return (
        <h3 key={i} className="text-sm font-semibold text-[#c0c0c0] mt-5 mb-2">
          {line.replace(/\*\*/g, "")}
        </h3>
      );
    }
    if (line.match(/^[-*]\s/)) {
      return (
        <div key={i} className="flex items-start gap-2 ml-2 mb-1.5">
          <span className="mt-1.5 text-xs" style={{ color: accentColor }}>&#8226;</span>
          <p className="text-sm text-[#d4d4d4] leading-[1.8] font-[350]" style={{ fontFamily: "'Georgia', 'Times New Roman', serif" }}>
            {renderMarkdownInline(line.replace(/^[-*]\s/, ""))}
          </p>
        </div>
      );
    }
    if (line.match(/^\d+\.\s/)) {
      const num = line.match(/^(\d+)\./)?.[1];
      return (
        <div key={i} className="flex items-start gap-2 ml-2 mb-1.5">
          <span className="mt-0.5 text-xs font-mono min-w-[16px]" style={{ color: accentColor }}>{num}.</span>
          <p className="text-sm text-[#d4d4d4] leading-[1.8] font-[350]" style={{ fontFamily: "'Georgia', 'Times New Roman', serif" }}>
            {renderMarkdownInline(line.replace(/^\d+\.\s/, ""))}
          </p>
        </div>
      );
    }
    return (
      <p key={i} className="text-sm text-[#d4d4d4] leading-[1.8] mb-3 font-[350]" style={{ fontFamily: "'Georgia', 'Times New Roman', serif" }}>
        {renderMarkdownInline(line)}
      </p>
    );
  });
}

// --- Components ---

function PageDivider({ date }: { date: string }) {
  const d = new Date(date + "T00:00:00");
  const dayName = d.toLocaleDateString("en-US", { weekday: "long" });
  const monthDay = d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });

  return (
    <div className="flex items-center gap-4 my-8">
      <div className="flex-1 h-px bg-gradient-to-r from-transparent via-white/[0.08] to-transparent" />
      <div className="flex items-center gap-2 px-4 py-2 rounded-full bg-white/[0.03] border border-white/[0.06]">
        <Calendar className="size-3.5 text-[#4a9eff]" />
        <span className="text-xs font-medium text-[#4a9eff] tracking-wider uppercase">{dayName}</span>
        <span className="text-xs text-[#666666]">{monthDay}</span>
      </div>
      <div className="flex-1 h-px bg-gradient-to-r from-transparent via-white/[0.08] to-transparent" />
    </div>
  );
}

function JournalEntryCard({ entry }: { entry: JournalEntry }) {
  const pnl = entry.pnl;
  const isProfit = pnl !== null && pnl > 0;
  const isLoss = pnl !== null && pnl < 0;

  return (
    <div className="group relative pl-8 pb-6">
      {/* Timeline dot */}
      <div className="absolute left-0 top-1 w-4 h-4 rounded-full border-2 border-[#4a9eff]/40 bg-[#0a0a0a] group-hover:border-[#4a9eff] transition-colors z-10" />
      {/* Timeline line */}
      <div className="absolute left-[7px] top-5 bottom-0 w-px bg-white/[0.06]" />

      <div className="bg-white/[0.02] hover:bg-white/[0.04] border border-white/[0.06] hover:border-white/[0.1] rounded-xl p-5 transition-all duration-300">
        {/* Header */}
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2 flex-wrap">
            <Clock className="size-3 text-[#888888]" />
            <span className="text-xs text-[#888888] font-mono">{formatTime(entry.timestamp)}</span>
            {entry.symbol && (
              <Badge variant="secondary" className="text-[10px] bg-[#4a9eff]/10 text-[#4a9eff] border-[#4a9eff]/20">
                {entry.symbol}
              </Badge>
            )}
            {entry.side && (
              <Badge variant="secondary" className={`text-[10px] ${
                entry.side === "buy"
                  ? "bg-[#00d4aa]/10 text-[#00d4aa] border-[#00d4aa]/20"
                  : "bg-[#ff4466]/10 text-[#ff4466] border-[#ff4466]/20"
              }`}>
                {entry.side.toUpperCase()}
              </Badge>
            )}
            {entry.strategy && (
              <Badge variant="secondary" className="text-[10px] bg-white/5 text-[#888888] border-white/10">
                {entry.strategy}
              </Badge>
            )}
          </div>
          {pnl !== null && (
            <div className={`flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-mono font-medium ${
              isProfit ? "bg-[#00d4aa]/10 text-[#00d4aa]" : isLoss ? "bg-[#ff4466]/10 text-[#ff4466]" : "bg-white/5 text-[#888888]"
            }`}>
              {isProfit ? <TrendingUp className="size-3" /> : isLoss ? <TrendingDown className="size-3" /> : null}
              ${Math.abs(pnl).toFixed(2)}
            </div>
          )}
        </div>

        {/* Rationale */}
        {entry.rationale && (
          <div className="mb-3">
            <p className="text-sm text-[#d4d4d4] leading-[1.8] font-[350]" style={{ fontFamily: "'Georgia', 'Times New Roman', serif" }}>
              {entry.rationale}
            </p>
          </div>
        )}

        {/* Outcome */}
        {entry.outcome && (
          <div className="mb-3 pl-3 border-l-2 border-[#4a9eff]/30">
            <p className="text-xs text-[#a0a0a0] leading-relaxed italic">{entry.outcome}</p>
          </div>
        )}

        {/* Lesson */}
        {entry.lesson && (
          <div className="flex items-start gap-2 mt-3 p-3 rounded-lg bg-[#ffa500]/[0.05] border border-[#ffa500]/[0.1]">
            <Brain className="size-3.5 text-[#ffa500] mt-0.5 shrink-0" />
            <p className="text-xs text-[#ffa500]/80 leading-relaxed" style={{ fontFamily: "'Georgia', 'Times New Roman', serif" }}>
              {entry.lesson}
            </p>
          </div>
        )}

        {/* Tags */}
        {entry.tags && (
          <div className="flex flex-wrap gap-1.5 mt-3">
            {entry.tags.split(",").map((tag, i) => (
              <span key={i} className="text-[10px] px-2 py-0.5 rounded-full bg-white/[0.03] border border-white/[0.06] text-[#666666]">
                {tag.trim()}
              </span>
            ))}
          </div>
        )}

        {/* Trade details */}
        {entry.price && entry.qty && (
          <div className="flex items-center gap-4 mt-3 pt-3 border-t border-white/[0.04]">
            <span className="text-[10px] text-[#666666] font-mono">
              {entry.qty} @ ${entry.price.toLocaleString()}
            </span>
            {entry.trade_id && (
              <span className="text-[10px] text-[#555555] font-mono">
                Trade #{entry.trade_id}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function DailyJournalCard({ journal, isSelected, onClick }: {
  journal: DailyJournal;
  isSelected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-4 py-3 rounded-lg border transition-all ${
        isSelected
          ? "bg-[#4a9eff]/10 border-[#4a9eff]/30"
          : "bg-white/[0.02] border-white/[0.06] hover:bg-white/[0.04] hover:border-white/[0.1]"
      }`}
    >
      <div className="flex items-center justify-between mb-1">
        <span className={`text-xs font-medium ${isSelected ? "text-[#4a9eff]" : "text-[#e8e8e8]"}`}>
          {formatDateShort(journal.timestamp)}
        </span>
        <span className="text-[10px] text-[#666666]">{timeAgo(journal.timestamp)}</span>
      </div>
      <p className="text-[11px] text-[#888888] line-clamp-2 leading-relaxed">
        {journal.content.replace(/[#*\-]/g, "").slice(0, 120)}...
      </p>
    </button>
  );
}

// --- Main Page ---

type Tab = "daily" | "trade_log" | "weekly" | "heatmap" | "tearsheets";

export function Journal() {
  const [tab, setTab] = useState<Tab>("daily");

  // Daily journals (auto-generated, stored)
  const [dailyJournals, setDailyJournals] = useState<DailyJournal[]>([]);
  const [selectedDaily, setSelectedDaily] = useState<number>(0);
  const [showDailyDetail, setShowDailyDetail] = useState(false);
  const [loadingDaily, setLoadingDaily] = useState(true);

  // Weekly reviews (auto-generated, stored)
  const [weeklyReviews, setWeeklyReviews] = useState<WeeklyReview[]>([]);
  const [selectedWeekly, setSelectedWeekly] = useState<number>(0);
  const [showWeeklyDetail, setShowWeeklyDetail] = useState(false);
  const [loadingWeekly, setLoadingWeekly] = useState(true);

  const isMobile = useIsMobile();

  // Trade log (per-trade journal entries)
  const [entries, setEntries] = useState<JournalEntry[]>([]);
  const [loadingEntries, setLoadingEntries] = useState(true);
  const [historyPage, setHistoryPage] = useState(0);
  const ENTRIES_PER_PAGE = 20;

  // Heatmap: fetch pnl history
  const { data: pnlHistory } = usePolling<Array<{ date: string; pnl: number; trade_count?: number }>>(
    api.pnlHistory,
    60000
  );

  // AI Tear Sheets
  const [tearSheets, setTearSheets] = useState<Array<{ id: string; timestamp: string; preview: string; content: string }>>([]);
  const [loadingTearSheets, setLoadingTearSheets] = useState(false);
  const [generatingTearSheet, setGeneratingTearSheet] = useState(false);
  const [selectedTearSheet, setSelectedTearSheet] = useState<number>(0);

  const loadDaily = useCallback(async () => {
    setLoadingDaily(true);
    try {
      const res = await fetchAPI<{ journals: DailyJournal[] }>(api.journalDaily + "?limit=30");
      setDailyJournals(res.journals || []);
    } catch {
      setDailyJournals([]);
    } finally {
      setLoadingDaily(false);
    }
  }, []);

  const loadWeekly = useCallback(async () => {
    setLoadingWeekly(true);
    try {
      const res = await fetchAPI<{ reviews: WeeklyReview[] }>(api.journalWeekly + "?limit=10");
      setWeeklyReviews(res.reviews || []);
    } catch {
      setWeeklyReviews([]);
    } finally {
      setLoadingWeekly(false);
    }
  }, []);

  const loadEntries = useCallback(async () => {
    setLoadingEntries(true);
    try {
      const res = await fetchAPI<{ entries: JournalEntry[] }>(api.journalEntries + "?limit=100");
      setEntries(res.entries || []);
    } catch {
      setEntries([]);
    } finally {
      setLoadingEntries(false);
    }
  }, []);

  const loadTearSheets = useCallback(async () => {
    setLoadingTearSheets(true);
    try {
      const res = await fetchAPI<Array<{ id: string; timestamp: string; preview: string; content: string }>>(api.reviews);
      setTearSheets(Array.isArray(res) ? res : []);
    } catch {
      setTearSheets([]);
    } finally {
      setLoadingTearSheets(false);
    }
  }, []);

  const generateTearSheet = async () => {
    setGeneratingTearSheet(true);
    try {
      await fetchAPI(api.reviewsGenerate, { method: 'POST', body: JSON.stringify({ days: 7 }) });
      // Poll until the new sheet appears
      setTimeout(() => loadTearSheets(), 3000);
    } catch { /* ignore */ } finally {
      setGeneratingTearSheet(false);
    }
  };

  useEffect(() => {
    loadDaily();
    loadWeekly();
    loadEntries();
    loadTearSheets();
  }, [loadDaily, loadWeekly, loadEntries, loadTearSheets]);

  // Paginated trade log
  const grouped = groupByDate(entries);
  const allDates = Array.from(grouped.keys()).sort((a, b) => b.localeCompare(a));
  const totalPages = Math.max(1, Math.ceil(allDates.length / ENTRIES_PER_PAGE));
  const pagedDates = allDates.slice(historyPage * ENTRIES_PER_PAGE, (historyPage + 1) * ENTRIES_PER_PAGE);

  const currentDaily = dailyJournals[selectedDaily] || null;
  const currentWeekly = weeklyReviews[selectedWeekly] || null;

  const tabs: { key: Tab; label: string; icon: typeof BookOpen }[] = [
    { key: "daily", label: "Daily", icon: Sparkles },
    { key: "trade_log", label: "Trade Log", icon: FileText },
    { key: "weekly", label: "Weekly", icon: BookMarked },
    { key: "heatmap", label: "Heatmap", icon: BarChart2 },
    { key: "tearsheets", label: "AI Reports", icon: Brain },
  ];

  return (
    <div className="p-4 sm:p-6 max-w-4xl mx-auto">
      {/* Page Header */}
      <div className="mb-8">
        <div className="flex items-center gap-4 mb-2">
          <div className="p-3 rounded-xl bg-gradient-to-br from-[#4a9eff]/15 to-[#4a9eff]/15 border border-[#4a9eff]/20">
            <BookOpen className="size-7 text-[#4a9eff]" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-[#e8e8e8] tracking-wider">TRADING JOURNAL</h1>
            <p className="text-xs text-[#666666] tracking-[0.15em] uppercase">Insights, Lessons & Performance Records</p>
          </div>
        </div>
        <div className="mt-4 h-px bg-gradient-to-r from-[#4a9eff]/30 via-white/[0.08] to-[#4a9eff]/30" />
      </div>

      {/* Tab Selector */}
      <div className="flex gap-1 mb-6 p-1 rounded-xl bg-white/[0.02] border border-white/[0.06]">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg text-xs font-medium tracking-wider uppercase transition-all ${
              tab === t.key
                ? "bg-white/[0.08] text-[#e8e8e8] border border-white/[0.1]"
                : "text-[#666666] hover:text-[#888888] border border-transparent"
            }`}
          >
            <t.icon className="size-3.5" />
            {t.label}
          </button>
        ))}
      </div>

      {/* === DAILY TAB === */}
      {tab === "daily" && (
        <div className="grid grid-cols-1 lg:grid-cols-[280px_1fr] gap-4">
          {/* Sidebar — journal list */}
          <Card className={`bg-[#0a0a0a]/80 border-white/[0.06] ${isMobile && showDailyDetail ? "hidden" : "block"}`}>
            <CardContent className="p-4">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-xs font-medium text-[#888888] uppercase tracking-wider">Entries</h3>
                <span className="text-[10px] text-[#555555]">{dailyJournals.length} total</span>
              </div>
              {loadingDaily ? (
                <div className="flex items-center justify-center py-8">
                  <RefreshCw className="size-4 text-[#4a9eff] animate-spin" />
                </div>
              ) : dailyJournals.length === 0 ? (
                <div className="text-center py-8">
                  <Sparkles className="size-8 text-[#333333] mx-auto mb-2" />
                  <p className="text-xs text-[#666666]">No daily journals yet</p>
                  <p className="text-[10px] text-[#555555] mt-1">Generated automatically at end of day</p>
                </div>
              ) : (
                <div className="space-y-2 max-h-[60vh] overflow-y-auto">
                  {dailyJournals.map((j, i) => (
                    <DailyJournalCard
                      key={j.id}
                      journal={j}
                      isSelected={i === selectedDaily}
                      onClick={() => { setSelectedDaily(i); setShowDailyDetail(true); }}
                    />
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          {/* Main content — selected journal */}
          <Card className={`bg-[#0a0a0a]/80 border-white/[0.06] ${isMobile && !showDailyDetail ? "hidden" : "block"}`}>
            <CardContent className="p-5 sm:p-8">
              {isMobile && (
                <button
                  onClick={() => setShowDailyDetail(false)}
                  className="mb-6 flex items-center gap-1 px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-[#888888] text-xs hover:bg-white/10 transition-colors"
                >
                  <ChevronLeft className="size-3" /> Back to entries
                </button>
              )}
              {currentDaily ? (
                <div>
                  <div className="flex items-center justify-between mb-6 pb-4 border-b border-white/[0.06]">
                    <div className="flex items-center gap-3">
                      <div className="p-2 rounded-lg bg-[#4a9eff]/10 border border-[#4a9eff]/20">
                        <Sparkles className="size-5 text-[#4a9eff]" />
                      </div>
                      <div>
                        <h3 className="text-lg font-semibold text-[#e8e8e8] tracking-wide">
                          {formatDateFull(currentDaily.timestamp)}
                        </h3>
                        <p className="text-xs text-[#666666]">{timeAgo(currentDaily.timestamp)}</p>
                      </div>
                    </div>
                    {/* Pagination arrows */}
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => setSelectedDaily(Math.min(dailyJournals.length - 1, selectedDaily + 1))}
                        disabled={selectedDaily >= dailyJournals.length - 1}
                        className="p-1.5 rounded-lg hover:bg-white/5 text-[#888888] disabled:opacity-30 transition-colors"
                      >
                        <ChevronLeft className="size-4" />
                      </button>
                      <span className="text-[10px] text-[#666666] font-mono min-w-[40px] text-center">
                        {selectedDaily + 1}/{dailyJournals.length}
                      </span>
                      <button
                        onClick={() => setSelectedDaily(Math.max(0, selectedDaily - 1))}
                        disabled={selectedDaily <= 0}
                        className="p-1.5 rounded-lg hover:bg-white/5 text-[#888888] disabled:opacity-30 transition-colors"
                      >
                        <ChevronRight className="size-4" />
                      </button>
                    </div>
                  </div>
                  <div>{renderMarkdownContent(currentDaily.content, "#4a9eff")}</div>
                </div>
              ) : (
                <div className="flex flex-col items-center justify-center py-16 text-center">
                  <BookOpen className="size-12 text-[#333333] mb-4" />
                  <p className="text-sm text-[#666666] mb-1">No daily journal entries yet</p>
                  <p className="text-xs text-[#555555]">The system generates a journal at the end of each trading day</p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* === WEEKLY TAB === */}
      {tab === "weekly" && (
        <Card className="bg-[#0a0a0a]/80 border-white/[0.06]">
          <CardContent className="p-5 sm:p-8">
            {loadingWeekly ? (
              <div className="flex items-center justify-center py-16">
                <RefreshCw className="size-6 text-[#4a9eff] animate-spin" />
              </div>
            ) : weeklyReviews.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <BookMarked className="size-12 text-[#333333] mb-4" />
                <p className="text-sm text-[#666666] mb-1">No weekly reviews yet</p>
                <p className="text-xs text-[#555555]">Generated automatically every Sunday at midnight</p>
              </div>
            ) : (
              <div>
                <div className="flex items-center justify-between mb-6 pb-4 border-b border-white/[0.06]">
                  <div className="flex items-center gap-3">
                    <div className="p-2 rounded-lg bg-[#4a9eff]/10 border border-[#4a9eff]/20">
                      <BookMarked className="size-5 text-[#4a9eff]" />
                    </div>
                    <div>
                      <h3 className="text-lg font-semibold text-[#e8e8e8] tracking-wide">
                        Week of {formatDateShort(currentWeekly?.timestamp || "")}
                      </h3>
                      <p className="text-xs text-[#666666]">{currentWeekly ? timeAgo(currentWeekly.timestamp) : ""}</p>
                    </div>
                  </div>
                  {weeklyReviews.length > 1 && (
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => setSelectedWeekly(Math.min(weeklyReviews.length - 1, selectedWeekly + 1))}
                        disabled={selectedWeekly >= weeklyReviews.length - 1}
                        className="p-1.5 rounded-lg hover:bg-white/5 text-[#888888] disabled:opacity-30 transition-colors"
                      >
                        <ChevronLeft className="size-4" />
                      </button>
                      <span className="text-[10px] text-[#666666] font-mono min-w-[40px] text-center">
                        {selectedWeekly + 1}/{weeklyReviews.length}
                      </span>
                      <button
                        onClick={() => setSelectedWeekly(Math.max(0, selectedWeekly - 1))}
                        disabled={selectedWeekly <= 0}
                        className="p-1.5 rounded-lg hover:bg-white/5 text-[#888888] disabled:opacity-30 transition-colors"
                      >
                        <ChevronRight className="size-4" />
                      </button>
                    </div>
                  )}
                </div>
                {currentWeekly && (
                  <div>{renderMarkdownContent(currentWeekly.content, "#4a9eff")}</div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* === TRADE LOG TAB === */}
      {tab === "trade_log" && (
        <Card className="bg-[#0a0a0a]/80 border-white/[0.06]">
          <CardContent className="p-5 sm:p-8">
            <div className="flex items-center justify-between mb-6 pb-4 border-b border-white/[0.06]">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-[#00d4aa]/10 border border-[#00d4aa]/20">
                  <FileText className="size-5 text-[#00d4aa]" />
                </div>
                <div>
                  <h3 className="text-lg font-semibold text-[#e8e8e8] tracking-wide">Trade Log</h3>
                  <p className="text-xs text-[#666666]">
                    {entries.length} entries across {allDates.length} days
                  </p>
                </div>
              </div>
            </div>

            {loadingEntries ? (
              <div className="flex flex-col items-center justify-center py-16">
                <RefreshCw className="size-6 text-[#4a9eff] animate-spin mb-3" />
                <p className="text-sm text-[#666666]">Loading entries...</p>
              </div>
            ) : entries.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <FileText className="size-12 text-[#333333] mb-4" />
                <p className="text-sm text-[#666666] mb-1">No journal entries yet</p>
                <p className="text-xs text-[#555555]">Entries are created automatically as trades execute</p>
              </div>
            ) : (
              <>
                {pagedDates.map((date) => (
                  <div key={date}>
                    <PageDivider date={date} />
                    {grouped.get(date)!.map((entry) => (
                      <JournalEntryCard key={entry.id} entry={entry} />
                    ))}
                  </div>
                ))}

                {totalPages > 1 && (
                  <div className="flex items-center justify-center gap-4 mt-8 pt-6 border-t border-white/[0.06]">
                    <button
                      onClick={() => setHistoryPage(Math.max(0, historyPage - 1))}
                      disabled={historyPage === 0}
                      className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-[#888888] text-xs hover:bg-white/10 disabled:opacity-30 transition-colors"
                    >
                      <ChevronLeft className="size-3" />
                      Prev
                    </button>
                    <span className="text-xs text-[#666666] font-mono">
                      Page {historyPage + 1} of {totalPages}
                    </span>
                    <button
                      onClick={() => setHistoryPage(Math.min(totalPages - 1, historyPage + 1))}
                      disabled={historyPage >= totalPages - 1}
                      className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-[#888888] text-xs hover:bg-white/10 disabled:opacity-30 transition-colors"
                    >
                      Next
                      <ChevronRight className="size-3" />
                    </button>
                  </div>
                )}
              </>
            )}
          </CardContent>
        </Card>
      )}

      {/* === HEATMAP TAB === */}
      {tab === "heatmap" && (
        <Card className="bg-[#0a0a0a]/80 border-white/[0.06]">
          <CardContent className="p-5 sm:p-8">
            <div className="flex items-center gap-3 mb-6 pb-4 border-b border-white/[0.06]">
              <div className="p-2 rounded-lg bg-[#4a9eff]/10 border border-[#4a9eff]/20">
                <BarChart2 className="size-5 text-[#4a9eff]" />
              </div>
              <div>
                <h3 className="text-lg font-semibold text-[#e8e8e8] tracking-wide">P&L Heatmap</h3>
                <p className="text-xs text-[#666666]">365-day trading performance calendar</p>
              </div>
            </div>
            <PnLHeatmap pnlHistory={pnlHistory ?? []} />
          </CardContent>
        </Card>
      )}

      {/* === AI TEAR SHEETS TAB === */}
      {tab === "tearsheets" && (
        <Card className="bg-[#0a0a0a]/80 border-white/[0.06]">
          <CardContent className="p-5 sm:p-8">
            <div className="flex items-center justify-between mb-6 pb-4 border-b border-white/[0.06]">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-[#4a9eff]/10 border border-[#4a9eff]/20">
                  <Brain className="size-5 text-[#4a9eff]" />
                </div>
                <div>
                  <h3 className="text-lg font-semibold text-[#e8e8e8] tracking-wide">AI Tear Sheets</h3>
                  <p className="text-xs text-[#666666]">LLM-generated post-trade performance critiques</p>
                </div>
              </div>
              <button
                onClick={generateTearSheet}
                disabled={generatingTearSheet}
                className="flex items-center gap-2 px-4 py-2 rounded-lg bg-[#4a9eff]/15 border border-[#4a9eff]/30 text-[#4a9eff] text-xs font-medium hover:bg-[#4a9eff]/25 transition-colors disabled:opacity-50"
              >
                {generatingTearSheet ? <Loader2 className="size-3.5 animate-spin" /> : <Brain className="size-3.5" />}
                {generatingTearSheet ? 'Generating...' : 'Generate New Report'}
              </button>
            </div>

            {loadingTearSheets ? (
              <div className="flex flex-col items-center justify-center py-16">
                <Loader2 className="size-6 text-[#4a9eff] animate-spin mb-3" />
                <p className="text-sm text-[#666666]">Loading reports...</p>
              </div>
            ) : tearSheets.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <Brain className="size-12 text-[#333333] mb-4" />
                <p className="text-sm text-[#666666] mb-2">No AI tear sheets yet</p>
                <p className="text-xs text-[#555555] mb-6">Generate your first report to get a clinical critique of recent trade performance</p>
                <button
                  onClick={generateTearSheet}
                  disabled={generatingTearSheet}
                  className="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-[#4a9eff]/15 border border-[#4a9eff]/30 text-[#4a9eff] text-sm font-medium hover:bg-[#4a9eff]/25 transition-colors disabled:opacity-50"
                >
                  {generatingTearSheet ? <Loader2 className="size-4 animate-spin" /> : <Brain className="size-4" />}
                  {generatingTearSheet ? 'Generating...' : 'Generate First Report'}
                </button>
              </div>
            ) : (
              <div className="grid grid-cols-1 lg:grid-cols-[240px_1fr] gap-4">
                {/* Sidebar — report list */}
                <div className="space-y-2">
                  {tearSheets.map((sheet, i) => (
                    <button
                      key={sheet.id}
                      onClick={() => setSelectedTearSheet(i)}
                      className={`w-full text-left p-3 rounded-xl border transition-all duration-200 ${
                        selectedTearSheet === i
                          ? 'bg-[#4a9eff]/10 border-[#4a9eff]/30'
                          : 'bg-white/[0.02] border-white/[0.06] hover:bg-white/5'
                      }`}
                    >
                      <p className={`text-xs font-medium ${selectedTearSheet === i ? 'text-[#4a9eff]' : 'text-[#e8e8e8]'}`}>
                        {new Date(sheet.timestamp).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })}
                      </p>
                      <p className="text-[10px] text-[#666666] mt-1 line-clamp-2 leading-relaxed">
                        {sheet.preview.replace(/[#*]/g, '').slice(0, 80)}...
                      </p>
                    </button>
                  ))}
                </div>

                {/* Report content */}
                <div className="min-h-[400px]">
                  {tearSheets[selectedTearSheet] && (
                    <div className="prose">
                      {renderMarkdownContent(tearSheets[selectedTearSheet].content, '#4a9eff')}
                    </div>
                  )}
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Footer */}
      <div className="mt-6 flex items-center justify-center gap-3 opacity-40">
        <div className="w-8 h-px bg-white/20" />
        <BookOpen className="size-3 text-[#666666]" />
        <span className="text-[10px] text-[#666666] tracking-[0.2em] uppercase">OT-CORP Trading Journal</span>
        <BookOpen className="size-3 text-[#666666]" />
        <div className="w-8 h-px bg-white/20" />
      </div>
    </div>
  );
}
