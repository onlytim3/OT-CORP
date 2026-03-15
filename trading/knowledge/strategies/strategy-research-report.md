# Strategy Research Report — 2026-03-14

## Coverage Summary

- **Total strategies cataloged**: 86
- **Implemented**: 23
- **Partially implemented**: 4
- **Not yet implemented**: 57
- **Deleted (negative backtest)**: 2
- **Overall coverage**: 29.1%

---

## Category Coverage

| Category | Implemented | Total | Coverage |
|---|---|---|---|
| Trend Following | 1 | 8 | 12% |
| Mean Reversion | 2 | 5 | 40% |
| Momentum | 2 | 6 | 33% |
| Regime Detection & Switching | 4 | 7 | 57% |
| Statistical Arbitrage | 5 | 8 | 62% |
| Volatility Strategies | 1 | 5 | 20% |
| Factor Investing | 2 | 6 | 33% |
| Carry & Yield | 0 | 4 | 12% |
| Sentiment & Alternative Data | 1 | 7 | 14% |
| Market Microstructure | 4 | 7 | 57% |
| Cross-Asset Signals | 2 | 7 | 36% |
| Machine Learning | 0 | 5 | 0% |
| Timing & Seasonality | 0 | 6 | 0% |
| Portfolio-Level Strategies | 0 | 5 | 0% |

---

## High-Priority Gaps (Priority >= 7)

| Strategy | Category | Priority | Expected Sharpe | Data Needed |
|---|---|---|---|---|
| **Dynamic Regime-Based Allocation** | Portfolio-Level Strategies | 9/10 | 0.7-1.5 | regime model + multi-strategy |
| **Dual Momentum (Antonacci)** | Momentum | 8/10 | 0.6-1.3 | OHLCV multi-asset |
| **Time-Series Momentum (Moskowitz)** | Momentum | 8/10 | 0.5-1.0 | OHLCV 12m history |
| **Correlation Regime Detection** | Regime Detection & Switching | 8/10 | 0.5-1.0 | OHLCV multi-asset |
| **Cash-and-Carry Basis Trade** | Carry & Yield | 8/10 | 1.5-3.8 | spot + quarterly futures |
| **Order Flow Imbalance** | Market Microstructure | 8/10 | 0.5-1.5 | order book depth (AsterDex) |
| **Token Unlock/Vesting Calendar** | Timing & Seasonality | 8/10 | 0.5-1.2 | unlock calendars |
| **Kelly Criterion Sizing** | Portfolio-Level Strategies | 8/10 | N/A (sizing) | trade history |
| **Hurst Exponent Trend Detection** | Trend Following | 7/10 | 0.6-1.3 | OHLCV |
| **Ornstein-Uhlenbeck Mean Reversion** | Mean Reversion | 7/10 | 0.8-1.5 | OHLCV |
| **Structural Break Detection (CUSUM/Bai-Perron)** | Regime Detection & Switching | 7/10 | N/A (filter) | OHLCV |
| **Index Basket Arbitrage** | Statistical Arbitrage | 7/10 | 0.8-1.5 | OHLCV multi-asset |
| **GARCH-Based Position Sizing** | Volatility Strategies | 7/10 | N/A (sizing overlay) | OHLCV |
| **MVRV Z-Score** | Factor Investing | 7/10 | 0.5-1.2 | on-chain (MVRV) |
| **Exchange Inflow/Outflow** | Sentiment & Alternative Data | 7/10 | 0.5-1.2 | on-chain (exchange balances) |
| **Yield Curve → Risk Asset Rotation** | Cross-Asset Signals | 7/10 | 0.3-0.7 | Treasury yields (FRED) |
| **Cross-Market Momentum Spillover** | Cross-Asset Signals | 7/10 | 0.4-0.9 | equity + crypto intraday |
| **Cross-Asset Risk Parity** | Cross-Asset Signals | 7/10 | 0.6-1.2 | multi-asset prices |
| **Meta-Strategy Selector** | Machine Learning | 7/10 | N/A (selector) | strategy performance history |
| **Risk Parity Allocation** | Portfolio-Level Strategies | 7/10 | 0.6-1.2 | covariance matrix |

---

## Categories with Zero Coverage

- **Carry & Yield**: 4 strategies identified, none implemented
- **Machine Learning**: 5 strategies identified, none implemented
- **Timing & Seasonality**: 6 strategies identified, none implemented
- **Portfolio-Level Strategies**: 5 strategies identified, none implemented

---

## Full Implementation Queue (by priority)

