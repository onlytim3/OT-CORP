"""Strategy Research Engine — discovers, catalogs, and identifies gaps in strategy coverage.

Maintains a structured universe of all known strategy families, maps them against
implemented strategies, and produces gap analysis reports. Designed to be run
periodically (weekly) or on-demand to surface new strategy ideas.

Usage:
    from trading.intelligence.strategy_researcher import run_research_cycle
    report = run_research_cycle()
    print(report.summary())
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from trading.config import KNOWLEDGE_DIR, STRATEGY_ENABLED

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy Universe — exhaustive taxonomy of all known strategy families
# ---------------------------------------------------------------------------

STRATEGY_UNIVERSE = {
    "trend_following": {
        "label": "Trend Following",
        "description": "Exploit directional price persistence using filters and breakouts",
        "strategies": {
            "ema_crossover": {
                "name": "EMA Crossover",
                "description": "Fast/slow exponential moving average crossover",
                "sharpe_range": "0.3-0.8",
                "regime": "trending",
                "data_needed": "OHLCV",
                "complexity": "low",
                "decay_risk": "low",
                "status": "deleted",
                "notes": "Removed — negative backtest returns",
            },
            "kalman_trend": {
                "name": "Kalman Filter Trend",
                "description": "Adaptive trend extraction via Kalman filter state estimation",
                "sharpe_range": "2.0-3.5",
                "regime": "trending",
                "data_needed": "OHLCV",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "implemented",
                "notes": "Best performer — Sharpe 3.49 in 90-day backtest",
            },
            "donchian_breakout": {
                "name": "Donchian Channel Breakout",
                "description": "Buy on N-period high, sell on N-period low",
                "sharpe_range": "0.4-1.0",
                "regime": "trending",
                "data_needed": "OHLCV",
                "complexity": "low",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 5,
            },
            "keltner_channel": {
                "name": "Keltner Channel Breakout",
                "description": "ATR-based channels around EMA — breakout on squeeze release",
                "sharpe_range": "0.5-1.2",
                "regime": "trending",
                "data_needed": "OHLCV",
                "complexity": "low",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 4,
            },
            "hull_ma_trend": {
                "name": "Hull Moving Average Trend",
                "description": "Low-lag MA using weighted moving average of WMAs",
                "sharpe_range": "0.5-1.0",
                "regime": "trending",
                "data_needed": "OHLCV",
                "complexity": "low",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 3,
            },
            "hurst_persistence": {
                "name": "Hurst Exponent Trend Detection",
                "description": "Use Hurst exponent to detect persistent vs mean-reverting regimes, trade only when H > 0.5",
                "sharpe_range": "0.6-1.3",
                "regime": "all",
                "data_needed": "OHLCV",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 7,
            },
            "supertrend": {
                "name": "SuperTrend Indicator",
                "description": "ATR-based trailing stop trend indicator",
                "sharpe_range": "0.4-0.9",
                "regime": "trending",
                "data_needed": "OHLCV",
                "complexity": "low",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 3,
            },
            "adaptive_trend": {
                "name": "KAMA Adaptive Trend",
                "description": "Kaufman Adaptive Moving Average — adapts smoothing to volatility",
                "sharpe_range": "0.5-1.1",
                "regime": "all",
                "data_needed": "OHLCV",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 5,
            },
        },
    },
    "mean_reversion": {
        "label": "Mean Reversion",
        "description": "Exploit price deviations from equilibrium in ranging markets",
        "strategies": {
            "rsi_divergence": {
                "name": "RSI Divergence",
                "description": "Price/RSI divergence detection for reversal signals",
                "sharpe_range": "0.5-1.2",
                "regime": "ranging",
                "data_needed": "OHLCV",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "implemented",
            },
            "regime_mean_reversion": {
                "name": "Regime-Conditional Mean Reversion",
                "description": "Z-score mean reversion only during HMM-detected ranging regimes",
                "sharpe_range": "0.8-1.8",
                "regime": "ranging",
                "data_needed": "OHLCV",
                "complexity": "high",
                "decay_risk": "low",
                "status": "implemented",
            },
            "bollinger_mean_reversion": {
                "name": "Bollinger Band Mean Reversion",
                "description": "Buy at lower band, sell at upper band with volume confirmation",
                "sharpe_range": "0.3-0.8",
                "regime": "ranging",
                "data_needed": "OHLCV",
                "complexity": "low",
                "decay_risk": "low",
                "status": "deleted",
                "notes": "Removed — negative backtest returns",
            },
            "ou_process": {
                "name": "Ornstein-Uhlenbeck Mean Reversion",
                "description": "Fit OU process to estimate mean-reversion speed and half-life, trade when price deviates > 2σ",
                "sharpe_range": "0.8-1.5",
                "regime": "ranging",
                "data_needed": "OHLCV",
                "complexity": "high",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 7,
            },
            "stochastic_extreme": {
                "name": "Stochastic Oscillator Extremes",
                "description": "Trade reversals at stochastic K/D extreme readings with momentum confirmation",
                "sharpe_range": "0.3-0.7",
                "regime": "ranging",
                "data_needed": "OHLCV",
                "complexity": "low",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 3,
            },
        },
    },
    "momentum": {
        "label": "Momentum",
        "description": "Exploit persistence in returns — winners keep winning, losers keep losing",
        "strategies": {
            "cross_asset_momentum": {
                "name": "Cross-Asset Momentum",
                "description": "Rank assets by recent returns, go long top performers, short worst",
                "sharpe_range": "0.5-1.2",
                "regime": "trending",
                "data_needed": "OHLCV multi-asset",
                "complexity": "medium",
                "decay_risk": "medium",
                "status": "implemented",
            },
            "meme_momentum": {
                "name": "Meme Coin Momentum",
                "description": "Volume-confirmed momentum for high-vol meme tokens",
                "sharpe_range": "0.3-2.0",
                "regime": "trending",
                "data_needed": "OHLCV + volume",
                "complexity": "medium",
                "decay_risk": "high",
                "status": "implemented",
            },
            "dual_momentum": {
                "name": "Dual Momentum (Antonacci)",
                "description": "Combine absolute momentum (is asset trending up?) with relative momentum (which asset trends most?)",
                "sharpe_range": "0.6-1.3",
                "regime": "trending",
                "data_needed": "OHLCV multi-asset",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 8,
            },
            "time_series_momentum": {
                "name": "Time-Series Momentum (Moskowitz)",
                "description": "Each asset's own past 12-month return predicts next month — go long positive, short negative",
                "sharpe_range": "0.5-1.0",
                "regime": "trending",
                "data_needed": "OHLCV 12m history",
                "complexity": "low",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 8,
            },
            "factor_momentum": {
                "name": "Factor Momentum",
                "description": "Momentum applied to factor returns rather than individual assets",
                "sharpe_range": "0.5-1.1",
                "regime": "trending",
                "data_needed": "factor scores",
                "complexity": "high",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 5,
            },
            "52w_high_momentum": {
                "name": "52-Week High Proximity",
                "description": "Assets near their 52-week high continue outperforming (George & Hwang 2004)",
                "sharpe_range": "0.5-0.9",
                "regime": "trending",
                "data_needed": "OHLCV 1y",
                "complexity": "low",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 6,
            },
        },
    },
    "regime_detection": {
        "label": "Regime Detection & Switching",
        "description": "Identify market state (bull/bear/ranging/volatile) to condition other strategies",
        "strategies": {
            "hmm_regime": {
                "name": "Hidden Markov Model Regime",
                "description": "2-4 state HMM on returns to classify bull/bear/ranging/crisis",
                "sharpe_range": "0.8-1.8",
                "regime": "all",
                "data_needed": "OHLCV",
                "complexity": "high",
                "decay_risk": "low",
                "status": "implemented",
            },
            "garch_volatility": {
                "name": "GARCH Volatility Regime",
                "description": "GARCH(1,1) for vol forecasting, classify high/low vol regimes",
                "sharpe_range": "0.3-0.8",
                "regime": "all",
                "data_needed": "OHLCV",
                "complexity": "high",
                "decay_risk": "low",
                "status": "implemented",
                "notes": "Disabled — vol regimes don't predict direction alone",
            },
            "volatility_regime": {
                "name": "Volatility Regime Classifier",
                "description": "Vol compression/expansion detection for regime-aware entry/exit",
                "sharpe_range": "0.5-1.2",
                "regime": "all",
                "data_needed": "OHLCV",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "implemented",
            },
            "markov_switching_garch": {
                "name": "Markov-Switching GARCH",
                "description": "Combine HMM with GARCH — regime-dependent volatility dynamics",
                "sharpe_range": "0.8-1.5",
                "regime": "all",
                "data_needed": "OHLCV",
                "complexity": "very_high",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 6,
            },
            "correlation_regime": {
                "name": "Correlation Regime Detection",
                "description": "Track rolling correlations between assets; regime change when correlation structure breaks",
                "sharpe_range": "0.5-1.0",
                "regime": "all",
                "data_needed": "OHLCV multi-asset",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 8,
            },
            "structural_break": {
                "name": "Structural Break Detection (CUSUM/Bai-Perron)",
                "description": "Statistical tests for parameter instability — detect when market dynamics change",
                "sharpe_range": "N/A (filter)",
                "regime": "all",
                "data_needed": "OHLCV",
                "complexity": "high",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 7,
            },
            "dxy_dollar_regime": {
                "name": "Dollar Strength Regime",
                "description": "DXY regime as filter for crypto/commodity direction",
                "sharpe_range": "0.3-0.7",
                "regime": "all",
                "data_needed": "DXY index, macro",
                "complexity": "low",
                "decay_risk": "low",
                "status": "implemented",
                "notes": "Disabled — requires Alpaca ETF access",
            },
        },
    },
    "stat_arb": {
        "label": "Statistical Arbitrage",
        "description": "Exploit pricing inefficiencies between related instruments",
        "strategies": {
            "pairs_trading": {
                "name": "Cointegration Pairs Trading",
                "description": "Find cointegrated pairs, trade spread deviations",
                "sharpe_range": "0.8-2.0",
                "regime": "ranging",
                "data_needed": "OHLCV multi-asset",
                "complexity": "high",
                "decay_risk": "medium",
                "status": "implemented",
            },
            "funding_arb": {
                "name": "Funding Rate Arbitrage",
                "description": "Exploit extreme perps funding rates via mean reversion",
                "sharpe_range": "1.5-3.0",
                "regime": "all",
                "data_needed": "funding rates (AsterDex)",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "implemented",
            },
            "basis_zscore": {
                "name": "Basis Z-Score Trading",
                "description": "Trade spot-futures basis when z-score is extreme",
                "sharpe_range": "1.0-2.5",
                "regime": "all",
                "data_needed": "spot + futures prices",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "implemented",
            },
            "cross_basis_rv": {
                "name": "Cross-Pair Basis Relative Value",
                "description": "Compare basis across pairs, trade rich vs cheap",
                "sharpe_range": "0.5-1.5",
                "regime": "all",
                "data_needed": "multi-pair basis",
                "complexity": "high",
                "decay_risk": "low",
                "status": "implemented",
                "notes": "NEVER leverage — liquidation-prone in backtests",
            },
            "funding_term_structure": {
                "name": "Funding Rate Term Structure",
                "description": "Compare current funding vs 7d/30d avg — trade term structure slope",
                "sharpe_range": "1.0-2.0",
                "regime": "all",
                "data_needed": "funding rate history",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "implemented",
            },
            "triangular_arb": {
                "name": "Triangular Arbitrage",
                "description": "Exploit price discrepancies across 3 trading pairs (A/B × B/C ≠ A/C)",
                "sharpe_range": "3.0-10.0",
                "regime": "all",
                "data_needed": "real-time order books",
                "complexity": "very_high",
                "decay_risk": "high",
                "status": "not_implemented",
                "priority": 4,
                "notes": "Requires sub-second execution — may not be viable with 4h cycle",
            },
            "copula_pairs": {
                "name": "Copula-Based Pairs Trading",
                "description": "Use copulas instead of linear cointegration for nonlinear dependency modeling",
                "sharpe_range": "0.8-1.8",
                "regime": "ranging",
                "data_needed": "OHLCV multi-asset",
                "complexity": "very_high",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 5,
            },
            "index_arb": {
                "name": "Index Basket Arbitrage",
                "description": "Trade deviations between crypto index (top-10) and individual components",
                "sharpe_range": "0.8-1.5",
                "regime": "all",
                "data_needed": "OHLCV multi-asset",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 7,
            },
        },
    },
    "volatility": {
        "label": "Volatility Strategies",
        "description": "Trade volatility itself — compression/expansion, forecasting, premium harvesting",
        "strategies": {
            "breakout_detection": {
                "name": "Volatility Breakout",
                "description": "Detect vol compression, enter on expansion breakout",
                "sharpe_range": "0.3-0.9",
                "regime": "transitioning",
                "data_needed": "OHLCV",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "implemented",
                "notes": "Disabled — too many false breakouts",
            },
            "garch_sizing": {
                "name": "GARCH-Based Position Sizing",
                "description": "Use GARCH vol forecast to scale position size inversely to expected vol",
                "sharpe_range": "N/A (sizing overlay)",
                "regime": "all",
                "data_needed": "OHLCV",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 7,
                "notes": "Overlay, not standalone — apply to existing strategies",
            },
            "variance_risk_premium": {
                "name": "Variance Risk Premium",
                "description": "Implied vol typically exceeds realized vol — harvest the premium",
                "sharpe_range": "0.8-1.5",
                "regime": "all",
                "data_needed": "options data (implied vol)",
                "complexity": "high",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 3,
                "notes": "Requires options market data — limited availability in crypto",
            },
            "vol_of_vol": {
                "name": "Volatility-of-Volatility Trading",
                "description": "Trade when vol itself becomes volatile (VVIX equivalent for crypto)",
                "sharpe_range": "0.5-1.2",
                "regime": "crisis/transition",
                "data_needed": "OHLCV high frequency",
                "complexity": "high",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 5,
            },
            "realized_vol_breakout": {
                "name": "Realized Vol Breakout",
                "description": "When realized vol breaks above its own Bollinger Band, expect regime change",
                "sharpe_range": "0.4-0.9",
                "regime": "transitioning",
                "data_needed": "OHLCV",
                "complexity": "low",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 6,
            },
        },
    },
    "factor_investing": {
        "label": "Factor Investing",
        "description": "Systematic allocation based on fundamental and quantitative factors",
        "strategies": {
            "factor_crypto": {
                "name": "Multi-Factor Crypto Ranking",
                "description": "Rank coins by momentum, volatility, volume factors — long top, short bottom",
                "sharpe_range": "0.5-1.5",
                "regime": "all",
                "data_needed": "OHLCV multi-asset",
                "complexity": "high",
                "decay_risk": "medium",
                "status": "implemented",
            },
            "multi_factor_rank": {
                "name": "Cross-Sectional Factor Ranking",
                "description": "Multi-factor scoring with momentum, value, quality, size",
                "sharpe_range": "0.6-1.5",
                "regime": "all",
                "data_needed": "OHLCV + fundamentals",
                "complexity": "high",
                "decay_risk": "medium",
                "status": "implemented",
            },
            "nvt_value": {
                "name": "NVT Ratio Value",
                "description": "Network Value to Transactions ratio — crypto's P/E equivalent",
                "sharpe_range": "0.4-1.0",
                "regime": "all",
                "data_needed": "on-chain (NVT)",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 6,
            },
            "mvrv_value": {
                "name": "MVRV Z-Score",
                "description": "Market Value to Realized Value — identify over/undervaluation",
                "sharpe_range": "0.5-1.2",
                "regime": "all",
                "data_needed": "on-chain (MVRV)",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 7,
            },
            "low_vol_anomaly": {
                "name": "Low Volatility Anomaly",
                "description": "Low-vol assets outperform high-vol on risk-adjusted basis",
                "sharpe_range": "0.4-0.8",
                "regime": "all",
                "data_needed": "OHLCV multi-asset",
                "complexity": "low",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 6,
            },
            "quality_factor": {
                "name": "Quality Factor (Dev Activity/TVL)",
                "description": "Rank by developer commits, TVL growth, revenue — long quality, short junk",
                "sharpe_range": "0.5-1.2",
                "regime": "all",
                "data_needed": "on-chain + GitHub",
                "complexity": "high",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 6,
            },
        },
    },
    "carry_yield": {
        "label": "Carry & Yield",
        "description": "Capture yield differentials and carry across instruments",
        "strategies": {
            "funding_carry": {
                "name": "Funding Rate Carry",
                "description": "Harvest funding payments from perps — go long negative funding, short positive",
                "sharpe_range": "1.5-3.5",
                "regime": "all",
                "data_needed": "funding rates",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "partially_implemented",
                "notes": "Covered by funding_arb but not pure carry (includes mean reversion)",
            },
            "basis_carry": {
                "name": "Cash-and-Carry Basis Trade",
                "description": "Long spot, short quarterly futures at premium — collect basis convergence",
                "sharpe_range": "1.5-3.8",
                "regime": "contango",
                "data_needed": "spot + quarterly futures",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 8,
                "notes": "Highly profitable in bull markets — AsterDex may have quarterly contracts",
            },
            "staking_yield_diff": {
                "name": "Staking Yield Differential",
                "description": "Arbitrage staking yield differences across protocols/chains",
                "sharpe_range": "1.0-2.0",
                "regime": "all",
                "data_needed": "staking rates (on-chain)",
                "complexity": "high",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 4,
                "notes": "Requires DeFi protocol integration",
            },
            "lending_rate_arb": {
                "name": "Lending Rate Arbitrage",
                "description": "Borrow on low-rate platform, lend on high-rate platform",
                "sharpe_range": "0.8-2.0",
                "regime": "all",
                "data_needed": "lending rates (DeFi)",
                "complexity": "high",
                "decay_risk": "high",
                "status": "not_implemented",
                "priority": 3,
                "notes": "Smart contract risk; requires DeFi integration",
            },
        },
    },
    "sentiment_altdata": {
        "label": "Sentiment & Alternative Data",
        "description": "Alpha from non-price data — social, on-chain, news, search trends",
        "strategies": {
            "fear_greed_contrarian": {
                "name": "Fear & Greed Contrarian",
                "description": "Buy extreme fear, sell extreme greed — sentiment mean reversion",
                "sharpe_range": "0.5-1.0",
                "regime": "all",
                "data_needed": "Fear & Greed Index",
                "complexity": "low",
                "decay_risk": "low",
                "status": "partially_implemented",
                "notes": "Used as input to intelligence engine, not standalone strategy",
            },
            "social_sentiment_nlp": {
                "name": "Social Media Sentiment NLP",
                "description": "Aggregate Reddit/Twitter sentiment via NLP — trade extreme readings",
                "sharpe_range": "0.3-0.8",
                "regime": "all",
                "data_needed": "social media APIs",
                "complexity": "very_high",
                "decay_risk": "high",
                "status": "not_implemented",
                "priority": 5,
            },
            "google_trends": {
                "name": "Google Trends Signal",
                "description": "Search volume spikes predict retail interest — contrarian or momentum",
                "sharpe_range": "0.3-0.7",
                "regime": "all",
                "data_needed": "Google Trends API",
                "complexity": "medium",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 4,
            },
            "exchange_flow": {
                "name": "Exchange Inflow/Outflow",
                "description": "Large exchange inflows = selling pressure; outflows = accumulation",
                "sharpe_range": "0.5-1.2",
                "regime": "all",
                "data_needed": "on-chain (exchange balances)",
                "complexity": "high",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 7,
            },
            "whale_wallet_tracking": {
                "name": "Whale Wallet Tracking",
                "description": "Follow large wallets' accumulation/distribution patterns",
                "sharpe_range": "0.4-1.0",
                "regime": "all",
                "data_needed": "on-chain (wallet analysis)",
                "complexity": "high",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 6,
            },
            "developer_activity": {
                "name": "Developer Activity Signal",
                "description": "GitHub commits/contributors as quality signal — rising activity = bullish",
                "sharpe_range": "0.3-0.8",
                "regime": "all",
                "data_needed": "GitHub API",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 5,
            },
            "news_sentiment": {
                "name": "News Headline Sentiment",
                "description": "NLP scoring of crypto/commodity news headlines — trade extreme readings",
                "sharpe_range": "0.3-0.7",
                "regime": "all",
                "data_needed": "news RSS feeds",
                "complexity": "medium",
                "decay_risk": "medium",
                "status": "partially_implemented",
                "notes": "Intelligence engine scores headlines, but no standalone strategy",
            },
        },
    },
    "microstructure": {
        "label": "Market Microstructure",
        "description": "Alpha from order flow, liquidity dynamics, and market structure",
        "strategies": {
            "taker_divergence": {
                "name": "Taker Buy/Sell Divergence",
                "description": "Trade when taker buy/sell ratio diverges from price direction",
                "sharpe_range": "0.3-0.8",
                "regime": "all",
                "data_needed": "taker volume (AsterDex)",
                "complexity": "medium",
                "decay_risk": "medium",
                "status": "implemented",
                "notes": "NEVER leverage — loses 93% at 3x",
            },
            "microstructure_composite": {
                "name": "Microstructure Composite",
                "description": "Composite score from taker flow, orderbook imbalance, and basis-funding divergence",
                "sharpe_range": "0.8-2.0",
                "regime": "volatile",
                "data_needed": "liquidation feed (AsterDex)",
                "complexity": "high",
                "decay_risk": "low",
                "status": "implemented",
            },
            "oi_price_divergence": {
                "name": "Open Interest / Price Divergence",
                "description": "Rising OI + falling price = short buildup; trade the resolution",
                "sharpe_range": "0.5-1.3",
                "regime": "all",
                "data_needed": "OI data (AsterDex)",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "implemented",
            },
            "whale_flow": {
                "name": "Whale Flow Tracking",
                "description": "Detect large order flow from whale accounts, follow the flow",
                "sharpe_range": "0.3-0.8",
                "regime": "all",
                "data_needed": "order flow (AsterDex)",
                "complexity": "high",
                "decay_risk": "medium",
                "status": "implemented",
            },
            "order_flow_imbalance": {
                "name": "Order Flow Imbalance",
                "description": "Measure bid/ask volume imbalance in order book — predict short-term direction",
                "sharpe_range": "0.5-1.5",
                "regime": "all",
                "data_needed": "order book depth (AsterDex)",
                "complexity": "high",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 8,
            },
            "spread_dynamics": {
                "name": "Bid-Ask Spread Dynamics",
                "description": "Widening spreads predict vol spikes; narrowing spreads signal opportunity",
                "sharpe_range": "0.3-0.8",
                "regime": "all",
                "data_needed": "order book (AsterDex)",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 6,
            },
            "vwap_anchored": {
                "name": "VWAP Anchored Trading",
                "description": "Trade deviations from anchored VWAP as support/resistance",
                "sharpe_range": "0.4-0.9",
                "regime": "all",
                "data_needed": "OHLCV + volume",
                "complexity": "low",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 5,
            },
        },
    },
    "cross_asset": {
        "label": "Cross-Asset Signals",
        "description": "Alpha from relationships between different asset classes",
        "strategies": {
            "gold_crypto_hedge": {
                "name": "Gold/BTC Ratio Mean Reversion",
                "description": "Trade gold/BTC ratio extremes — digital gold thesis",
                "sharpe_range": "0.4-1.0",
                "regime": "all",
                "data_needed": "gold + BTC prices",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "implemented",
            },
            "equity_crypto_correlation": {
                "name": "Equity/Crypto Correlation Regime",
                "description": "Trade crypto based on equity market correlation regime shifts",
                "sharpe_range": "0.3-0.8",
                "regime": "all",
                "data_needed": "equity indices + crypto",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "implemented",
            },
            "yield_curve_rotation": {
                "name": "Yield Curve → Risk Asset Rotation",
                "description": "Steepening yield curve → risk-on (long crypto); flattening → risk-off",
                "sharpe_range": "0.3-0.7",
                "regime": "all",
                "data_needed": "Treasury yields (FRED)",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 7,
            },
            "commodity_supercycle": {
                "name": "Commodity Supercycle Signals",
                "description": "Long-term commodity cycle detection — 15-20yr cycles in real commodity prices",
                "sharpe_range": "0.3-0.6",
                "regime": "long-term",
                "data_needed": "commodity prices, CPI",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 4,
            },
            "dxy_crypto_inverse": {
                "name": "Dollar/Crypto Inverse Correlation",
                "description": "Strong inverse DXY/BTC correlation — trade crypto based on dollar regime",
                "sharpe_range": "0.3-0.7",
                "regime": "all",
                "data_needed": "DXY + crypto",
                "complexity": "low",
                "decay_risk": "low",
                "status": "partially_implemented",
                "notes": "dxy_dollar strategy exists but is disabled",
            },
            "cross_market_spillover": {
                "name": "Cross-Market Momentum Spillover",
                "description": "Equity momentum spills over to crypto with 1-4 hour lag",
                "sharpe_range": "0.4-0.9",
                "regime": "correlated",
                "data_needed": "equity + crypto intraday",
                "complexity": "medium",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 7,
            },
            "risk_parity_cross_asset": {
                "name": "Cross-Asset Risk Parity",
                "description": "Allocate across crypto, commodities, equities based on inverse vol weighting",
                "sharpe_range": "0.6-1.2",
                "regime": "all",
                "data_needed": "multi-asset prices",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 7,
            },
        },
    },
    "machine_learning": {
        "label": "Machine Learning",
        "description": "ML/AI-driven signal generation and strategy selection",
        "strategies": {
            "xgboost_signal": {
                "name": "XGBoost Signal Ensemble",
                "description": "Gradient-boosted trees combining technical + on-chain + sentiment features",
                "sharpe_range": "0.5-1.5",
                "regime": "all",
                "data_needed": "multi-source features",
                "complexity": "very_high",
                "decay_risk": "high",
                "status": "not_implemented",
                "priority": 6,
            },
            "lstm_price_prediction": {
                "name": "LSTM Price Prediction",
                "description": "Sequence model for next-period return prediction",
                "sharpe_range": "0.3-1.0",
                "regime": "all",
                "data_needed": "OHLCV sequence",
                "complexity": "very_high",
                "decay_risk": "very_high",
                "status": "not_implemented",
                "priority": 4,
            },
            "rl_execution": {
                "name": "RL-Based Execution Optimization",
                "description": "Reinforcement learning for optimal order placement and timing",
                "sharpe_range": "N/A (execution)",
                "regime": "all",
                "data_needed": "order book + fills",
                "complexity": "very_high",
                "decay_risk": "high",
                "status": "not_implemented",
                "priority": 3,
            },
            "anomaly_detection": {
                "name": "Anomaly Detection (Isolation Forest)",
                "description": "Detect unusual market conditions that precede large moves",
                "sharpe_range": "0.4-1.0",
                "regime": "all",
                "data_needed": "multi-feature matrix",
                "complexity": "high",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 6,
            },
            "meta_learner": {
                "name": "Meta-Strategy Selector",
                "description": "ML model that selects which strategy to deploy based on market conditions",
                "sharpe_range": "N/A (selector)",
                "regime": "all",
                "data_needed": "strategy performance history",
                "complexity": "very_high",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 7,
                "notes": "Requires sufficient trade history from multiple strategies",
            },
        },
    },
    "timing_seasonality": {
        "label": "Timing & Seasonality",
        "description": "Exploit recurring temporal patterns in returns",
        "strategies": {
            "intraday_seasonality": {
                "name": "Intraday Seasonality (Hour-of-Day)",
                "description": "Crypto shows predictable intraday patterns — Asian/European/US session effects",
                "sharpe_range": "0.3-0.7",
                "regime": "all",
                "data_needed": "OHLCV hourly",
                "complexity": "low",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 6,
            },
            "day_of_week": {
                "name": "Day-of-Week Effect",
                "description": "Monday dip / weekend premium patterns in crypto",
                "sharpe_range": "0.2-0.5",
                "regime": "all",
                "data_needed": "OHLCV daily",
                "complexity": "low",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 4,
            },
            "monthly_seasonality": {
                "name": "Monthly Seasonality",
                "description": "January effect, month-end rebalancing flows, halving cycle months",
                "sharpe_range": "0.2-0.6",
                "regime": "all",
                "data_needed": "OHLCV daily",
                "complexity": "low",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 5,
            },
            "options_expiry": {
                "name": "Options Expiry Effects",
                "description": "Max pain pinning, gamma squeeze potential around large options expiries",
                "sharpe_range": "0.3-0.8",
                "regime": "all",
                "data_needed": "options OI, expiry calendar",
                "complexity": "high",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 5,
            },
            "halving_cycle": {
                "name": "Bitcoin Halving Cycle",
                "description": "4-year halving cycle positioning — historical pattern of pre/post-halving rallies",
                "sharpe_range": "0.3-0.8",
                "regime": "long-term",
                "data_needed": "BTC price + halving dates",
                "complexity": "low",
                "decay_risk": "medium",
                "status": "not_implemented",
                "priority": 5,
            },
            "token_unlock_calendar": {
                "name": "Token Unlock/Vesting Calendar",
                "description": "Short tokens approaching large unlock events — sell pressure from vesting",
                "sharpe_range": "0.5-1.2",
                "regime": "all",
                "data_needed": "unlock calendars",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 8,
                "notes": "High alpha — unlock events create predictable selling pressure",
            },
        },
    },
    "portfolio_level": {
        "label": "Portfolio-Level Strategies",
        "description": "Strategies that operate on portfolio construction rather than individual signals",
        "strategies": {
            "risk_parity": {
                "name": "Risk Parity Allocation",
                "description": "Equal risk contribution from each asset — inverse vol weighting with correlations",
                "sharpe_range": "0.6-1.2",
                "regime": "all",
                "data_needed": "covariance matrix",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 7,
            },
            "black_litterman": {
                "name": "Black-Litterman with Signal Views",
                "description": "Combine market equilibrium with strategy signals as views for optimal allocation",
                "sharpe_range": "0.6-1.3",
                "regime": "all",
                "data_needed": "covariance + signal views",
                "complexity": "very_high",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 5,
            },
            "hrp": {
                "name": "Hierarchical Risk Parity",
                "description": "Cluster assets by correlation, allocate using hierarchical tree structure",
                "sharpe_range": "0.6-1.1",
                "regime": "all",
                "data_needed": "covariance matrix",
                "complexity": "high",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 6,
            },
            "kelly_sizing": {
                "name": "Kelly Criterion Sizing",
                "description": "Optimal fraction of capital to risk per trade based on win rate and payoff ratio",
                "sharpe_range": "N/A (sizing)",
                "regime": "all",
                "data_needed": "trade history",
                "complexity": "medium",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 8,
                "notes": "Critical for capital efficiency — should be implemented as overlay",
            },
            "dynamic_regime_allocation": {
                "name": "Dynamic Regime-Based Allocation",
                "description": "Shift portfolio weights based on detected market regime (bull/bear/crisis)",
                "sharpe_range": "0.7-1.5",
                "regime": "all",
                "data_needed": "regime model + multi-strategy",
                "complexity": "high",
                "decay_risk": "low",
                "status": "not_implemented",
                "priority": 9,
                "notes": "Highest-priority portfolio strategy — ties regime detection to allocation",
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

@dataclass
class StrategyInfo:
    """Summary of a single strategy in the universe."""
    name: str
    category: str
    status: str
    priority: int = 0
    sharpe_range: str = ""
    complexity: str = ""
    decay_risk: str = ""
    data_needed: str = ""
    notes: str = ""


@dataclass
class GapAnalysis:
    """Complete gap analysis of the strategy universe."""
    timestamp: str
    total_strategies: int = 0
    implemented: int = 0
    partially_implemented: int = 0
    not_implemented: int = 0
    deleted: int = 0
    disabled: int = 0
    coverage_pct: float = 0.0
    categories: dict = field(default_factory=dict)
    high_priority_gaps: list = field(default_factory=list)
    category_gaps: list = field(default_factory=list)
    implementation_queue: list = field(default_factory=list)

    def summary(self) -> str:
        """One-line summary."""
        return (
            f"Strategy Universe: {self.implemented}/{self.total_strategies} implemented "
            f"({self.coverage_pct:.0f}%), {len(self.high_priority_gaps)} high-priority gaps, "
            f"{len(self.category_gaps)} category-level gaps"
        )

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "total_strategies": self.total_strategies,
            "implemented": self.implemented,
            "partially_implemented": self.partially_implemented,
            "not_implemented": self.not_implemented,
            "deleted": self.deleted,
            "coverage_pct": self.coverage_pct,
            "categories": self.categories,
            "high_priority_gaps": [
                {"name": s.name, "category": s.category, "priority": s.priority,
                 "sharpe": s.sharpe_range, "data": s.data_needed}
                for s in self.high_priority_gaps
            ],
            "implementation_queue": [
                {"name": s.name, "category": s.category, "priority": s.priority}
                for s in self.implementation_queue
            ],
        }


def analyze_gaps() -> GapAnalysis:
    """Analyze the full strategy universe and identify gaps.

    Returns a GapAnalysis with:
    - Coverage statistics
    - Per-category breakdown
    - High-priority unimplemented strategies
    - Suggested implementation queue (sorted by priority)
    """
    analysis = GapAnalysis(
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    all_strategies: list[StrategyInfo] = []
    unimplemented: list[StrategyInfo] = []

    for cat_key, category in STRATEGY_UNIVERSE.items():
        cat_implemented = 0
        cat_total = 0
        cat_strategies = []

        for strat_key, strat in category["strategies"].items():
            status = strat.get("status", "not_implemented")
            info = StrategyInfo(
                name=strat["name"],
                category=category["label"],
                status=status,
                priority=strat.get("priority", 0),
                sharpe_range=strat.get("sharpe_range", ""),
                complexity=strat.get("complexity", ""),
                decay_risk=strat.get("decay_risk", ""),
                data_needed=strat.get("data_needed", ""),
                notes=strat.get("notes", ""),
            )
            all_strategies.append(info)
            cat_strategies.append(info)
            cat_total += 1

            if status == "implemented":
                analysis.implemented += 1
                cat_implemented += 1
            elif status == "partially_implemented":
                analysis.partially_implemented += 1
                cat_implemented += 0.5
            elif status == "deleted":
                analysis.deleted += 1
            elif status == "not_implemented":
                analysis.not_implemented += 1
                unimplemented.append(info)

        # Category-level stats
        cat_coverage = (cat_implemented / cat_total * 100) if cat_total > 0 else 0
        analysis.categories[cat_key] = {
            "label": category["label"],
            "total": cat_total,
            "implemented": int(cat_implemented),
            "coverage_pct": round(cat_coverage, 1),
            "strategies": [
                {"name": s.name, "status": s.status, "priority": s.priority}
                for s in cat_strategies
            ],
        }

        # Flag categories with zero or very low coverage
        if cat_implemented < 1:
            analysis.category_gaps.append({
                "category": category["label"],
                "coverage": f"{cat_coverage:.0f}%",
                "total_strategies": cat_total,
            })

    analysis.total_strategies = len(all_strategies)
    analysis.coverage_pct = (
        (analysis.implemented + analysis.partially_implemented * 0.5)
        / analysis.total_strategies * 100
    ) if analysis.total_strategies > 0 else 0

    # High-priority gaps (priority >= 7)
    analysis.high_priority_gaps = sorted(
        [s for s in unimplemented if s.priority >= 7],
        key=lambda s: -s.priority,
    )

    # Full implementation queue (sorted by priority desc)
    analysis.implementation_queue = sorted(
        unimplemented,
        key=lambda s: -s.priority,
    )

    return analysis


def get_strategy_universe_summary() -> dict:
    """Get a compact summary of the strategy universe for display."""
    analysis = analyze_gaps()
    return {
        "summary": analysis.summary(),
        "coverage": f"{analysis.coverage_pct:.0f}%",
        "implemented": analysis.implemented,
        "total": analysis.total_strategies,
        "high_priority_gaps": len(analysis.high_priority_gaps),
        "top_5_priorities": [
            {"name": s.name, "category": s.category, "priority": s.priority,
             "sharpe": s.sharpe_range}
            for s in analysis.implementation_queue[:5]
        ],
        "category_gaps": analysis.category_gaps,
    }


def generate_research_report() -> str:
    """Generate a full-text research report on strategy coverage and gaps.

    Returns markdown-formatted report suitable for saving to knowledge dir.
    """
    analysis = analyze_gaps()

    lines = [
        f"# Strategy Research Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        "## Coverage Summary",
        "",
        f"- **Total strategies cataloged**: {analysis.total_strategies}",
        f"- **Implemented**: {analysis.implemented}",
        f"- **Partially implemented**: {analysis.partially_implemented}",
        f"- **Not yet implemented**: {analysis.not_implemented}",
        f"- **Deleted (negative backtest)**: {analysis.deleted}",
        f"- **Overall coverage**: {analysis.coverage_pct:.1f}%",
        "",
        "---",
        "",
        "## Category Coverage",
        "",
        "| Category | Implemented | Total | Coverage |",
        "|---|---|---|---|",
    ]

    for cat_key, cat_data in analysis.categories.items():
        lines.append(
            f"| {cat_data['label']} | {cat_data['implemented']} | "
            f"{cat_data['total']} | {cat_data['coverage_pct']:.0f}% |"
        )

    lines += [
        "",
        "---",
        "",
        "## High-Priority Gaps (Priority >= 7)",
        "",
        "| Strategy | Category | Priority | Expected Sharpe | Data Needed |",
        "|---|---|---|---|---|",
    ]

    for s in analysis.high_priority_gaps:
        lines.append(
            f"| **{s.name}** | {s.category} | {s.priority}/10 | "
            f"{s.sharpe_range} | {s.data_needed} |"
        )

    lines += [
        "",
        "---",
        "",
        "## Categories with Zero Coverage",
        "",
    ]

    if analysis.category_gaps:
        for gap in analysis.category_gaps:
            lines.append(
                f"- **{gap['category']}**: {gap['total_strategies']} strategies identified, "
                f"none implemented"
            )
    else:
        lines.append("All categories have at least one implementation.")

    lines += [
        "",
        "---",
        "",
        "## Full Implementation Queue (by priority)",
        "",
        "| # | Strategy | Category | Priority | Sharpe | Complexity | Decay Risk |",
        "|---|---|---|---|---|---|---|",
    ]

    for i, s in enumerate(analysis.implementation_queue, 1):
        lines.append(
            f"| {i} | {s.name} | {s.category} | {s.priority}/10 | "
            f"{s.sharpe_range} | {s.complexity} | {s.decay_risk} |"
        )

    lines += [
        "",
        "---",
        "",
        "## Recommended Next Actions",
        "",
        "### Immediate (this week)",
    ]

    immediate = [s for s in analysis.implementation_queue if s.priority >= 8]
    for s in immediate[:5]:
        lines.append(f"1. **{s.name}** ({s.category}) — Priority {s.priority}/10, "
                     f"Sharpe {s.sharpe_range}")

    lines += [
        "",
        "### Near-term (this month)",
    ]

    nearterm = [s for s in analysis.implementation_queue if 6 <= s.priority <= 7]
    for s in nearterm[:8]:
        lines.append(f"1. **{s.name}** ({s.category}) — Priority {s.priority}/10")

    lines += [
        "",
        "### Backlog (when capacity allows)",
    ]

    backlog = [s for s in analysis.implementation_queue if s.priority <= 5]
    for s in backlog[:5]:
        lines.append(f"1. **{s.name}** ({s.category}) — Priority {s.priority}/10")

    lines.append("")
    return "\n".join(lines)


def run_research_cycle() -> GapAnalysis:
    """Run a full research cycle: analyze gaps and save report.

    This is the main entry point called by the scheduler.
    """
    log.info("Running strategy research cycle...")

    analysis = analyze_gaps()
    log.info("Strategy research: %s", analysis.summary())

    # Save report to knowledge dir
    report_path = KNOWLEDGE_DIR / "strategies" / "strategy-research-report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = generate_research_report()
    report_path.write_text(report)
    log.info("Research report saved to %s", report_path)

    return analysis
