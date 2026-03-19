import { Outlet, Link, useLocation } from "react-router";
import { LayoutDashboard, TrendingUp, Bot, BarChart3, Activity, MessageSquare, Sun, Moon, AlertTriangle, Shield, Flame, Zap, ChevronDown, Clock, BookOpen, RefreshCw } from "lucide-react";
import { cn } from "./ui/utils";
import { useState, useRef, useEffect } from "react";
import { ChatPanel } from "./ChatPanel";
import { api, usePolling, fetchAPI } from "../config/api";

// --- Trading Sessions ---
interface TradingSession {
  name: string;
  short: string;
  color: string;
  // Hours in UTC
  startUTC: number;
  endUTC: number;
}

const SESSIONS: TradingSession[] = [
  { name: 'Sydney',    short: 'SYD', color: '#c084fc', startUTC: 22, endUTC: 7 },
  { name: 'Tokyo',     short: 'TKY', color: '#f472b6', startUTC: 0,  endUTC: 9 },
  { name: 'London',    short: 'LDN', color: '#4a9eff', startUTC: 8,  endUTC: 17 },
  { name: 'New York',  short: 'NYC', color: '#00d4aa', startUTC: 13, endUTC: 22 },
];

function isSessionActive(s: TradingSession, utcHour: number): boolean {
  if (s.startUTC < s.endUTC) {
    return utcHour >= s.startUTC && utcHour < s.endUTC;
  }
  // Wraps midnight (e.g. Sydney 22-7)
  return utcHour >= s.startUTC || utcHour < s.endUTC;
}