| # | Strategy | Category | Priority | Sharpe | Complexity | Decay Risk |
|---|---|---|---|---|---|---|
| 1 | Dynamic Regime-Based Allocation | Portfolio-Level Strategies | 9/10 | 0.7-1.5 | high | low |
| 2 | Dual Momentum (Antonacci) | Momentum | 8/10 | 0.6-1.3 | medium | low |
| 3 | Time-Series Momentum (Moskowitz) | Momentum | 8/10 | 0.5-1.0 | low | low |
| 4 | Correlation Regime Detection | Regime Detection & Switching | 8/10 | 0.5-1.0 | medium | low |
| 5 | Cash-and-Carry Basis Trade | Carry & Yield | 8/10 | 1.5-3.8 | medium | low |
| 6 | Order Flow Imbalance | Market Microstructure | 8/10 | 0.5-1.5 | high | medium |
| 7 | Token Unlock/Vesting Calendar | Timing & Seasonality | 8/10 | 0.5-1.2 | medium | low |
| 8 | Kelly Criterion Sizing | Portfolio-Level Strategies | 8/10 | N/A (sizing) | medium | low |
| 9 | Hurst Exponent Trend Detection | Trend Following | 7/10 | 0.6-1.3 | medium | low |
| 10 | Ornstein-Uhlenbeck Mean Reversion | Mean Reversion | 7/10 | 0.8-1.5 | high | low |
| 11 | Structural Break Detection (CUSUM/Bai-Perron) | Regime Detection & Switching | 7/10 | N/A (filter) | high | low |
| 12 | Index Basket Arbitrage | Statistical Arbitrage | 7/10 | 0.8-1.5 | medium | low |
| 13 | GARCH-Based Position Sizing | Volatility Strategies | 7/10 | N/A (sizing overlay) | medium | low |
| 14 | MVRV Z-Score | Factor Investing | 7/10 | 0.5-1.2 | medium | low |
| 15 | Exchange Inflow/Outflow | Sentiment & Alternative Data | 7/10 | 0.5-1.2 | high | low |
| 16 | Yield Curve → Risk Asset Rotation | Cross-Asset Signals | 7/10 | 0.3-0.7 | medium | low |
| 17 | Cross-Market Momentum Spillover | Cross-Asset Signals | 7/10 | 0.4-0.9 | medium | medium |
| 18 | Cross-Asset Risk Parity | Cross-Asset Signals | 7/10 | 0.6-1.2 | medium | low |
| 19 | Meta-Strategy Selector | Machine Learning | 7/10 | N/A (selector) | very_high | medium |
| 20 | Risk Parity Allocation | Portfolio-Level Strategies | 7/10 | 0.6-1.2 | medium | low |
| 21 | 52-Week High Proximity | Momentum | 6/10 | 0.5-0.9 | low | low |
| 22 | Markov-Switching GARCH | Regime Detection & Switching | 6/10 | 0.8-1.5 | very_high | low |
| 23 | Realized Vol Breakout | Volatility Strategies | 6/10 | 0.4-0.9 | low | low |
| 24 | NVT Ratio Value | Factor Investing | 6/10 | 0.4-1.0 | medium | low |
| 25 | Low Volatility Anomaly | Factor Investing | 6/10 | 0.4-0.8 | low | low |
| 26 | Quality Factor (Dev Activity/TVL) | Factor Investing | 6/10 | 0.5-1.2 | high | low |
| 27 | Whale Wallet Tracking | Sentiment & Alternative Data | 6/10 | 0.4-1.0 | high | medium |
| 28 | Bid-Ask Spread Dynamics | Market Microstructure | 6/10 | 0.3-0.8 | medium | low |
| 29 | XGBoost Signal Ensemble | Machine Learning | 6/10 | 0.5-1.5 | very_high | high |
| 30 | Anomaly Detection (Isolation Forest) | Machine Learning | 6/10 | 0.4-1.0 | high | medium |
| 31 | Intraday Seasonality (Hour-of-Day) | Timing & Seasonality | 6/10 | 0.3-0.7 | low | medium |
| 32 | Hierarchical Risk Parity | Portfolio-Level Strategies | 6/10 | 0.6-1.1 | high | low |
| 33 | Donchian Channel Breakout | Trend Following | 5/10 | 0.4-1.0 | low | low |
| 34 | KAMA Adaptive Trend | Trend Following | 5/10 | 0.5-1.1 | medium | low |
| 35 | Factor Momentum | Momentum | 5/10 | 0.5-1.1 | high | medium |
| 36 | Copula-Based Pairs Trading | Statistical Arbitrage | 5/10 | 0.8-1.8 | very_high | medium |
| 37 | Volatility-of-Volatility Trading | Volatility Strategies | 5/10 | 0.5-1.2 | high | medium |
| 38 | Social Media Sentiment NLP | Sentiment & Alternative Data | 5/10 | 0.3-0.8 | very_high | high |
| 39 | Developer Activity Signal | Sentiment & Alternative Data | 5/10 | 0.3-0.8 | medium | low |
| 40 | VWAP Anchored Trading | Market Microstructure | 5/10 | 0.4-0.9 | low | low |
| 41 | Monthly Seasonality | Timing & Seasonality | 5/10 | 0.2-0.6 | low | medium |
| 42 | Options Expiry Effects | Timing & Seasonality | 5/10 | 0.3-0.8 | high | medium |
| 43 | Bitcoin Halving Cycle | Timing & Seasonality | 5/10 | 0.3-0.8 | low | medium |
| 44 | Black-Litterman with Signal Views | Portfolio-Level Strategies | 5/10 | 0.6-1.3 | very_high | low |
| 45 | Keltner Channel Breakout | Trend Following | 4/10 | 0.5-1.2 | low | low |
| 46 | Triangular Arbitrage | Statistical Arbitrage | 4/10 | 3.0-10.0 | very_high | high |
| 47 | Staking Yield Differential | Carry & Yield | 4/10 | 1.0-2.0 | high | medium |
| 48 | Google Trends Signal | Sentiment & Alternative Data | 4/10 | 0.3-0.7 | medium | medium |
| 49 | Commodity Supercycle Signals | Cross-Asset Signals | 4/10 | 0.3-0.6 | medium | low |
| 50 | LSTM Price Prediction | Machine Learning | 4/10 | 0.3-1.0 | very_high | very_high |
| 51 | Day-of-Week Effect | Timing & Seasonality | 4/10 | 0.2-0.5 | low | medium |
| 52 | Hull Moving Average Trend | Trend Following | 3/10 | 0.5-1.0 | low | low |
| 53 | SuperTrend Indicator | Trend Following | 3/10 | 0.4-0.9 | low | low |
| 54 | Stochastic Oscillator Extremes | Mean Reversion | 3/10 | 0.3-0.7 | low | low |
| 55 | Variance Risk Premium | Volatility Strategies | 3/10 | 0.8-1.5 | high | low |
| 56 | Lending Rate Arbitrage | Carry & Yield | 3/10 | 0.8-2.0 | high | high |
| 57 | RL-Based Execution Optimization | Machine Learning | 3/10 | N/A (execution) | very_high | high |

