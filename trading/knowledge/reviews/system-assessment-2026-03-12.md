# Autonomous Trading System — Full Assessment Report
**Date:** 2026-03-12
**System:** Trading Daemon v4.0 | ~$101.7K Paper Portfolio | Alpaca API
**Assessed by:** Portfolio Manager, Quantitative Researcher, Systems Architect

---

## Executive Summary

Three independent assessments converge on the same conclusion: **the system is architecturally sound but not yet production-ready for unattended live capital.** The core trading pipeline (data → signals → aggregation → risk → execution → sync) works correctly. The gaps are operational maturity issues, not design failures.

**Overall Viability: 7.1/10**

| Component | Score | Key Issue |
|-----------|-------|-----------|
| Architecture | 7.5 | Clean layering, some duplication between main.py and scheduler.py |
| Trading Pipeline | 7.0 | ETF signals dropped outside market hours (no queue) |
| Risk Management | 8.0 | 9-layer checks solid, missing leverage awareness + VaR |
| Data Feeds | 7.0 | Good primary feeds, single points of failure on sentiment/macro |
| Execution | 7.5 | Good retry logic, market-only orders, no slippage tracking |
| Monitoring | 6.5 | Dashboard has broken paper-mode import, no health endpoint |
| Strategy Quality | 7.0 | Diverse & well-implemented, heavy BTC bias, no backtesting validation |
| Autonomous Readiness | 6.5 | Good error handling, no watchdog, hardcoded 2026 holidays |
| Code Quality | 7.0 | Clean patterns, zero tests, inconsistent logging |
| Security | 6.5 | Dashboard binds 0.0.0.0 with no auth, error messages leak data |

---

## Critical Issues (Fix Before Live Trading)

### 1. Broken Dashboard in Paper Mode
**File:** `trading/monitor/web.py` line 29
**Issue:** References `trading.execution.paper` which was removed in v4.0. Dashboard crashes on import in paper mode — the primary monitoring tool is broken.
**Fix:** Remove paper mode routing from web.py, always use Alpaca client (same fix applied to scheduler/main/sync).

### 2. ProfitTracker State Lost on Restart
**File:** `trading/risk/profit_manager.py`
**Issue:** High watermarks and trailing stop state live in memory (`_profit_tracker` global). Daemon restart loses all trailing stop levels. A restart during a rally misses profit-taking exits.
**Fix:** Persist watermarks to SQLite table. Load on startup, update after each check.

### 3. No Process Supervisor
**Issue:** Daemon runs as `nohup python3`. Crash = trading stops until manual restart.
**Fix:** systemd service (Linux) or launchd plist (macOS) with auto-restart.

### 4. Hardcoded 2026 Holidays
**File:** `trading/execution/market_hours.py` lines 25-37
**Issue:** `NYSE_HOLIDAYS_2026` set expires Dec 31, 2026. System will trade ETFs on 2027 holidays.
**Fix:** Use `exchange_calendars` library or maintain multi-year dict with year validation.

### 5. Leveraged ETFs Treated as 1x for Risk
**File:** `trading/risk/manager.py`
**Issue:** 33% allocation to UGL (2x gold) = 66% effective gold exposure. Risk limits don't account for leverage.
**Fix:** Multiply position values by leverage factor in risk checks. Reduce max_position_pct to 15% for live.

### 6. No Startup Config Validation
**Issue:** Empty `ALPACA_API_KEY` only fails on first API call, potentially hours into a cycle.
**Fix:** Validate all required keys and test API connectivity at daemon startup.

---

## High Priority Issues (Fix Within First Week)

### 7. New Alpaca Client Per API Call
**File:** `trading/execution/alpaca_client.py`
Creates fresh `TradingClient`/`StockHistoricalDataClient`/`CryptoHistoricalDataClient` on every call (30-50 per cycle). TLS handshake overhead.
**Fix:** Module-level lazy singletons.

### 8. No Order Idempotency
**Issue:** Crash after `submit_order()` but before `insert_trade()` = orphaned order + possible duplicate on restart.
**Fix:** Client-side idempotency keys. On startup, reconcile Alpaca orders against local records.

