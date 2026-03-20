# OT-CORP System Evaluation

## What It Is

OT-CORP is an **autonomous algorithmic trading system** (~35,000 lines of Python) with a React dashboard, deployed on Render. It trades crypto perpetual futures on AsterDex (primary) and equities via Alpaca (secondary), running 22 strategies across crypto, stocks, commodities, and indices on a configurable interval (default 1-4 hours).

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Dashboard (React + Vite + Tailwind + MUI)      │
│  Served by Flask/Gunicorn                       │
├─────────────────────────────────────────────────┤
│  Scheduler (daemon loop every N hours)          │
│  ├── Strategy Registry (22 strategies)          │
│  ├── Signal Aggregator (dedup, conflict resolve)│
│  ├── Risk Manager (14 risk checks)              │
│  ├── Execution Router (AsterDex / Alpaca)       │
│  ├── Profit Manager (SL/TP/trailing)            │
│  └── Autonomous Intelligence Engine             │
│      ├── Performance Agent                      │
│      ├── Research Agent                         │
│      ├── Risk Agent                             │
│      ├── Regime Agent                           │
│      └── Learning Agent                         │
├─────────────────────────────────────────────────┤
│  Data Layer                                     │
│  ├── AsterDex API (OHLCV, orderbook, funding)   │
│  ├── Alpaca (equities, crypto quotes)           │
│  ├── FRED (macro data)                          │
│  ├── alternative.me (Fear & Greed)              │
│  └── yfinance (fallback)                        │
├─────────────────────────────────────────────────┤
│  SQLite + WAL mode (17 tables, FTS5 search)     │
│  Persistent disk on Render (/data)              │
└─────────────────────────────────────────────────┘
```

---

## Strengths

### 1. Comprehensive Risk Management (A)
The RiskManager runs **14 independent checks** before every trade: account status, buying power, volume gate, spread/market impact, position sizing, crypto exposure cap (70%), correlation group limits (50%), directional exposure (80% long / 30% short), daily loss halt (5%), max drawdown halt (20%), cash reserve, trade count, total leverage cap (5x), and sector exposure limits. ATR-based stop losses with per-asset-class multipliers (2.0x-4.0x). This is production-grade risk infrastructure.

### 2. Strategy Diversity (A-)
22 strategies spanning: technical (RSI divergence, Kalman trend, HMM regime, GARCH, breakout), market microstructure (taker divergence, OI-price divergence, whale flow), funding rate strategies (arb, term structure, forecast), cross-asset (gold-crypto hedge, equity correlation, cross-asset momentum), on-chain flow, news sentiment, pairs trading, and factor models. Strategies that lost money in backtests have been pruned. Regime-aware gating restricts strategies to appropriate market conditions.

### 3. Self-Improving Architecture (A-)
The autonomous intelligence engine with 5 specialized agents (Performance, Research, Risk, Regime, Learning) that communicate through a recommendation system. Auto-disables losing strategies (<25% win rate), auto-tightens risk during drawdowns, auto-rebalances toward winners, and re-evaluates disabled strategies via periodic backtesting. Adaptive thresholds with bounded tuning ranges prevent runaway parameter drift.

### 4. Operational Maturity (B+)
Clean deployment config (Render with persistent disk), structured logging, notification support (Discord/Telegram), web dashboard for monitoring, trade journaling, knowledge base with FTS5 search, fill quality tracking, volume profiling, and trade narrative generation via LLMs.

### 5. Clean Code Architecture (B+)
Abstract `Strategy` base class with auto-discovery registry pattern. Clear separation of concerns: data, strategy, execution, risk, learning, monitoring. Configuration centralized in `config.py` with validation on startup. SQLite with WAL mode and proper migrations.

---

## Weaknesses & Risks

### 1. SQLite Under Concurrent Load (High Risk)
The system runs Gunicorn with 2 workers + 4 threads alongside a background daemon, all hitting the same SQLite file. WAL mode helps but SQLite is not designed for concurrent write-heavy workloads. The `record_daily_pnl` function already has a retry loop for BUSY errors — a symptom of this problem. Under heavy strategy cycles, this will cause data loss or corruption.

**Recommendation:** Migrate to PostgreSQL (Render offers managed Postgres), or at minimum enforce single-writer access with a queue.

### 2. No Automated Test Coverage (High Risk)
Only 5 test files exist (`test_config.py`, `test_market_hours.py`, `test_profit_manager.py`, `test_risk_manager.py`, `test_sync.py`) for a 35,000-line codebase. No CI/CD pipeline. No integration tests for the execution path. Strategy signal generation, order routing, and the autonomous intelligence engine are untested. A regression in any of these could cause real money losses.

**Recommendation:** Add pytest CI with at minimum: strategy signal unit tests, risk manager integration tests, and paper-trade end-to-end tests.

### 3. Error Handling in Critical Paths (Medium-High Risk)
The main trading loop (`cmd_run`) wraps each strategy in a try/except that prints the error and continues. The execution router and autonomous engine have similar blanket exception handling. Silent failures in position sync or fill verification could leave the system in an inconsistent state (e.g., DB thinks a position is open but it was actually closed on-exchange).

**Recommendation:** Differentiate recoverable vs. fatal errors. Position sync failures should halt further trading until resolved.

### 4. No API Rate Limiting or Circuit Breaking (Medium Risk)
Multiple strategies hit the AsterDex API for OHLCV data, orderbook data, and funding rates independently. With 22 strategies running simultaneously, this could trigger rate limits. No exponential backoff on data fetches (only on git operations). The data cache (`cache.py`) is cleared at the start of each cycle, forcing re-fetches.

**Recommendation:** Add a centralized rate-limited API client with request coalescing. Cache OHLCV data within a cycle instead of clearing everything.

### 5. Leverage Configuration Concerns (Medium Risk)
The "greedy" profile allows 10x leverage on Kalman trend. While backtested, backtests don't capture tail events, exchange outages, or liquidation cascading. The system caps total leverage at 5x in the risk manager, but individual position leverage can exceed this if positions are small relative to portfolio.

### 6. Dashboard Frontend Over-Dependency (Low-Medium)
The React dashboard has 65+ npm dependencies including the full MUI, Radix UI, and multiple animation libraries for what appears to be a monitoring dashboard. This increases build times and attack surface. The package name `@figma/my-make-file` suggests this may have been scaffolded from a template rather than purpose-built.

### 7. Agency Agents Directory (Low)
The `agency-agents/` directory contains 80+ markdown files defining AI agent personas (CEO, CTO, designers, etc.) with integration configs for external tools. These appear disconnected from the trading system itself and add repository bloat.

---

## Summary Scorecard

| Dimension | Grade | Notes |
|---|---|---|
| Strategy Quality | A- | Diverse, backtested, regime-aware, losers pruned |
| Risk Management | A | 14 checks, ATR stops, leverage caps, sector limits |
| Self-Improvement | A- | 5 agents, adaptive thresholds, auto-disable/enable |
| Data Infrastructure | B | Multiple sources, but no rate limiting or coalescing |
| Execution Quality | B | TWAP, smart routing, fill analysis; but silent failures |
| Testing & QA | D | 5 test files for 35K LOC, no CI |
| Database | C+ | SQLite under concurrent load is a ticking time bomb |
| Operational Readiness | B+ | Good deployment, monitoring, notifications |
| Code Quality | B+ | Clean patterns, good separation of concerns |
| **Overall** | **B+** | Production-capable trading system with strong alpha-generation architecture, but infrastructure gaps (testing, database, error handling) create meaningful operational risk |

---

## Priority Actions

1. **Add PostgreSQL** — Replace SQLite before scaling (or before going live)
2. **Add CI + tests** — Minimum: risk manager, strategy signals, execution path
3. **Fix error handling** — Position sync failures must halt trading
4. **Add API rate limiting** — Centralized client with request coalescing
5. **Audit leverage profiles** — Stress-test greedy profile against tail scenarios
