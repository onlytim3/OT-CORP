import { Outlet, Link, useLocation } from "react-router";
import { LayoutDashboard, TrendingUp, Bot, BarChart3, Activity, MessageSquare, Sun, Moon, AlertTriangle } from "lucide-react";
import { cn } from "./ui/utils";
import { useState } from "react";
import { ChatPanel } from "./ChatPanel";
import { api, usePolling, fetchAPI } from "../config/api";

export function DashboardLayout() {
  const location = useLocation();
  const [chatOpen, setChatOpen] = useState(false);
  const [showModeConfirm, setShowModeConfirm] = useState(false);
  const [switching, setSwitching] = useState(false);
  const { data: modeData, refresh: refreshMode } = usePolling<{ mode: string }>(api.mode, 30000);

  const mode = modeData?.mode || 'paper';

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
    { path: "/agents", icon: Bot, label: "Agents" },
    { path: "/analytics", icon: BarChart3, label: "Analytics" },
  ];

  return (
    <div className="flex flex-col h-screen bg-black">
      {/* Top Header */}
      <header className="h-16 bg-black border-b border-white/8 flex items-center justify-between px-6">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-lg bg-[#4a9eff] border border-[#4a9eff]/50">
            <Activity className="size-6 text-black" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-[#e8e8e8] tracking-wider">OT-CORP</h1>
            <p className="text-[10px] text-[#666666] uppercase tracking-[0.2em]">Trading Terminal</p>
          </div>
        </div>

        <div className="flex items-center gap-4">
          {/* Mode Toggle */}
          <button
            onClick={switchMode}
            disabled={switching}
            className={cn(
              "flex items-center gap-2 px-4 py-2 rounded-lg border cursor-pointer transition-colors disabled:opacity-50",
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

          {/* System Status */}
          <div className="flex items-center gap-2 px-4 py-2 rounded-lg bg-[#00d4aa]/10 border border-[#00d4aa]/30">
            <div className="size-2 rounded-full bg-[#00d4aa] animate-pulse" />
            <span className="text-xs text-[#00d4aa] font-medium tracking-wider">ONLINE</span>
          </div>
        </div>
      </header>

      {/* Live Mode Confirmation Modal */}
      {showModeConfirm && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/80">
          <div className="bg-[#0a0a0a] border border-[#ff4466]/30 rounded-lg p-6 max-w-md w-full mx-4">
            <div className="flex items-center gap-3 mb-4">
              <div className="p-2 rounded-lg bg-[#ff4466]/15 border border-[#ff4466]/30">
                <AlertTriangle className="size-6 text-[#ff4466]" />
              </div>
              <h3 className="text-lg font-bold text-[#e8e8e8] uppercase tracking-wider">Switch to Live</h3>
            </div>
            <p className="text-sm text-[#888888] mb-6 leading-relaxed">
              This will execute <span className="text-[#ff4466] font-medium">real trades with real money</span>.
              Make sure your API keys and risk parameters are configured correctly.
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => setShowModeConfirm(false)}
                className="flex-1 px-4 py-2.5 rounded-lg bg-white/5 border border-white/10 text-[#c0c0c0] text-sm font-medium hover:bg-white/10 transition-colors uppercase tracking-wider"
              >
                Cancel
              </button>
              <button
                onClick={confirmLiveMode}
                disabled={switching}
                className="flex-1 px-4 py-2.5 rounded-lg bg-[#ff4466] text-white text-sm font-medium hover:bg-[#ff4466]/80 transition-colors disabled:opacity-50 uppercase tracking-wider"
              >
                {switching ? 'Switching...' : 'Confirm Live'}
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
          "fixed bottom-24 right-4 z-50 p-4 rounded-full transition-all duration-300",
          chatOpen
            ? "bg-[#ff4466] scale-90"
            : "bg-[#4a9eff] hover:bg-[#4a9eff]/80"
        )}
      >
        <MessageSquare className="size-6 text-white" />
      </button>

      {/* Chat Panel */}
      <ChatPanel isOpen={chatOpen} onClose={() => setChatOpen(false)} />

      {/* Bottom Navigation */}
      <nav className="fixed bottom-0 left-0 right-0 h-20 bg-black border-t border-white/8 z-50">
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