### 9. ETF Signals Silently Dropped
**File:** `trading/scheduler.py` lines 211-219
**Issue:** "DEFERRED" ETF signals during closed market are logged but never re-queued. Signal generated at 6 PM is lost.
**Fix:** Persist deferred signals to DB. Replay at next market-open cycle.

### 10. Dashboard Binds 0.0.0.0 With No Auth
**File:** `trading/monitor/web.py` line 123
**Issue:** Account balances and trade history exposed to network.
**Fix:** Bind to 127.0.0.1 by default. Add Flask-HTTPAuth if network access needed.

### 11. Risk Manager N+1 Query
**File:** `trading/risk/manager.py`
**Issue:** `get_positions()` called 4 separate times per trade check.
**Fix:** Fetch once, pass to sub-checks.

### 12. FIFO Trade Pairing Bug
**File:** `trading/execution/sync.py` line 298
**Issue:** `pair_trades()` fully closes a buy trade even when sell quantity < buy quantity. P&L accounting corrupted.
**Fix:** Only close the matched portion; leave remainder as open.

### 13. Zero Automated Tests
**Issue:** `trading/tests/__init__.py` is empty. Risk manager, aggregator, and sync logic are untested.
**Fix:** Write unit tests for risk checks, signal aggregation, and trade pairing first.

---

## Research Findings: Best Practices & Roadmap

### Risk Management Upgrades Needed

| Parameter | Current | Live Target | Rationale |
|-----------|---------|-------------|-----------|
| max_position_pct | 33% | 15% | 2x leverage = 30% effective |
| max_crypto_pct | 70% | 50% | Reduce concentration |
| max_correlated_group | 50% | 30% | Correlations spike in stress |
| min_cash_reserve | 10% | 20% | Buffer for margin calls |
| stop_loss_pct | 7% | 5% | Tighter for leveraged products |
| max_daily_loss | 5% | 3% | Preserve capital |
| max_drawdown | 20% | 15% | Earlier kill switch |

**Add:** VaR/CVaR calculation, volatility-targeted position sizing (replace fixed %), multi-level kill switch (pause → close → emergency liquidation), weekly stress testing.

### Execution Upgrades Needed

- **Limit orders with IOC fallback** for ETFs (wider spreads)
- **TWAP execution** for orders > 1% of daily volume
- **WebSocket streaming** for stop-loss monitoring (replace 15-min polling)
- **Slippage tracking** (expected vs actual fill price per trade)

### Backtesting Gaps (Critical for Live)

- **Walk-forward validation** — current engine runs single in-sample pass (overfitting risk)
- **Deflated Sharpe Ratio** — adjusts for multiple testing with 10 strategies
- **Parameter stability testing** — perturb by ±20%, check if performance drops >50%
- **Strategy correlation matrix** — two strategies at r>0.7 should share one budget slot

### Data Infrastructure

- **SQLite is fine for now.** Migrate to PostgreSQL + TimescaleDB only if tick data or multi-process needed
- **Add Redis** for price cache, kill switch flag, and pub/sub to dashboard
- **Add DuckDB** for fast backtesting analytics on Parquet files
- **Missing data sources:** BTC funding rates, exchange inflows, Google Trends, CFTC COT

### Technology Stack Additions

| Need | Tool | Priority |
|------|------|----------|
| Fast backtesting | VectorBT | High |
| Risk analytics | empyrical + pyfolio | High |
| Portfolio optimization | PyPortfolioOpt | Medium |
| Technical indicators | pandas-ta | Low (custom indicators work) |
| GARCH volatility | arch | Medium |
| Error tracking | Sentry | High (for live) |
| Process supervision | systemd/launchd | Critical |
| Deployment | AWS EC2 t3.small us-east-1 | High (for live) |

### Performance Metrics to Add

Currently tracking Sharpe only. Add:
- **Sortino Ratio** (penalizes only downside vol, target >1.5)
- **Calmar Ratio** (annual return / max drawdown, target >1.0)
- **Profit Factor** (gross profit / gross loss, target >1.5)
- **Signal IC** (correlation of signal strength with forward returns, target >0.05)
- **Max Drawdown Duration** (days from trough to new peak, target <60 days)

---

