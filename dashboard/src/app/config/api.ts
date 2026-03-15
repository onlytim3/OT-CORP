// API Configuration for OT-CORP Trading System
// In production (served by Flask), use same origin. In dev, use local Flask server.
const API_BASE_URL = import.meta.env.VITE_API_URL || (import.meta.env.PROD ? '' : 'http://localhost:5050');

export const api = {
  baseUrl: API_BASE_URL,
  status: `${API_BASE_URL}/api/status`,
  health: `${API_BASE_URL}/api/health`,
  mode: `${API_BASE_URL}/api/mode`,
  trades: `${API_BASE_URL}/api/trades`,
  trade: (id: number) => `${API_BASE_URL}/api/trade/${id}`,
  position: (symbol: string) => `${API_BASE_URL}/api/position/${symbol}`,
  strategies: `${API_BASE_URL}/api/strategies`,
  strategy: (name: string) => `${API_BASE_URL}/api/strategy/${name}`,
  actions: `${API_BASE_URL}/api/actions`,
  pnl: (date: string) => `${API_BASE_URL}/api/pnl/${date}`,
  intelligence: `${API_BASE_URL}/api/intelligence`,
  allocation: `${API_BASE_URL}/api/allocation`,
  agents: `${API_BASE_URL}/api/agents`,
  recommendation: (id: number) => `${API_BASE_URL}/api/recommendation/${id}`,
  volume: `${API_BASE_URL}/api/volume`,
  volumeSymbol: (symbol: string) => `${API_BASE_URL}/api/volume/${symbol}`,
  chat: `${API_BASE_URL}/api/chat`,
  chatConfirm: `${API_BASE_URL}/api/chat/confirm`,
};

// --- Types ---

export interface Account {
  portfolio_value: number;
  cash: number;
  buying_power: number;
  equity: number;
  status: string;
  paper: boolean;
}

export interface Position {
  symbol: string;
  qty: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  age?: string;
  side?: string;
}

export interface StatusResponse {
  account: Account;
  positions: Position[];
  summary: {
    total_trades: number;
    open_positions: number;
    total_signals: number;
    active_strategies: number;
  };
  mode: string;
  timestamp: string;
}

export interface Trade {
  id: number;
  timestamp: string;
  symbol: string;
  side: string;
  qty: number;
  price: number;
  total: number;
  strategy: string;
  pnl: number | null;
  pnl_pct: number | null;
  closed_at: string | null;
}

export interface ActionItem {
  id: number;
  timestamp: string;
  category: string;
  action: string;
  actor: string;
  details: string;
  data: Record<string, unknown> | null;
}

export interface Strategy {
  name: string;
  enabled: boolean;
  signals: number;
  trades: number;
  buys: number;
  sells: number;
  closed_trades: number;
  win_rate: number | null;
  total_pnl: number;
}

export interface Intelligence {
  fear_greed: { value: number; classification: string; timestamp: string } | null;
  briefings: ActionItem[];
  regime_signals: { id: number; timestamp: string; strategy: string; signal: string; strength: number; data: Record<string, unknown> | null }[];
}

export interface AgentsResponse {
  pending: Recommendation[];
  recent: Recommendation[];
  activity: ActionItem[];
}

export interface Recommendation {
  id: number;
  timestamp: string;
  from_agent: string;
  action: string;
  target: string;
  status: string;
  data: Record<string, unknown> | null;
  reasoning: string;
}

export interface VolumeAnalysis {
  symbol: string;
  aster_symbol?: string;
  ratio: number;
  trend: number;
  spread_bps: number;
  recent_quote_volume: number;
  sizing_multiplier: number;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  action_id?: string;
  confirmation_required?: boolean;
}

// --- Fetch helper ---

let connectionFailed = false;

export async function fetchAPI<T>(url: string, options?: RequestInit): Promise<T> {
  try {
    const response = await fetch(url, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options?.headers },
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ error: response.statusText }));
      throw new Error(error.error || `HTTP ${response.status}`);
    }
    connectionFailed = false;
    return await response.json();
  } catch (error) {
    if (!connectionFailed) console.warn('API Error, using mock data:', error);
    connectionFailed = true;
    return getMockDataForUrl<T>(url);
  }
}

export function isUsingMockData() { return connectionFailed; }

// --- Polling hook ---
import { useState, useEffect, useCallback } from 'react';

export function usePolling<T>(url: string, intervalMs = 10000) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await fetchAPI<T>(url);
      setData(result);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [url]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, intervalMs);
    return () => clearInterval(id);
  }, [refresh, intervalMs]);

  return { data, loading, error, refresh };
}

// --- Mock data ---

const MOCK_STATUS: StatusResponse = {
  account: { portfolio_value: 1000, cash: 150, buying_power: 350, equity: 850, status: 'ACTIVE', paper: true },
  positions: [
    { symbol: 'BTCUSDT', qty: 0.01, avg_cost: 45200, current_price: 47200, market_value: 472, unrealized_pnl: 20, unrealized_pnl_pct: 4.42, age: '2h' },
    { symbol: 'ETHUSDT', qty: 0.15, avg_cost: 3150, current_price: 3440, market_value: 516, unrealized_pnl: 43.5, unrealized_pnl_pct: 9.21, age: '1d' },
  ],
  summary: { total_trades: 12, open_positions: 2, total_signals: 45, active_strategies: 18 },
  mode: 'paper',
  timestamp: new Date().toISOString(),
};

function getMockDataForUrl<T>(url: string): T {
  if (url.includes('/api/status')) return MOCK_STATUS as T;
  if (url.includes('/api/actions')) return [] as T;
  if (url.includes('/api/strategies')) return [
    { name: 'kalman_trend', enabled: true, signals: 24, trades: 8, buys: 5, sells: 3, closed_trades: 3, win_rate: 66.7, total_pnl: 42.50 },
    { name: 'hmm_regime', enabled: true, signals: 18, trades: 5, buys: 3, sells: 2, closed_trades: 2, win_rate: 50.0, total_pnl: 12.30 },
  ] as T;
  if (url.includes('/api/trades')) return [] as T;
  if (url.includes('/api/intelligence')) return { fear_greed: null, briefings: [], regime_signals: [] } as T;
  if (url.includes('/api/agents')) return { pending: [], recent: [], activity: [] } as T;
  if (url.includes('/api/volume')) return [] as T;
  if (url.includes('/api/mode')) return { mode: 'paper' } as T;
  return {} as T;
}