function TradingClock() {
  const [now, setNow] = useState(new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  const utcHour = now.getUTCHours();
  const activeSessions = SESSIONS.filter(s => isSessionActive(s, utcHour));
  const localTime = now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

  // Determine overlap label
  const overlapLabel = activeSessions.length >= 2
    ? activeSessions.map(s => s.short).join(' + ')
    : activeSessions.length === 1
    ? activeSessions[0].name
    : 'Crypto Only';

  const primaryColor = activeSessions.length >= 2
    ? '#ffa500' // overlap = high volume
    : activeSessions.length === 1
    ? activeSessions[0].color
    : '#888888';

  return (
    <div className="hidden sm:flex items-center gap-2 px-3 py-1.5 rounded-lg bg-white/5 border border-white/[0.08]">
      <Clock className="size-3.5 text-[#888888]" />
      <span className="text-sm font-mono text-[#e8e8e8] tabular-nums">{localTime}</span>
      <div className="w-px h-4 bg-white/10" />
      <div className="flex items-center gap-1.5">
        {activeSessions.length >= 2 && (
          <span className="relative flex size-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full opacity-75" style={{ backgroundColor: primaryColor }} />
            <span className="relative inline-flex rounded-full size-2" style={{ backgroundColor: primaryColor }} />
          </span>
        )}
        {activeSessions.length === 1 && (
          <span className="relative flex size-2">
            <span className="relative inline-flex rounded-full size-2" style={{ backgroundColor: primaryColor }} />
          </span>
        )}
        <span className="text-[10px] font-medium uppercase tracking-wider" style={{ color: primaryColor }}>
          {overlapLabel}
        </span>
      </div>
    </div>
  );
}

type ProfileKey = 'conservative' | 'moderate' | 'aggressive' | 'greedy';

const PROFILE_CONFIG: Record<ProfileKey, { label: string; icon: typeof Shield; color: string; short: string }> = {
  conservative: { label: 'Conservative', icon: Shield, color: '#00d4aa', short: 'SAFE' },
  moderate: { label: 'Moderate', icon: BarChart3, color: '#4a9eff', short: 'MOD' },
  aggressive: { label: 'Aggressive', icon: Flame, color: '#ffa500', short: 'AGG' },
  greedy: { label: 'Greedy', icon: Zap, color: '#ff4466', short: 'MAX' },
};

export function DashboardLayout() {
  const location = useLocation();
  const [chatOpen, setChatOpen] = useState(false);
  const [showModeConfirm, setShowModeConfirm] = useState(false);
  const [showProfileMenu, setShowProfileMenu] = useState(false);
  const [showGreedyConfirm, setShowGreedyConfirm] = useState(false);
  const [switching, setSwitching] = useState(false);
  const profileRef = useRef<HTMLDivElement>(null);
  const { data: modeData, refresh: refreshMode } = usePolling<{ mode: string }>(api.mode, 30000);
  const { data: profileData, refresh: refreshProfile } = usePolling<{ profile: string }>(api.profile, 30000);

  const mode = modeData?.mode || 'paper';
  const profile = (profileData?.profile || 'aggressive') as ProfileKey;
  const profileInfo = PROFILE_CONFIG[profile] || PROFILE_CONFIG.aggressive;

  // Close profile menu on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (profileRef.current && !profileRef.current.contains(e.target as Node)) {
        setShowProfileMenu(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const switchProfile = async (target: ProfileKey) => {
    if (target === profile) { setShowProfileMenu(false); return; }
    if (target === 'greedy') { setShowGreedyConfirm(true); setShowProfileMenu(false); return; }
    setSwitching(true);
    try {
      await fetchAPI(api.profile, { method: 'POST', body: JSON.stringify({ profile: target }) });
      refreshProfile();
    } catch { /* ignore */ } finally { setSwitching(false); setShowProfileMenu(false); }
  };

  const confirmGreedy = async () => {
    setSwitching(true);
    try {
      await fetchAPI(api.profile, { method: 'POST', body: JSON.stringify({ profile: 'greedy', confirm: true }) });
      refreshProfile();
    } catch { /* ignore */ } finally { setSwitching(false); setShowGreedyConfirm(false); }
  };

  const switchMode = async () => {
    const target = mode === 'paper' ? 'live' : 'paper';

    // Switching to live requires confirmation
    if (target === 'live') {
      setShowModeConfirm(true);
      return;
    }

    setSwitching(true);
    try {
      await fetchAPI(api.mode, {
        method: 'POST',
        body: JSON.stringify({ mode: target }),
      });
      refreshMode();
    } catch {
      // ignore
    } finally {
      setSwitching(false);
    }
  };

  const confirmLiveMode = async () => {
    setSwitching(true);
    try {
      await fetchAPI(api.mode, {
        method: 'POST',
        body: JSON.stringify({ mode: 'live', confirm: true }),
      });
      refreshMode();
    } catch {
      // ignore
    } finally {
      setSwitching(false);
      setShowModeConfirm(false);
    }
  };

  const navItems = [
    { path: "/", icon: LayoutDashboard, label: "Overview" },
    { path: "/trading", icon: TrendingUp, label: "Trading" },
    { path: "/journal", icon: BookOpen, label: "Journal" },
    { path: "/agents", icon: Bot, label: "Agents" },
    { path: "/analytics", icon: BarChart3, label: "Analytics" },
  ];

  return (
    <div className="flex flex-col h-screen">
      {/* Top Header */}
      <header className="h-16 bg-[#0a0a0a] border-b border-white/[0.06] flex items-center justify-between px-6 sticky top-0 z-40">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-lg bg-[#4a9eff] border border-[#4a9eff]/50">
            <Activity className="size-6 text-black" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-[#e8e8e8] tracking-wider">OT-CORP</h1>
            <p className="text-[10px] text-[#666666] uppercase tracking-[0.2em]">Trading Terminal</p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {/* Trading Clock */}
          <TradingClock />
          {/* Profile Selector */}
          <div className="relative" ref={profileRef}>
            <button
              onClick={() => setShowProfileMenu(!showProfileMenu)}
              disabled={switching}
              className="flex items-center gap-2 px-3 py-2 rounded-lg border cursor-pointer transition-colors disabled:opacity-50"
              style={{
                backgroundColor: `${profileInfo.color}10`,
                borderColor: `${profileInfo.color}30`,
              }}
            >
              <profileInfo.icon className="size-4" style={{ color: profileInfo.color }} />
              <span className="text-xs font-medium tracking-wider hidden sm:inline" style={{ color: profileInfo.color }}>
                {profileInfo.label.toUpperCase()}
              </span>
              <span className="text-xs font-medium tracking-wider sm:hidden" style={{ color: profileInfo.color }}>
                {profileInfo.short}
              </span>
              <ChevronDown className="size-3" style={{ color: profileInfo.color }} />
            </button>

            {/* Profile Dropdown */}
            {showProfileMenu && (
              <div className="absolute right-0 top-12 z-[60] w-56 bg-[#0a0a0a] border border-white/[0.1] rounded-xl shadow-2xl shadow-black/50 overflow-hidden">
                <div className="p-2 border-b border-white/5">
                  <p className="text-[10px] text-[#888888] uppercase tracking-wider px-2">Trading Mentality</p>
                </div>
                {(Object.keys(PROFILE_CONFIG) as ProfileKey[]).map((key) => {
                  const cfg = PROFILE_CONFIG[key];
                  const isActive = key === profile;
                  return (
                    <button
                      key={key}
                      onClick={() => switchProfile(key)}
                      className={cn(
                        "w-full flex items-center gap-3 px-4 py-3 text-left transition-colors",
                        isActive ? "bg-white/10" : "hover:bg-white/5"
                      )}
                    >
                      <cfg.icon className="size-4 shrink-0" style={{ color: cfg.color }} />
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium" style={{ color: isActive ? cfg.color : '#e8e8e8' }}>
                          {cfg.label}
                        </p>
                      </div>
                      {isActive && <div className="size-2 rounded-full shrink-0" style={{ backgroundColor: cfg.color }} />}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          {/* Mode Toggle */}
          <button
            onClick={switchMode}
            disabled={switching}
            className={cn(
              "flex items-center gap-2 px-3 py-2 rounded-lg border cursor-pointer transition-colors disabled:opacity-50",
              mode === 'paper'
                ? 'bg-[#4a9eff]/10 border-[#4a9eff]/30 hover:bg-[#4a9eff]/20'
                : 'bg-[#00d4aa]/10 border-[#00d4aa]/30 hover:bg-[#00d4aa]/20'
            )}
          >
            {mode === 'paper' ? <Moon className="size-4 text-[#4a9eff]" /> : <Sun className="size-4 text-[#00d4aa]" />}
            <span className={`text-xs font-medium tracking-wider ${mode === 'paper' ? 'text-[#4a9eff]' : 'text-[#00d4aa]'}`}>
              {mode.toUpperCase()}
            </span>
          </button>

          {/* Refresh Button */}
          <button
            onClick={() => window.location.reload()}
            className="p-2 rounded-lg hover:bg-white/10 transition-colors"
            title="Refresh data"
          >
            <RefreshCw className="size-4 text-[#888888]" />
          </button>

          {/* System Status */}
          <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-[#00d4aa]/10 border border-[#00d4aa]/20 ">
            <div className="size-2 rounded-full bg-[#00d4aa] animate-pulse" />
            <span className="text-xs text-[#00d4aa] font-medium tracking-wider">ONLINE</span>
          </div>
        </div>
      </header>

      {/* Live Mode Confirmation Modal */}
      {showModeConfirm && (
        <div className="fixed inset-0 z-[100] flex max-sm:items-end sm:items-center justify-center bg-black/80">
          <div className="bg-[#0a0a0a] border border-[#ff4466]/20 max-sm:rounded-t-2xl max-sm:rounded-b-none sm:rounded-xl p-5 sm:p-6 max-w-md w-full sm:mx-4 shadow-2xl shadow-black/50">
            <div className="flex items-center gap-3 mb-4">
              <div className="p-2 rounded-lg bg-[#ff4466]/15 border border-[#ff4466]/30">
                <AlertTriangle className="size-5 sm:size-6 text-[#ff4466]" />
              </div>
              <h3 className="text-base sm:text-lg font-bold text-[#e8e8e8] uppercase tracking-wider">Switch to Live</h3>
            </div>
            <p className="text-sm text-[#888888] mb-6 leading-relaxed">
              This will execute <span className="text-[#ff4466] font-medium">real trades with real money</span>.
              Make sure your API keys and risk parameters are configured correctly.
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => setShowModeConfirm(false)}
                className="flex-1 px-4 py-3 sm:py-2.5 rounded-lg bg-white/5 border border-white/10 text-[#c0c0c0] text-sm font-medium hover:bg-white/10 transition-colors uppercase tracking-wider"
              >
                Cancel
              </button>
              <button
                onClick={confirmLiveMode}
                disabled={switching}
                className="flex-1 px-4 py-3 sm:py-2.5 rounded-lg bg-[#ff4466] text-white text-sm font-medium hover:bg-[#ff4466]/80 transition-colors disabled:opacity-50 uppercase tracking-wider"
              >
                {switching ? 'Switching...' : 'Confirm Live'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Greedy Profile Confirmation Modal */}
      {showGreedyConfirm && (
        <div className="fixed inset-0 z-[100] flex max-sm:items-end sm:items-center justify-center bg-black/80">
          <div className="bg-[#0a0a0a] border border-[#ff4466]/20 max-sm:rounded-t-2xl max-sm:rounded-b-none sm:rounded-xl p-5 sm:p-6 max-w-md w-full sm:mx-4 shadow-2xl shadow-black/50">
            <div className="flex items-center gap-3 mb-4">
              <div className="p-2 rounded-lg bg-[#ff4466]/15 border border-[#ff4466]/30">
                <Zap className="size-5 sm:size-6 text-[#ff4466]" />
              </div>
              <h3 className="text-base sm:text-lg font-bold text-[#e8e8e8] uppercase tracking-wider">Switch to Greedy</h3>
            </div>
            <p className="text-sm text-[#888888] mb-2 leading-relaxed">
              Greedy mode uses up to <span className="text-[#ff4466] font-medium">10x leverage</span> with minimal cash reserves.
            </p>
            <p className="text-sm text-[#888888] mb-6 leading-relaxed">
              This can lead to <span className="text-[#ff4466] font-medium">rapid liquidation</span> in volatile markets. Only use this if you understand the risks.
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => setShowGreedyConfirm(false)}
                className="flex-1 px-4 py-3 sm:py-2.5 rounded-lg bg-white/5 border border-white/10 text-[#c0c0c0] text-sm font-medium hover:bg-white/10 transition-colors uppercase tracking-wider"
              >
                Cancel
              </button>
              <button
                onClick={confirmGreedy}
                disabled={switching}
                className="flex-1 px-4 py-3 sm:py-2.5 rounded-lg bg-[#ff4466] text-white text-sm font-medium hover:bg-[#ff4466]/80 transition-colors disabled:opacity-50 uppercase tracking-wider"
              >
                {switching ? 'Switching...' : 'Confirm Greedy'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Main Content */}
      <main className="flex-1 overflow-auto pb-20">
        <Outlet />
      </main>

      {/* Chat FAB */}
      <button
        onClick={() => setChatOpen(!chatOpen)}
        className={cn(
          "fixed bottom-24 right-4 z-50 p-4 rounded-full transition-all duration-300 shadow-lg",
          chatOpen
            ? "bg-[#ff4466] scale-90 shadow-[#ff4466]/20"
            : "bg-[#4a9eff] hover:bg-[#4a9eff]/80 shadow-[#4a9eff]/20"
        )}
      >
        <MessageSquare className="size-6 text-white" />
      </button>

      {/* Chat Panel */}
      <ChatPanel isOpen={chatOpen} onClose={() => setChatOpen(false)} />

      {/* Bottom Navigation */}
      <nav className="fixed bottom-0 left-0 right-0 h-20 bg-black border-t border-white/[0.06] z-50">
        <div className="h-full flex items-center justify-around px-4 max-w-2xl mx-auto">
          {navItems.map((item) => {
            const isActive = location.pathname === item.path;
            return (
              <Link
                key={item.path}
                to={item.path}
                className={cn(
                  "flex flex-col items-center justify-center gap-1 px-6 py-2 rounded-lg transition-all duration-200 relative min-w-[80px]",
                  isActive ? "text-[#4a9eff]" : "text-[#666666] hover:text-[#888888]"
                )}
              >
                {isActive && (
                  <div className="absolute inset-0 bg-[#4a9eff]/10 rounded-lg border border-[#4a9eff]/20" />
                )}
                <div className="relative z-10">
                  <item.icon className="size-6" />
                </div>
                <span className={cn("text-[10px] uppercase tracking-wider font-medium relative z-10", isActive && "font-semibold")}>
                  {item.label}
                </span>
              </Link>
            );
          })}
        </div>
      </nav>
    </div>
  );
}