## Paper-to-Live Transition Checklist

### Phase 1: Validation (2-4 weeks)
- [ ] All strategies backtested with walk-forward validation
- [ ] Out-of-sample Sharpe within 50% of in-sample for each strategy
- [ ] 30+ days of paper trading with Sharpe > 0.5
- [ ] Paper drawdown < 15%
- [ ] Risk manager has blocked at least one trade (proves it works)
- [ ] Stop-loss triggered at least once (proves it works)
- [ ] Data sources monitored for reliability (uptime > 99%)

### Phase 2: Infrastructure (1-2 weeks)
- [ ] Cloud VM deployed (AWS EC2 t3.small, us-east-1)
- [ ] systemd service with auto-restart
- [ ] External uptime monitoring
- [ ] All alert channels tested
- [ ] Database backup schedule
- [ ] Log rotation configured
- [ ] Kill switch tested and documented

### Phase 3: Shadow Trading (2-4 weeks)
- [ ] Paper and live running simultaneously, identical config
- [ ] Signal comparison between paper and live every cycle
- [ ] Fill quality verified: actual vs expected
- [ ] Live-paper divergence < 0.5% per cycle
- [ ] All emergency procedures tested with small positions

### Phase 4: Gradual Deployment (4-8 weeks)
- [ ] Week 1-2: $10K with 3 highest-conviction strategies
- [ ] Week 3-4: $25K with 5 strategies
- [ ] Week 5-6: $50K with 8 strategies
- [ ] Week 7-8: $75K with all 10 strategies
- [ ] Each step requires: positive P&L, no unexpected issues, Sharpe > 0.3
- [ ] Always keep 20% cash reserve

---

## Prioritized Implementation Roadmap

### Phase 1: Critical Fixes (Week 1)
1. Fix dashboard paper-mode crash
2. Persist ProfitTracker watermarks to SQLite
3. Add startup config validation
4. Bind dashboard to 127.0.0.1
5. Singleton Alpaca clients
6. Fix FIFO trade pairing remainder bug

### Phase 2: Risk Hardening (Weeks 1-3)
7. Tighten risk parameters for live
8. Implement leverage-aware risk checks
9. Add multi-level kill switch
10. Add data quality checks (price sanity, staleness)
11. Order submission idempotency
12. Write risk manager unit tests

### Phase 3: Backtesting Rigor (Weeks 3-6)
13. Walk-forward validation in backtest engine
14. Out-of-sample testing with holdout period
15. Parameter stability testing
16. Strategy correlation matrix
17. Deflated Sharpe Ratio implementation

### Phase 4: Execution Quality (Weeks 5-8)
18. Limit orders with IOC timeout + market fallback
19. WebSocket streaming for stop-loss checks
20. Slippage tracking per trade
21. Signal queue for deferred ETF trades
22. Dynamic market holidays (exchange_calendars)

### Phase 5: Monitoring & Operations (Weeks 7-10)
23. VaR/CVaR in daily P&L snapshot
24. Sortino, Calmar, profit factor tracking
25. System health check endpoint (/api/health)
26. Dashboard: equity curve, risk metrics, blotter
27. PnL attribution by strategy
28. Process supervisor (systemd/launchd)
29. Database backup automation
30. Structured JSON logging with rotation

### Phase 6: Live Preparation (Weeks 9-12)
31. Cloud VM deployment
32. External monitoring setup
33. CI/CD with GitHub Actions
34. Regime detection layer
35. Alternative data sources (funding rates, exchange flows)

### Phase 7: Shadow + Live (Weeks 12-24)
36. Shadow trading (paper + live parallel)
37. Gradual capital deployment per ramp schedule
38. Weekly stress testing
39. Tax lot tracking setup

---

## Verdict

The system demonstrates strong engineering across the trading domain. Clean architecture, diversified strategies, thoughtful risk layering. The gaps are **operational maturity** — state persistence, process supervision, testing, and execution refinement. All critical issues are fixable in 1-2 weeks without architectural changes.

**Bottom line:** This system is viable for extended paper trading and limited-capital live testing today. With the Phase 1-2 fixes (2-3 weeks of work), it would be ready for the gradual live deployment ramp.