---

## Recommended Next Actions

### Immediate (this week)
1. **Dynamic Regime-Based Allocation** (Portfolio-Level Strategies) — Priority 9/10, Sharpe 0.7-1.5
1. **Dual Momentum (Antonacci)** (Momentum) — Priority 8/10, Sharpe 0.6-1.3
1. **Time-Series Momentum (Moskowitz)** (Momentum) — Priority 8/10, Sharpe 0.5-1.0
1. **Correlation Regime Detection** (Regime Detection & Switching) — Priority 8/10, Sharpe 0.5-1.0
1. **Cash-and-Carry Basis Trade** (Carry & Yield) — Priority 8/10, Sharpe 1.5-3.8

### Near-term (this month)
1. **Hurst Exponent Trend Detection** (Trend Following) — Priority 7/10
1. **Ornstein-Uhlenbeck Mean Reversion** (Mean Reversion) — Priority 7/10
1. **Structural Break Detection (CUSUM/Bai-Perron)** (Regime Detection & Switching) — Priority 7/10
1. **Index Basket Arbitrage** (Statistical Arbitrage) — Priority 7/10
1. **GARCH-Based Position Sizing** (Volatility Strategies) — Priority 7/10
1. **MVRV Z-Score** (Factor Investing) — Priority 7/10
1. **Exchange Inflow/Outflow** (Sentiment & Alternative Data) — Priority 7/10
1. **Yield Curve → Risk Asset Rotation** (Cross-Asset Signals) — Priority 7/10

### Backlog (when capacity allows)
1. **Donchian Channel Breakout** (Trend Following) — Priority 5/10
1. **KAMA Adaptive Trend** (Trend Following) — Priority 5/10
1. **Factor Momentum** (Momentum) — Priority 5/10
1. **Copula-Based Pairs Trading** (Statistical Arbitrage) — Priority 5/10
1. **Volatility-of-Volatility Trading** (Volatility Strategies) — Priority 5/10
