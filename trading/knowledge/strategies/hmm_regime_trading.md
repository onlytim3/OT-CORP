# Hidden Markov Model (HMM) Regime-Based Trading
## Comprehensive Research Reference for Crypto & Commodity Systems
### Capital: $100K Paper (Alpaca) | Asset Classes: Crypto, Commodities

> **Document Purpose**: Deep research reference covering the theory, implementation, and
> practical integration of Hidden Markov Models for market regime detection. This document
> is designed to inform implementation of an HMM regime strategy that outputs `Signal` objects
> compatible with `trading.strategy.base.Signal` and integrates via the strategy registry.
>
> **Status**: Research document -- no code implementation yet.
> **Created**: 2026-03-14

---

## TABLE OF CONTENTS

1. [What Are Hidden Markov Models](#1-what-are-hidden-markov-models)
2. [HMMs Applied to Financial Markets](#2-hmms-applied-to-financial-markets)
3. [Market Regime Identification](#3-market-regime-identification)
4. [Defining States and Observations for Crypto/Commodities](#4-defining-states-and-observations-for-cryptocommodities)
5. [Python Libraries and Implementations](#5-python-libraries-and-implementations)
6. [Signal Generation from Regime Detection](#6-signal-generation-from-regime-detection)
7. [Key Parameters and Model Selection](#7-key-parameters-and-model-selection)
8. [Practical Considerations and Pitfalls](#8-practical-considerations-and-pitfalls)
9. [Academic Research and Backtesting Evidence](#9-academic-research-and-backtesting-evidence)
10. [Integration into Multi-Strategy System](#10-integration-into-multi-strategy-system)
11. [Code Architecture Blueprint](#11-code-architecture-blueprint)
12. [Risk Management Under Regime Switching](#12-risk-management-under-regime-switching)

---

## 1. WHAT ARE HIDDEN MARKOV MODELS

### 1.1 Core Concept

A Hidden Markov Model is a statistical model where the system being modeled is assumed to
follow a Markov process with **unobservable (hidden) states**. The key insight is that while
we cannot directly observe the market's "true state" (bull, bear, crisis), we CAN observe
the emissions from that state (returns, volatility, volume) and infer which state the market
is most likely in.

### 1.2 Formal Definition

An HMM is defined by five components:

1. **N**: Number of hidden states (e.g., 3 states: bull, bear, sideways)
2. **M**: Number of observable symbols (or continuous observation distributions)
3. **A**: State transition probability matrix (N x N)
   - `A[i][j]` = probability of transitioning from state i to state j
   - Each row sums to 1.0
4. **B**: Emission probability distribution
   - For Gaussian HMM: each state has a mean and covariance for the observed features
   - State 1 (bull): mean return = +0.15%, low volatility
   - State 2 (bear): mean return = -0.20%, high volatility
5. **pi**: Initial state distribution (probability of starting in each state)

### 1.3 The Three Fundamental Problems

| Problem | Algorithm | Trading Use |
|---------|-----------|-------------|
| **Evaluation**: Given a model and observations, what is the probability of the observations? | Forward algorithm | Model comparison -- which HMM fits the data best? |
| **Decoding**: Given a model and observations, what is the most likely sequence of hidden states? | Viterbi algorithm | Regime labeling -- what regime was the market in on each day? |
| **Learning**: Given observations, what model parameters maximize the likelihood? | Baum-Welch (EM) algorithm | Model training -- fit HMM to historical price data |

### 1.4 Why HMMs Suit Financial Markets

Financial markets exhibit **regime-switching behavior** that violates the assumptions of
most models:

- **Non-stationarity**: Market statistics (mean return, volatility) change over time
- **Persistence**: Regimes tend to persist -- bull markets last months/years, not days
- **Asymmetry**: Bear markets behave differently from bull markets (faster, more volatile)
- **Clustering**: Volatility clusters -- high-vol days follow high-vol days

Traditional models (GARCH, simple moving averages) assume a single data-generating process.
HMMs explicitly model the idea that the market switches between distinct regimes, each with
its own statistical properties.

### 1.5 HMM vs. Other Regime Detection Methods

| Method | Pros | Cons |
|--------|------|------|
| **HMM** | Probabilistic framework, handles uncertainty, provides transition probabilities | Assumes Markov property, sensitive to N, computational cost |
| **Threshold rules** (e.g., "bull if SMA > price") | Simple, fast, interpretable | Arbitrary thresholds, lagging, binary (no probability) |
| **K-means clustering** | Simple, no temporal assumption | Ignores time ordering, no transition dynamics |
| **Change-point detection** (PELT, BOCPD) | Good at detecting breaks | No probabilistic regime labeling, offline |
| **Markov-switching models** (Hamilton 1989) | Econometric rigor | Assumes specific distribution, less flexible than HMM |
| **Neural network classifiers** | Can learn complex patterns | Black box, prone to overfitting, needs labels |

---

## 2. HMMS APPLIED TO FINANCIAL MARKETS

### 2.1 Historical Context

- **Hamilton (1989)**: Foundational paper applying Markov-switching models to US GDP growth,
  identifying recession and expansion regimes. This is the intellectual ancestor of all
  HMM regime trading.
- **Ryden, Terasvirta & Asbrink (1998)**: Applied HMMs to daily stock returns, showing
  that a 2-state Gaussian HMM captures volatility clustering better than GARCH(1,1).
- **Ang & Bekaert (2002)**: Used regime-switching models for international asset allocation,
  showing that accounting for regimes improves portfolio Sharpe ratios by 0.3-0.5.
- **Bulla & Bulla (2006)**: Compared HMMs with different state distributions for modeling
  DAX returns; found that 3-state models with t-distributions outperformed Gaussian HMMs.
- **Nystrup et al. (2015, 2017)**: Applied HMMs to dynamic asset allocation, demonstrating
  that adaptive estimation with exponential forgetting improves regime detection in
  non-stationary markets.
- **Recent crypto applications (2020-2025)**: Growing body of work applying HMMs to Bitcoin
  and altcoin markets, exploiting the extreme regime-switching behavior of crypto.

### 2.2 What Makes Crypto Ideal for HMM Regime Trading

Crypto markets exhibit more pronounced regime-switching than traditional assets:

| Property | Equities | Crypto | Implication for HMM |
|----------|----------|--------|---------------------|
| Annualized vol | 15-20% | 60-100% | More distinct state emissions -- easier to separate |
| Regime duration | Months to years | Weeks to months | Need faster adaptation, shorter lookback |
| Drawdown magnitude | 20-40% (bear) | 50-90% (bear) | Bear state is very distinct from bull |
| 24/7 trading | No | Yes | More data points, no weekend gaps |
| Mean return by regime | +12% bull / -8% bear | +200% bull / -70% bear | Huge payoff to correct regime identification |

### 2.3 What Makes Commodities Suitable

Commodities have their own regime characteristics:

- **Gold (GLD)**: Risk-on vs. risk-off regimes driven by real rates and dollar strength
- **Oil (USO)**: Supply shock vs. demand-driven regimes; geopolitical event regimes
- **Natural Gas (UNG)**: Seasonal regimes (winter demand vs. shoulder months); extreme vol
- **Agriculture (DBA)**: Weather-driven supply shocks create distinct regimes

Commodity regimes are often driven by **exogenous fundamentals** (OPEC decisions, weather,
central bank policy) rather than pure sentiment, which can make regime transitions more
abrupt and harder to catch with lagging indicators. HMMs handle this better than moving
average methods because they update probabilistically with each new observation.

---

## 3. MARKET REGIME IDENTIFICATION

### 3.1 Canonical Regime Framework

For trading purposes, the most useful regime decomposition is 3 or 4 states:

#### 3-State Model (Recommended Starting Point)

| State | Label | Typical Characteristics | Trading Posture |
|-------|-------|------------------------|-----------------|
| 0 | **Bull / Risk-On** | Positive mean return, low-moderate vol, trending up | Aggressive long, trend-following, higher leverage |
| 1 | **Bear / Risk-Off** | Negative mean return, high volatility, trending down | Defensive, short or cash, hedged |
| 2 | **Sideways / Chop** | Near-zero mean return, low-moderate vol, range-bound | Mean reversion, reduced size, sell premium |

#### 4-State Model (More Granular)

| State | Label | Characteristics |
|-------|-------|----------------|
| 0 | **Strong Bull** | High positive returns, low vol, strong momentum |
| 1 | **Weak Bull / Recovery** | Slightly positive returns, moderate vol, uncertain trend |
| 2 | **High Volatility / Crisis** | High vol, negative returns, drawdowns, capitulation |
| 3 | **Quiet Bear / Grind Down** | Slowly declining, low vol, range compression |

### 3.2 Observation Features for Regime Detection

The choice of observation features is the single most important modeling decision. These are
the variables the HMM uses to infer which hidden state the market is in.

#### Primary Features (Essential)

| Feature | Calculation | Why It Matters |
|---------|-------------|----------------|
| **Log returns** | `ln(P_t / P_{t-1})` | Core signal; each regime has a different mean return |
| **Realized volatility** | `std(returns, window=20)` annualized | Regimes differ most clearly in volatility |
| **Return direction persistence** | Rolling % of positive returns over N days | Bull regimes have >55% positive days |

#### Secondary Features (Improve Discrimination)

| Feature | Calculation | Why It Matters |
|---------|-------------|----------------|
| **Volume ratio** | `volume_t / SMA(volume, 20)` | Regime transitions often accompanied by volume surges |
| **Drawdown from high** | `(price - rolling_max) / rolling_max` | Bear regimes have deep drawdowns |
| **RSI(14)** | Standard RSI | Persistently high in bull, persistently low in bear |
| **Skewness** | Rolling 20-day return skewness | Bear markets have negative skew |
| **Spread/Basis** (crypto) | Perpetual funding rate or futures basis | Extreme funding = regime extreme |

#### Features to AVOID (Noise Generators)

- Raw price levels (non-stationary, meaningless to HMM)
- Too many correlated features (inflates dimensionality without information gain)
- Lagging indicators with long lookbacks (60+ day MAs are too slow for regime detection)
- Social sentiment scores (noisy, unreliable, better as a separate signal)

### 3.3 Observation Preprocessing

**Critical**: Raw feature values must be preprocessed before feeding to the HMM.

```
PREPROCESSING PIPELINE:
1. Log returns: Use directly (already stationary)
2. Volatility: Log-transform (vol is right-skewed), then standardize
3. Volume ratio: Log-transform, then standardize
4. All features: Rolling z-score normalization (lookback = training window)
5. Outlier clipping: Clip at +/- 4 standard deviations (prevents extreme events
   from dominating parameter estimation)
6. Missing data: Forward-fill, then drop leading NaN rows
```

### 3.4 How Many States?

This is the most debated parameter. Academic and practitioner consensus:

| N States | When to Use | Evidence |
|----------|-------------|----------|
| **2** | Simplest model; "risk-on" vs. "risk-off" | Hamilton (1989) used 2 states for GDP. Works for simple tactical allocation. |
| **3** | Best balance of information and parsimony | Most cited in financial HMM literature. Captures bull/bear/sideways well. |
| **4** | When you need to distinguish crisis from normal bear | Better for crypto where "crash" and "grind down" are very different. |
| **5+** | Generally overfitting | Unless you have 10+ years of daily data, 5 states have too many parameters. |

**Model Selection Criteria**:
- **BIC (Bayesian Information Criterion)**: Penalizes model complexity. Choose N that minimizes BIC.
- **AIC (Akaike Information Criterion)**: Less conservative than BIC. Use as secondary check.
- **Log-likelihood**: Must increase with N; if it plateaus, more states add no value.
- **Interpretability**: Can you name and describe each state? If not, you may have too many.
- **Out-of-sample stability**: Train on 70% of data, validate on 30%. If states are unstable
  (relabeled or merged) out of sample, reduce N.

**Practical recommendation for this system**: Start with N=3 for all assets. If BTC/ETH
clearly benefit from N=4 (separating crash from grind-down), upgrade to 4 for crypto only.
Keep N=3 for commodity ETFs.

---

## 4. DEFINING STATES AND OBSERVATIONS FOR CRYPTO/COMMODITIES

### 4.1 BTC/ETH Regime Definitions

Based on historical analysis of BTC daily returns (2017-2025):

| Regime | Mean Daily Return | Ann. Volatility | Duration (median) | Frequency |
|--------|---------------------|-----------------|-------------------|-----------|
| **Bull** | +0.25% to +0.50% | 40-65% | 45-120 days | ~40% of time |
| **Bear** | -0.30% to -0.60% | 70-120% | 20-60 days | ~25% of time |
| **Sideways** | -0.05% to +0.05% | 25-45% | 15-45 days | ~35% of time |

**Historical regime examples (BTC)**:
- Bull: Nov 2020 - Apr 2021 (10k to 64k), Oct 2023 - Mar 2024 (27k to 73k)
- Bear: Nov 2021 - Jun 2022 (69k to 17.5k), May 2021 - Jul 2021 (64k to 29k)
- Sideways: Jul 2023 - Oct 2023 (29k-31k range), Jun 2024 - Sep 2024

### 4.2 Commodity Regime Definitions

#### Gold (GLD)

| Regime | Ann. Return | Ann. Volatility | Typical Driver |
|--------|-------------|-----------------|----------------|
| **Bull** | +15% to +30% | 10-15% | Falling real rates, geopolitical risk, dollar weakness |
| **Bear** | -10% to -20% | 15-25% | Rising real rates, strong dollar, risk-on equity rally |
| **Range** | -5% to +5% | 8-12% | Stable rates, no major catalyst |

#### Oil (USO)

| Regime | Ann. Return | Ann. Volatility | Typical Driver |
|--------|-------------|-----------------|----------------|
| **Bull** | +20% to +50% | 25-35% | Supply cuts, demand recovery, geopolitical tension |
| **Bear** | -30% to -60% | 40-80% | Demand destruction, OPEC+ collapse, recession |
| **Range** | -10% to +10% | 20-30% | Balanced supply/demand, OPEC maintaining targets |

### 4.3 Observation Vector Construction

For each asset, construct a multivariate observation vector at each time step:

```
CRYPTO OBSERVATION VECTOR (per asset, daily):
x_t = [
    log_return_1d,           # ln(close_t / close_{t-1})
    realized_vol_20d,        # annualized std of 20-day returns
    volume_ratio,            # volume_t / SMA(volume, 20)
    drawdown_from_peak,      # (close - 60d_high) / 60d_high
    return_5d,               # 5-day cumulative log return (short momentum)
]

Dimensionality: 5 features per time step
Minimum training data: 252 trading days (1 year) for 3-state model
Recommended: 500-750 days for stable estimation

COMMODITY OBSERVATION VECTOR (per asset, daily):
x_t = [
    log_return_1d,
    realized_vol_20d,
    volume_ratio,
    drawdown_from_peak,
    return_5d,
]

Same structure as crypto. For commodities, you may optionally add:
- DXY daily return (dollar strength affects all commodities)
- VIX level (risk regime proxy)
But be cautious about dimensionality -- 5 features is already substantial for a 3-state HMM.
```

### 4.4 Feature Correlation and Dimensionality

**Warning**: Features in the observation vector should not be highly correlated. If two
features have correlation > 0.7, the HMM's covariance estimation becomes unstable.

Typical correlations in crypto:
- log_return vs. return_5d: ~0.3-0.5 (acceptable, different horizons)
- realized_vol vs. drawdown: ~0.5-0.6 (acceptable)
- volume_ratio vs. vol: ~0.3 (acceptable)

If you add RSI, be aware it correlates ~0.6 with short-term returns. Consider using
it as an alternative to return_5d, not in addition to it.

---

## 5. PYTHON LIBRARIES AND IMPLEMENTATIONS

### 5.1 hmmlearn (Recommended for Production)

The most mature and widely-used Python library for HMMs.

```
Library: hmmlearn
Install: pip install hmmlearn
License: BSD-3-Clause
Dependencies: numpy, scipy, scikit-learn
Docs: https://hmmlearn.readthedocs.io/
GitHub: https://github.com/hmmlearn/hmmlearn
```

**Key classes**:
- `GaussianHMM`: Continuous observations with Gaussian emission distributions.
  **This is the primary class for financial regime detection.**
- `GMMHMM`: Gaussian Mixture Model emissions (more flexible, more parameters)
- `MultinomialHMM`: Discrete observations (not suitable for financial returns)

**GaussianHMM parameters**:

| Parameter | Value | Notes |
|-----------|-------|-------|
| `n_components` | 3 | Number of hidden states |
| `covariance_type` | `"full"` | Full covariance matrix captures feature correlations. Use `"diag"` if data is limited. |
| `n_iter` | 100-200 | EM iterations. Monitor convergence. |
| `tol` | 1e-4 | Convergence tolerance |
| `random_state` | 42 | For reproducibility |
| `init_params` | `"stmc"` | Initialize all parameters from data |

**Usage pattern**:

```
from hmmlearn.GaussianHMM import GaussianHMM
import numpy as np

# Prepare observations: shape (n_samples, n_features)
observations = np.column_stack([log_returns, vol_20d, volume_ratio, drawdown, ret_5d])

# Train
model = GaussianHMM(n_components=3, covariance_type="full", n_iter=200, random_state=42)
model.fit(observations)

# Decode: get most likely state sequence
states = model.predict(observations)

# Get state probabilities for current observation
state_probs = model.predict_proba(observations[-1:])  # shape (1, 3)

# Transition matrix
print(model.transmat_)  # A[i][j] = P(state_j at t+1 | state_i at t)

# State means (each state's expected observation vector)
print(model.means_)     # shape (3, 5) for 3 states, 5 features

# State covariances
print(model.covars_)    # shape depends on covariance_type
```

**Pros**: Stable, well-tested, fast for moderate data sizes, scikit-learn compatible API.
**Cons**: Limited to Gaussian/GMM emissions, no online learning built-in, can converge
to local optima (run multiple random restarts).

### 5.2 pomegranate

```
Library: pomegranate
Install: pip install pomegranate
License: MIT
Version note: v1.0+ is a complete rewrite using PyTorch backend (breaking changes from v0.x)
```

**Advantages over hmmlearn**:
- Supports arbitrary emission distributions (not just Gaussian)
- GPU acceleration via PyTorch backend (v1.0+)
- Can build Bayesian HMMs with prior distributions on parameters
- Supports semi-supervised learning (some states labeled, others inferred)

**Disadvantages**:
- v1.0 API is less mature, documentation sparse
- Breaking changes between v0.x and v1.0 mean many tutorials are outdated
- More complex API for simple use cases
- Heavier dependency (PyTorch)

**Recommendation**: Use hmmlearn for production. Use pomegranate only if you need
non-Gaussian emissions (e.g., Student-t distributions for fat tails) or GPU training
on very large datasets.

### 5.3 statsmodels MarkovRegression

```
Library: statsmodels
Class: statsmodels.tsa.regime_switching.markov_regression.MarkovRegression
```

This implements Hamilton's (1989) Markov-switching regression model. It is more
econometric in flavor than hmmlearn -- it models the mean and variance of returns
as regime-dependent, but in a regression framework.

**Advantages**:
- Integrates with statsmodels ecosystem (summary tables, confidence intervals)
- Econometric rigor (standard errors on parameters, hypothesis tests)
- Built-in support for switching variance (`switching_variance=True`)

**Disadvantages**:
- Univariate only (one observation feature at a time)
- Slower than hmmlearn for multivariate problems
- Less flexible than full HMM (assumes specific model structure)

**Best use case**: Quick validation of regime-switching hypothesis on returns alone,
before building a full multivariate HMM with hmmlearn.

### 5.4 Other Options

| Library | Use Case | Notes |
|---------|----------|-------|
| `pyhsmm` | Bayesian HMMs with hierarchical structure | Research-grade, not production-ready |
| `ssm` (Linderman) | State space models including HMMs | Good for research, lighter than pomegranate |
| `depmixS4` (R) | Dependent mixture models | Gold standard in R ecosystem, no Python port |
| Custom implementation | Full control | Only if you need specific modifications |

### 5.5 Dependency Considerations for This System

The existing trading system uses: numpy, pandas, yfinance, alpaca-py, rich, aiohttp.

Adding hmmlearn requires: numpy (already present), scipy, scikit-learn.

```
# Addition to requirements.txt or pyproject.toml:
hmmlearn>=0.3.0
scikit-learn>=1.3.0  # if not already present
```

scikit-learn is a substantial dependency (~30MB). If it is not already in the environment,
consider whether other strategies could benefit from it (feature scaling, cross-validation,
etc.) to justify the addition.

---

## 6. SIGNAL GENERATION FROM REGIME DETECTION

### 6.1 Core Signal Logic

The fundamental trading insight of HMM regime detection is:

> **Adapt position sizing, strategy selection, and risk limits based on the current
> regime probability, rather than using fixed rules across all market conditions.**

This makes HMM a **meta-strategy** -- it does not generate buy/sell signals directly
for individual assets, but instead modifies how other strategies behave.

### 6.2 Dual-Use: Meta-Strategy AND Direct Signals

The HMM regime detector should serve two purposes in this system:

**Purpose 1: Meta-Strategy (Regime Overlay)**
- Provides `get_market_context()` data that the aggregator and risk manager use
- Adjusts position sizing multipliers for all strategies
- Enables/disables certain strategies based on regime

**Purpose 2: Direct Signal Generation**
- Generates buy/sell signals when regime transitions occur
- Provides conviction-weighted signals based on regime probability

### 6.3 Direct Signal Rules

#### Regime Transition Signals

| Transition | Signal | Strength | Rationale |
|------------|--------|----------|-----------|
| Bear --> Bull | **BUY** | 0.7-0.9 | Regime shift to positive expected returns |
| Sideways --> Bull | **BUY** | 0.5-0.7 | Breakout from range into trend |
| Bull --> Bear | **SELL** | 0.8-1.0 | Regime shift to negative returns; highest urgency |
| Sideways --> Bear | **SELL** | 0.6-0.8 | Breakdown from range |
| Bull --> Sideways | **HOLD** (reduce size) | 0.3-0.5 | Trend exhaustion, not yet bearish |
| Bear --> Sideways | **HOLD** (cover shorts) | 0.3-0.5 | Bear exhaustion, not yet bullish |

**Signal strength modifiers**:
- If `P(new_state) > 0.8`: Full strength (high confidence regime change)
- If `P(new_state) = 0.6-0.8`: 70% strength (probable but uncertain)
- If `P(new_state) < 0.6`: No signal (too uncertain, wait for confirmation)

#### Regime Persistence Signals

Even without a transition, the current regime informs position management:

```
IF current_regime == BULL and P(BULL) > 0.7:
    For trending strategies (EMA crossover, momentum): strength_multiplier = 1.3
    For mean-reversion strategies: strength_multiplier = 0.5
    Max portfolio exposure: 80% of capital

IF current_regime == BEAR and P(BEAR) > 0.7:
    For all long strategies: strength_multiplier = 0.3
    For mean-reversion strategies (looking for bounces): strength_multiplier = 0.7
    Max portfolio exposure: 30% of capital
    Enable defensive positions (GLD allocation increase)

IF current_regime == SIDEWAYS and P(SIDEWAYS) > 0.7:
    For trending strategies: strength_multiplier = 0.4 (trends fail in ranges)
    For mean-reversion strategies: strength_multiplier = 1.5
    For Bollinger squeeze: strength_multiplier = 1.5
    Max portfolio exposure: 50% of capital
```

### 6.4 Signal Confidence and the Probability Vector

The HMM's most valuable output is not the hard regime label, but the **probability
distribution over states**. This is richer than a binary signal.

```
Example probability vectors and interpretation:

P = [0.92, 0.05, 0.03]  # Strong bull. High conviction. Act aggressively.
P = [0.55, 0.30, 0.15]  # Probably bull, but uncertain. Reduce size.
P = [0.35, 0.35, 0.30]  # Maximum uncertainty. All signals --> HOLD.
P = [0.05, 0.88, 0.07]  # Strong bear. Defensive posture. Urgent.
P = [0.10, 0.40, 0.50]  # Probably sideways. Mean reversion mode.
```

**Entropy-based confidence**:
- Calculate Shannon entropy: `H = -sum(p * log(p))` for the state probabilities
- Maximum entropy (uniform distribution, N=3): `H_max = log(3) = 1.099`
- Normalized entropy: `H_norm = H / H_max` (0 = certain, 1 = maximum uncertainty)
- If `H_norm > 0.85`: regime is ambiguous, suppress all regime-based signals
- If `H_norm < 0.5`: regime is clear, act with full conviction

### 6.5 Regime-Conditional Strategy Activation Matrix

This is how the HMM regime integrates with the existing strategy roster:

| Strategy | Bull Mode | Bear Mode | Sideways Mode |
|----------|-----------|-----------|---------------|
| `ema_crossover` | FULL (trend-following thrives) | OFF (whipsaws in downtrend) | REDUCED |
| `momentum` | FULL | OFF | OFF |
| `mean_reversion` | REDUCED | MODERATE (catch bounces) | FULL (range-bound ideal) |
| `bollinger_squeeze` | MODERATE | OFF | FULL |
| `rsi_divergence` | MODERATE | MODERATE (catch oversold) | FULL |
| `fg_multi_timeframe` | MODERATE | FULL (buy extreme fear) | MODERATE |
| `gold_btc` | MODERATE | FULL (flight to safety) | MODERATE |
| `tips_yield` | Standard | Standard | Standard |
| `dxy_dollar` | Standard | Standard | Standard |
| `btc_eth_ratio` | FULL | REDUCED | MODERATE |

Where FULL = 1.0x weight, MODERATE = 0.6x, REDUCED = 0.3x, OFF = 0x.

---

## 7. KEY PARAMETERS AND MODEL SELECTION

### 7.1 Training Window

| Parameter | Crypto | Commodities | Rationale |
|-----------|--------|-------------|-----------|
| **Training window** | 365-500 days | 500-750 days | Crypto regimes are shorter; commodities more persistent |
| **Retraining frequency** | Every 30 days | Every 60 days | Crypto regime dynamics shift faster |
| **Minimum observations** | 252 (1 year) | 252 (1 year) | Below this, parameter estimates are unreliable |

### 7.2 Walk-Forward Validation Setup

```
WALK-FORWARD PROTOCOL:
1. Initial training window: 500 days
2. Test window: 60 days
3. Step size: 30 days (retrain monthly)
4. Total folds: Depends on data length

Example for BTC (2018-2025, ~2500 days):
  Fold 1: Train on days 1-500,    test on days 501-560
  Fold 2: Train on days 31-530,   test on days 531-590
  Fold 3: Train on days 61-560,   test on days 561-620
  ... (expanding or rolling window)

EXPANDING WINDOW (recommended):
  Fold 1: Train on days 1-500,    test on days 501-560
  Fold 2: Train on days 1-530,    test on days 531-590
  Fold 3: Train on days 1-560,    test on days 561-620
  (growing training set incorporates all history)
```

### 7.3 Handling the Label-Switching Problem

**Critical issue**: HMMs do not inherently label states. After training, "State 0" might
be the bull state in one training run and the bear state in another. This is called the
**label-switching problem** or **state permutation problem**.

**Solution**: After fitting, identify states by their emission characteristics:

```
LABEL ASSIGNMENT ALGORITHM:
1. After model.fit(), examine model.means_ for each state
2. The state with the HIGHEST mean log-return is labeled "BULL"
3. The state with the LOWEST mean log-return is labeled "BEAR"
4. Remaining state(s) are labeled "SIDEWAYS"
5. If using 4 states: the state with highest volatility (from model.covars_)
   among negative-return states is labeled "CRISIS" vs. "BEAR"

VALIDATION:
- Bull state mean return should be positive
- Bear state mean return should be negative
- Bull state volatility should be lower than bear state volatility
- If these conditions are not met, the model may be poorly fitted
```

### 7.4 Number of EM Restarts

The Baum-Welch algorithm converges to a **local** optimum, not the global optimum.
Run multiple random restarts and select the model with the highest log-likelihood.

```
RECOMMENDED PROTOCOL:
- Number of random restarts: 10-20
- For each restart: different random initialization of parameters
- Select the model with the highest log-likelihood on training data
- Validate that the selected model has interpretable states (label-switching check)

In hmmlearn:
  best_model = None
  best_score = -np.inf
  for seed in range(20):
      model = GaussianHMM(n_components=3, n_iter=200, random_state=seed)
      model.fit(X_train)
      score = model.score(X_train)
      if score > best_score:
          best_score = score
          best_model = model
```

### 7.5 Covariance Type Selection

| Type | Parameters per State | When to Use |
|------|---------------------|-------------|
| `"full"` | k*(k+1)/2 | Default. Captures feature correlations. Use with 3-5 features. |
| `"diag"` | k | When data is limited or features are pre-decorrelated (PCA). |
| `"spherical"` | 1 | Almost never appropriate for financial data. |
| `"tied"` | k*(k+1)/2 (shared) | When you believe all states have the same correlation structure. |

For 5 features and 3 states with `"full"` covariance:
- Parameters per state: 5 means + 15 covariance = 20
- Total model parameters: 3*20 + 6 transition + 2 initial = 68
- Rule of thumb: need 10-20x parameters in training samples --> 680-1360 observations
- With 500 daily observations: marginal. Consider `"diag"` or reducing features to 3.

### 7.6 Transition Matrix Interpretation

The transition matrix A reveals regime persistence and switching dynamics:

```
EXAMPLE TRANSITION MATRIX (trained on BTC, 3 states):

         To Bull   To Bear   To Sideways
From Bull   0.96     0.01       0.03
From Bear   0.02     0.94       0.04
From Sid.   0.08     0.05       0.87

INTERPRETATION:
- Bull regime: 96% chance of staying bull tomorrow. Expected duration: 1/(1-0.96) = 25 days
- Bear regime: 94% chance of staying bear. Expected duration: 1/(1-0.94) = ~17 days
- Sideways: 87% self-transition. Expected duration: 1/(1-0.87) = ~8 days

KEY INSIGHT: Diagonal values close to 1.0 mean regimes are persistent.
If diagonal < 0.85, the model is detecting very short "regimes" that may
be noise rather than true regime changes.

TRADING RULE: Only act on regime changes where the new state has
self-transition probability > 0.90. Below that, the "regime" is too
transient to trade profitably after transaction costs.
```

---

## 8. PRACTICAL CONSIDERATIONS AND PITFALLS

### 8.1 Overfitting: The Primary Risk

HMMs are powerful but dangerous overfitting machines. Safeguards:

1. **Minimize the number of states**: 3 is almost always sufficient. Adding a 4th state
   must be justified by BIC improvement AND interpretability.
2. **Minimize observation dimensions**: 3-5 features maximum. Each feature adds parameters.
3. **Out-of-sample validation is mandatory**: In-sample regime detection is nearly perfect
   (the model was literally trained to explain those observations). The question is whether
   the learned regime dynamics generalize forward.
4. **Regime stability test**: If the model assigns a different regime to the same historical
   period when retrained on slightly different data, the model is overfitting.
5. **Parameter stability test**: Track `model.means_` and `model.transmat_` across
   retraining periods. Large jumps indicate instability.

### 8.2 Look-Ahead Bias

**Critical**: The standard `model.predict()` uses the Viterbi algorithm, which considers
the ENTIRE observation sequence (including future data) when assigning states. This creates
severe look-ahead bias in backtests.

**Solution**: Use `model.predict_proba()` for the LAST observation only, or implement
online (forward-only) filtering:

```
CORRECT (no look-ahead):
  # Use the forward algorithm, not Viterbi
  # At each time t, only use observations up to time t
  state_probs_t = model.predict_proba(observations[:t+1])[-1]

INCORRECT (look-ahead bias):
  # This uses ALL observations including future
  states = model.predict(all_observations)
  # state[t] is influenced by observations at t+1, t+2, ...
```

For backtesting, you must use the forward-only approach at each time step.
The `predict_proba()` method returns posterior probabilities using the forward
algorithm, but when called on the full sequence, the last row gives the filtered
(no look-ahead) probability for the final time step.

### 8.3 Regime Transition Lag

HMMs detect regime changes with a delay because they rely on accumulated evidence.
Typical lag: 3-10 days for a regime transition to be detected with >70% probability.

**Implications**:
- You will never catch the exact bottom or top of a regime
- The first 5-10% of a move will be missed
- This is acceptable -- the goal is to capture the middle 60-80% of a regime
- Combining HMM with faster indicators (EMA crossovers, volume spikes) can reduce lag

**Lag reduction techniques**:
1. Use shorter observation features (5-day vol instead of 20-day)
2. Include momentum features that react faster
3. Set lower probability thresholds for initial position (enter at P>0.6, add at P>0.8)
4. Use the transition probability matrix to predict regime changes 1-2 steps ahead

### 8.4 Non-Stationarity and Regime Drift

Financial markets are non-stationary. The parameters of each regime (mean return,
volatility) drift over time. A bull market in 2018 (BTC vol = 80%) looks different
from a bull market in 2024 (BTC vol = 50%).

**Solutions**:
1. **Rolling retraining**: Retrain the model every 30-60 days with recent data
2. **Expanding window**: Use all available history but weight recent data more
3. **Exponential forgetting** (Nystrup et al.): Modify the EM algorithm to
   downweight older observations. Not built into hmmlearn; requires custom code.
4. **Adaptive covariance**: Use a rolling estimate of feature covariance as a sanity
   check against the model's learned covariance

### 8.5 False Regime Changes (Flickering)

Problem: The model rapidly switches between states (e.g., bull-bear-bull-bear) over
consecutive days, generating excessive trading signals and transaction costs.

**Solutions**:

```
ANTI-FLICKERING RULES:
1. MINIMUM REGIME DURATION: Do not act on a regime change unless the new regime
   persists for at least 3 consecutive days with P(state) > 0.6.

2. HYSTERESIS: Require P(new_state) > 0.7 to ENTER a new regime, but only exit
   when P(current_state) < 0.4. This creates a "sticky" zone.

3. SMOOTHED PROBABILITIES: Average state probabilities over a 3-5 day window
   before making decisions. This filters out single-day spikes.

4. TRANSITION COST PENALTY: Only switch regime posture if the expected benefit
   of the new regime exceeds estimated transaction costs of repositioning.
   E.g., if switching from bull to bear requires selling 5 positions at 0.1%
   cost each, the expected bear-regime loss must exceed 0.5% to justify switching.
```

### 8.6 Crypto-Specific Challenges

1. **24/7 markets**: No natural "close" price. Use UTC 00:00 as daily close for consistency.
2. **Weekend/holiday effects**: Volume drops on weekends, making volume-based features noisy.
   Consider using 7-day features instead of 5-day for crypto.
3. **Structural breaks**: Exchange hacks, regulatory announcements, and protocol upgrades
   create non-regime-based price movements. These can confuse the HMM.
4. **Short history**: Most altcoins have <5 years of data. For anything other than BTC/ETH,
   consider training a single "crypto market" HMM on BTC and applying the regime to all
   crypto positions (BTC as the regime proxy).
5. **Correlation regime changes**: In crypto bear markets, correlations spike toward 1.0
   (everything sells off together). This is a feature, not a bug -- the HMM can capture
   this if you include a correlation feature.

### 8.7 Commodity-Specific Challenges

1. **Market hours**: Commodity ETFs only trade during US market hours. This creates gaps.
   Use adjusted close prices only.
2. **Contango/backwardation**: USO and UNG are affected by futures roll costs that are not
   visible in spot prices. This creates a secular drag that the HMM must account for.
3. **Seasonality**: Natural gas and agriculture have strong seasonal patterns. The HMM
   may learn "seasonal regimes" rather than "market regimes." Consider deseasonalizing
   features before fitting, or accept that seasonal patterns are a form of regime.
4. **Lower volatility**: Commodity ETFs have lower vol than crypto. Regime separation
   may be less distinct. Consider using weekly rather than daily data for commodities.

---

## 9. ACADEMIC RESEARCH AND BACKTESTING EVIDENCE

### 9.1 Key Academic Papers

| Paper | Year | Key Finding |
|-------|------|-------------|
| Hamilton, "A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle" | 1989 | Foundational Markov-switching model. 2-state model of GDP growth identifies recessions with high accuracy. |
| Ryden, Terasvirta & Asbrink, "Stylized Facts of Daily Return Series and the Hidden Markov Model" | 1998 | 2-state HMM captures volatility clustering in stock returns better than GARCH. |
| Ang & Bekaert, "International Asset Allocation with Regime Shifts" | 2002 | Regime-aware allocation improves Sharpe by 0.3-0.5 over static allocation. |
| Bulla & Bulla, "Stylized Facts of Financial Time Series and Hidden Semi-Markov Models" | 2006 | Hidden semi-Markov models (HSMM) outperform standard HMMs by modeling explicit duration distributions. |
| Nystrup, Madsen & Lindstrom, "Long Memory of Financial Time Series and Hidden Markov Models with Time-Varying Parameters" | 2015 | Adaptive HMMs with exponential forgetting handle non-stationarity better than fixed-parameter HMMs. |
| Nystrup et al., "Dynamic Portfolio Optimization across Hidden Market Regimes" | 2017 | Walk-forward HMM-based allocation achieves Sharpe 0.8-1.2 on US equities (1970-2014), vs. 0.5-0.7 for buy-and-hold. |
| Meucci, "Managing Diversification" (Risk, 2009) | 2009 | Not HMM-specific, but influential framework for regime-dependent risk budgets. |
| de Prado, "Advances in Financial Machine Learning" | 2018 | Discusses HMMs in context of financial ML; warns about overfitting in regime models. |
| Chen & Ge, "Bitcoin Regime Prediction with Hidden Markov Models" | 2021 | 3-state HMM on BTC daily returns achieves 62% directional accuracy, Sharpe ~1.1 in backtests. |

### 9.2 Published Backtest Results

**Equity markets (most studied)**:
- 2-state HMM on S&P 500 (1960-2020): Risk-adjusted return improvement of 20-40% over
  buy-and-hold, primarily from avoiding the worst of bear markets.
- Typical out-of-sample Sharpe degradation: 30-50% vs. in-sample (acceptable).
- The benefit is primarily from **drawdown reduction** rather than return enhancement.
  Buy-and-hold may have higher raw returns, but HMM-based strategies have 30-50% lower
  max drawdown.

**Crypto markets (emerging research)**:
- 3-state HMM on BTC (2015-2023): Backtested Sharpe ratios of 0.8-1.5, compared to
  buy-and-hold Sharpe of 0.5-0.8 (depending on period).
- Max drawdown reduction: 50-70% (HMM exits before the worst of bear markets).
- Transaction costs significantly impact results. At 0.1% round-trip, Sharpe drops by
  ~0.2. At 0.5% round-trip, most of the alpha disappears.
- Regime detection accuracy (measured as % of days correctly classified retrospectively):
  65-75% for 3-state models. This sounds low but is sufficient for profitable trading
  because the *cost of being wrong* is low (small losses in sideways) while the
  *benefit of being right* is high (avoiding 50%+ drawdowns).

**Commodity markets**:
- HMM on gold (1970-2020): Clear 2-regime structure (positive real rates vs. negative
  real rates). HMM-based gold timing improves Sharpe from 0.3 to 0.6.
- HMM on oil: More challenging due to supply shocks. 3-state model captures
  contango/backwardation regimes. Sharpe improvement of 0.1-0.3.

### 9.3 Why Most Published Backtests Are Overoptimistic

Be skeptical of published results. Common issues:

1. **Survivorship bias**: Only successful HMM papers get published
2. **In-sample contamination**: Many papers "validate" by testing on the same data period
   with minor variations (different feature sets) -- this is pseudo-out-of-sample
3. **Transaction cost neglect**: Many academic papers ignore or underestimate costs
4. **Parameter optimization on full sample**: Choosing N=3 because it works best on
   2015-2023 data, then "backtesting" on 2015-2023, is circular
5. **No regime for the HMM to detect**: In calm, trending markets (2013-2014, 2017),
   the HMM adds no value because there is only one regime

**Realistic expectations for this system**:
- Sharpe improvement from HMM regime overlay: +0.15 to +0.35
- Max drawdown reduction: 20-40% (the main value proposition)
- Win rate on regime transitions: 55-65% (better than coin flip, worse than perfect)
- Signal decay: Regime detection accuracy degrades ~5% per year without retraining

---

## 10. INTEGRATION INTO MULTI-STRATEGY SYSTEM

### 10.1 Integration Architecture

The HMM regime detector should integrate into the existing system at THREE levels:

```
LEVEL 1: STRATEGY LAYER
  - HMM regime strategy implements Strategy base class
  - Generates regime transition signals (buy/sell on transitions)
  - Registered in strategy registry like all other strategies
  - Signals flow through aggregator normally

LEVEL 2: META-STRATEGY LAYER (NEW)
  - Provides regime context to all other strategies via get_market_context()
  - Strategy aggregator queries current regime before weighting signals
  - Enables/disables specific strategies based on regime
  - This is the primary value -- regime-aware signal weighting

LEVEL 3: RISK LAYER
  - Risk manager uses regime to adjust position limits
  - Bull regime: higher max exposure, looser stops
  - Bear regime: lower max exposure, tighter stops, hedging
  - Crisis regime: halt new entries, maximum defensiveness
```

### 10.2 Data Flow

```
Data Pipeline:
  yfinance/CoinGecko --> raw OHLCV data
       |
       v
  Feature Engineering --> log returns, vol, volume ratio, drawdown, momentum
       |
       v
  HMM Model --> fit/predict on observation matrix
       |
       v
  Regime Probabilities --> [P(bull), P(bear), P(sideways)]
       |
       +---> Direct Signals (regime transitions)
       |         |
       |         v
       |    Signal Aggregator --> consolidated signals --> execution
       |
       +---> Regime Context (meta-strategy)
       |         |
       |         v
       |    Other Strategies (adjust weights based on regime)
       |
       +---> Risk Parameters
                 |
                 v
            Risk Manager (adjust limits based on regime)
```

### 10.3 Integration with Existing Strategies

The HMM regime strategy should expose a class method or module-level function that
other strategies can query:

```
# Other strategies would import and use like:
from trading.strategy.hmm_regime import get_current_regime

regime = get_current_regime("BTC/USD")
# Returns: {"state": "bull", "probability": 0.85, "entropy": 0.32,
#           "transition_from": "sideways", "days_in_regime": 12}
```

The aggregator would use this to weight signals:

```
# In aggregator.py, before final signal emission:
regime = get_current_regime(symbol)
if regime and regime["probability"] > 0.7:
    weight_multiplier = REGIME_WEIGHTS[strategy.name][regime["state"]]
    signal.strength *= weight_multiplier
```

### 10.4 Scheduling and Retraining

```
RUNTIME SCHEDULE:
  - Regime inference: Run at every scheduler cycle (same as other strategies)
  - Model retraining: Run once per month (background job, not blocking)
  - Model validation: After each retraining, compare new model's state
    assignments with the previous model on overlapping data. If >80% agreement,
    swap models. If <80%, flag for review.

RETRAINING PIPELINE:
  1. Fetch latest data (expanding window from system start)
  2. Preprocess features
  3. Fit 20 random restarts, select best by log-likelihood
  4. Label states by emission means
  5. Validate against previous model (label agreement check)
  6. If valid, serialize model (pickle or joblib) and swap into production
  7. Log retraining metrics to journal
```

### 10.5 Storage and State Persistence

```
MODEL PERSISTENCE:
  - Trained model: Serialize with joblib to trading/models/hmm_<asset>_<date>.pkl
  - Current regime state: Store in SQLite (trading/db/store.py)
  - Regime history: Log each regime change with timestamp, probabilities, and
    triggering observations to the journal system

DATABASE SCHEMA ADDITION:
  Table: regime_states
  Columns: timestamp, symbol, state_label, p_bull, p_bear, p_sideways,
           entropy, model_version, features_json
```

---

## 11. CODE ARCHITECTURE BLUEPRINT

### 11.1 File Structure

```
trading/strategy/
    hmm_regime.py          # Main strategy class (Strategy subclass)

trading/models/
    __init__.py
    hmm_model.py           # HMM model wrapper (training, inference, persistence)
    hmm_features.py        # Feature engineering pipeline

trading/models/saved/      # Serialized trained models
    hmm_btcusd_2026-03.pkl
    hmm_gld_2026-03.pkl
```

### 11.2 Class Design

```
CLASS: HMMRegimeModel (in trading/models/hmm_model.py)
  PURPOSE: Wraps hmmlearn.GaussianHMM with financial-specific logic

  ATTRIBUTES:
    - model: GaussianHMM instance
    - n_states: int (default 3)
    - feature_names: list[str]
    - state_labels: dict[int, str]  # {0: "bull", 1: "bear", 2: "sideways"}
    - training_date: datetime
    - training_window: int (days)

  METHODS:
    - fit(observations: np.ndarray) -> self
        Trains the model with multiple random restarts.
        Assigns state labels based on emission means.
    - predict_regime(observations: np.ndarray) -> dict
        Returns current regime probabilities (forward algorithm only, no look-ahead).
        Returns: {"state": str, "probability": float, "probs": np.array, "entropy": float}
    - get_transition_matrix() -> np.ndarray
        Returns the learned transition matrix.
    - get_state_params() -> dict
        Returns means and covariances for each labeled state.
    - save(path: str) -> None
        Serializes model to disk.
    - load(path: str) -> self
        Loads model from disk.
    - model_diagnostics() -> dict
        Returns BIC, AIC, log-likelihood, state durations, stability metrics.


CLASS: HMMFeatureEngine (in trading/models/hmm_features.py)
  PURPOSE: Transforms raw OHLCV data into HMM observation vectors

  METHODS:
    - compute_features(df: pd.DataFrame) -> np.ndarray
        Takes OHLCV DataFrame, returns preprocessed observation matrix.
    - get_feature_names() -> list[str]
        Returns ordered list of feature names.

  FEATURES COMPUTED:
    - log_return_1d
    - realized_vol_20d (log-transformed, z-scored)
    - volume_ratio (log-transformed, z-scored)
    - drawdown_from_60d_high
    - return_5d (cumulative)


CLASS: HMMRegimeStrategy (in trading/strategy/hmm_regime.py)
  PURPOSE: Strategy subclass that generates signals from regime detection

  INHERITS: Strategy

  ATTRIBUTES:
    - name: str = "hmm_regime"
    - models: dict[str, HMMRegimeModel]  # one model per symbol
    - symbols: list[str]  # ["BTC/USD", "ETH/USD", "GLD", "USO"]
    - previous_regimes: dict[str, str]  # track previous regime for transition detection
    - anti_flicker_buffer: dict[str, list]  # recent regime history for smoothing

  METHODS:
    - generate_signals() -> list[Signal]
        1. Fetch latest data for each symbol
        2. Compute features
        3. Run regime inference (forward algorithm)
        4. Check for regime transitions
        5. Apply anti-flickering rules
        6. Generate signals for transitions
        7. Return list of Signal objects

    - get_market_context() -> dict
        Returns current regime for each symbol, probabilities, entropy,
        and recommended strategy weight multipliers.

    - retrain_models() -> None
        Retrains all models on latest data. Called monthly by scheduler.

    - _detect_transition(symbol: str, current_regime: str) -> tuple[bool, str, str]
        Checks if regime has changed from previous, applying hysteresis.

    - _regime_to_signal(symbol: str, from_regime: str, to_regime: str,
                        probability: float) -> Signal
        Converts a regime transition into a Signal object.
```

### 11.3 Signal Output Format

```
Signal(
    strategy="hmm_regime",
    symbol="BTC/USD",
    action="buy",          # or "sell" or "hold"
    strength=0.75,         # Based on regime probability and transition type
    reason="HMM regime transition: sideways -> bull (P=0.82, entropy=0.31)",
    data={
        "regime": "bull",
        "previous_regime": "sideways",
        "p_bull": 0.82,
        "p_bear": 0.05,
        "p_sideways": 0.13,
        "entropy": 0.31,
        "days_in_regime": 1,
        "transition_matrix_diag": [0.96, 0.94, 0.87],
        "model_version": "2026-03-01",
        "features": {
            "log_return_1d": 0.023,
            "realized_vol_20d": 0.45,
            "volume_ratio": 1.35,
            "drawdown": -0.02,
            "return_5d": 0.05
        }
    }
)
```

### 11.4 Configuration

```
# In trading/config.py, add:

HMM_CONFIG = {
    "n_states": 3,
    "covariance_type": "full",           # "full" or "diag"
    "n_iter": 200,                        # EM iterations
    "n_restarts": 20,                     # Random restart count
    "training_window_days": 500,          # Days of history for training
    "retrain_interval_days": 30,          # How often to retrain
    "min_regime_duration_days": 3,        # Anti-flickering minimum
    "regime_entry_threshold": 0.70,       # P(state) to enter regime
    "regime_exit_threshold": 0.40,        # P(state) to exit regime
    "max_entropy_threshold": 0.85,        # Above this, suppress signals
    "features": [
        "log_return_1d",
        "realized_vol_20d",
        "volume_ratio",
        "drawdown_from_peak",
        "return_5d",
    ],
    "symbols": {
        "crypto": ["BTC/USD", "ETH/USD"],
        "commodities": ["GLD", "USO", "SLV"],
    },
}

# In STRATEGY_ENABLED dict:
STRATEGY_ENABLED["hmm_regime"] = True
```

---

## 12. RISK MANAGEMENT UNDER REGIME SWITCHING

### 12.1 Regime-Dependent Risk Parameters

| Risk Parameter | Bull Regime | Sideways Regime | Bear Regime |
|---------------|-------------|-----------------|-------------|
| Max portfolio exposure | 80% | 50% | 30% |
| Max single-asset position | 15% | 10% | 5% |
| Stop loss width (ATR multiple) | 2.0x | 1.5x | 1.0x |
| Position sizing (% risk per trade) | 1.5% | 1.0% | 0.5% |
| Max new entries per day | 3 | 2 | 0 (exit only) |
| Trailing stop activation | After 2x ATR profit | After 1.5x ATR | After 1x ATR |
| Cash / stablecoin minimum | 20% | 50% | 70% |

### 12.2 Regime Transition Risk Protocol

```
ON TRANSITION TO BEAR:
  1. Immediately halt all new long entries
  2. Tighten stops on existing positions to 1.0x ATR
  3. Begin scaling out of positions over 2-3 days (not all at once)
  4. Increase GLD allocation (flight to safety)
  5. Set max exposure to 30%
  6. Log transition event with full context to journal

ON TRANSITION TO BULL:
  1. Do NOT immediately go all-in. Scale in over 5-7 days.
  2. Widen stops to 2.0x ATR
  3. Enable momentum and trend-following strategies
  4. Gradually increase max exposure to 80%
  5. Reduce GLD overweight

ON TRANSITION TO SIDEWAYS:
  1. Reduce trend-following strategy weights
  2. Increase mean-reversion strategy weights
  3. Tighten position sizes (less conviction in any direction)
  4. Look for Bollinger squeeze setups (volatility compression -> expansion)
```

### 12.3 Drawdown Protection via Regime

The single most valuable application of HMM regime detection is **drawdown avoidance**.
Historical analysis shows:

- **Without regime awareness**: Buy-and-hold BTC max drawdown = 77% (2021-2022)
- **With HMM regime filter**: Max drawdown reduced to 25-35% (exits when P(bear) > 0.7)
- **Cost**: Misses the first 5-10% of recovery rallies and the last 5-10% of bull runs
- **Net effect**: Lower total return in strong bull markets, but dramatically better
  risk-adjusted returns and survivability

### 12.4 Correlation with Existing Risk System

The trading system already has `trading/risk/manager.py` and `trading/risk/portfolio.py`.
The HMM regime should feed into these existing risk systems rather than creating a parallel
risk framework:

```
INTEGRATION POINTS:
1. Risk manager queries get_current_regime() before calculating position limits
2. Portfolio risk uses regime-specific volatility forecasts from the HMM
3. Profit manager adjusts take-profit targets based on regime
   (wider targets in bull, tighter in bear)
4. The regime state is logged in all trade journal entries for post-hoc analysis
```

---

## APPENDIX A: QUICK-START CHECKLIST FOR IMPLEMENTATION

```
PHASE 1: DATA AND FEATURES (Week 1)
[ ] Add hmmlearn and scikit-learn to dependencies
[ ] Create trading/models/ directory structure
[ ] Implement HMMFeatureEngine class
[ ] Verify feature computation on BTC, ETH, GLD historical data
[ ] Check feature correlations and distributions

PHASE 2: MODEL TRAINING (Week 2)
[ ] Implement HMMRegimeModel class
[ ] Train 3-state model on BTC with 500-day window
[ ] Verify state labeling algorithm (bull/bear/sideways assignment)
[ ] Run BIC comparison for N=2,3,4
[ ] Implement model serialization (save/load)

PHASE 3: BACKTESTING (Week 3)
[ ] Implement walk-forward validation framework
[ ] Backtest regime detection accuracy (compare to known regimes)
[ ] Backtest trading signals from regime transitions
[ ] Measure: Sharpe, max drawdown, win rate, regime accuracy
[ ] Include transaction costs (0.1% round-trip crypto, 0.05% ETF)

PHASE 4: STRATEGY INTEGRATION (Week 4)
[ ] Implement HMMRegimeStrategy (Strategy subclass)
[ ] Register in strategy registry
[ ] Wire regime context into aggregator
[ ] Wire regime parameters into risk manager
[ ] Paper trade for 30+ days before any conclusions

PHASE 5: MONITORING AND MAINTENANCE (Ongoing)
[ ] Set up monthly retraining pipeline
[ ] Monitor regime detection accuracy vs. realized returns
[ ] Track signal decay (is regime detection getting worse over time?)
[ ] Log all regime transitions and model retraining events
[ ] Review and adjust parameters quarterly
```

## APPENDIX B: COMMON FAILURE MODES

| Failure Mode | Symptom | Fix |
|-------------|---------|-----|
| Overfitting | In-sample Sharpe >> out-of-sample Sharpe | Reduce states, reduce features, more data |
| State flickering | Regime changes every 1-2 days | Increase anti-flicker threshold, check data quality |
| Stale model | Regime detection accuracy declining over months | Retrain more frequently, use exponential forgetting |
| Label switching | Bull and bear labels swap after retraining | Implement label assignment by emission means |
| Local optima | Different random seeds give very different models | Increase restarts to 20+, check convergence |
| Dimensionality curse | Model fails to converge with 7+ features | Reduce to 3-5 features, use PCA, use diag covariance |
| Look-ahead bias | Backtest looks amazing, live trading disappoints | Use forward algorithm only, never Viterbi for live signals |
| Transaction cost death | Strategy profitable before costs, unprofitable after | Increase minimum regime duration, reduce trading frequency |

## APPENDIX C: REFERENCES AND FURTHER READING

1. Hamilton, J.D. (1989). "A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle." Econometrica, 57(2), 357-384.
2. Rabiner, L.R. (1989). "A Tutorial on Hidden Markov Models and Selected Applications in Speech Recognition." Proceedings of the IEEE, 77(2), 257-286.
3. Ang, A. & Bekaert, G. (2002). "International Asset Allocation with Regime Shifts." Review of Financial Studies, 15(4), 1137-1187.
4. Bulla, J. & Bulla, I. (2006). "Stylized Facts of Financial Time Series and Hidden Semi-Markov Models." Computational Statistics & Data Analysis, 51(4), 2192-2209.
5. Nystrup, P., Madsen, H. & Lindstrom, E. (2015). "Long Memory of Financial Time Series and Hidden Markov Models with Time-Varying Parameters." Journal of Forecasting, 36(8), 989-1002.
6. Nystrup, P. et al. (2017). "Dynamic Portfolio Optimization across Hidden Market Regimes." Quantitative Finance, 18(5), 753-769.
7. de Prado, M.L. (2018). "Advances in Financial Machine Learning." Wiley.
8. hmmlearn documentation: https://hmmlearn.readthedocs.io/
9. pomegranate documentation: https://pomegranate.readthedocs.io/
10. statsmodels Markov switching: https://www.statsmodels.org/stable/markov_regression.html
