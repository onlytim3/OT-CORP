# Advanced Algorithmic Trading Strategies for Crypto & Commodities
## Comprehensive Quantitative Research Reference
### Capital: $100K Paper (Alpaca) | Asset Classes: Crypto, Commodity ETFs

> **Document Purpose**: Deep quantitative reference covering advanced algorithmic strategies
> beyond basic technical analysis. Each strategy includes the mathematical framework, optimal
> parameters for crypto/commodities, expected performance from academic literature, key pitfalls,
> required Python libraries, and integration patterns with `trading.strategy.base.Signal`.
>
> **Critical Warning**: All reported Sharpe ratios and performance metrics are from academic
> backtests and published research. Expect 30-50% degradation in live trading. Every strategy
> here must survive walk-forward out-of-sample testing before deployment.
>
> **Status**: Research document -- guides future strategy implementation.
> **Created**: 2026-03-14

---

## TABLE OF CONTENTS

1. [HMM Regime-Based Trading](#1-hmm-regime-based-trading)
2. [Statistical Arbitrage / Pairs Trading](#2-statistical-arbitrage--pairs-trading)
3. [Trend Following with Adaptive Filters](#3-trend-following-with-adaptive-filters)
4. [Machine Learning Signal Generation](#4-machine-learning-signal-generation)
5. [Volatility Regime Strategies](#5-volatility-regime-strategies)
6. [Order Flow & Market Microstructure](#6-order-flow--market-microstructure)
7. [Cross-Asset Momentum](#7-cross-asset-momentum)
8. [Mean Reversion with Regime Awareness](#8-mean-reversion-with-regime-awareness)
9. [Factor-Based Crypto Investing](#9-factor-based-crypto-investing)
10. [Optimal Position Sizing](#10-optimal-position-sizing)
11. [Strategy Integration Matrix](#11-strategy-integration-matrix)

---

## 1. HMM REGIME-BASED TRADING

> **See also**: `trading/knowledge/strategies/hmm_regime_trading.md` for extended HMM theory.
> This section focuses on practical parameters and integration patterns.

### 1.1 Mathematical Framework

A Hidden Markov Model assumes the market evolves through K unobservable states (regimes),
each characterized by a distinct distribution of returns. The model is defined by:

**State transition matrix** A (K x K):

```
A[i][j] = P(S_t = j | S_{t-1} = i)
```

**Emission distributions** (Gaussian HMM):

```
P(r_t | S_t = k) = N(mu_k, sigma_k^2)
```

where r_t is the log return at time t, mu_k is the mean return in regime k, and
sigma_k is the volatility in regime k.

**Observation vector** (for multivariate Gaussian HMM):

```
O_t = [r_t, sigma_t, volume_ratio_t]

r_t           = log(P_t / P_{t-1})           -- log return
sigma_t       = std(r_{t-19:t})               -- 20-day realized volatility
volume_ratio  = V_t / EMA(V, 20)_t           -- volume relative to trend
```

**Key algorithms**:
- **Forward-backward (Baum-Welch)**: EM algorithm to fit model parameters (A, mu, sigma)
- **Viterbi**: Decode the most likely sequence of hidden states
- **Filtering**: P(S_t | O_{1:t}) -- real-time regime probability using only past data

### 1.2 Optimal Parameters for Crypto/Commodities

| Parameter | Crypto (BTC, ETH) | Commodities (GLD, USO) |
|-----------|-------------------|------------------------|
| Number of states (K) | 3 (bull/bear/sideways) | 2-3 (trending/mean-reverting) |
| Observation features | [log_return, realized_vol, volume_ratio] | [log_return, realized_vol] |
| Training window | 365-730 days | 500-1000 days |
| Retrain frequency | Monthly | Quarterly |
| Covariance type | "full" (captures return-vol correlation) | "diag" (simpler, fewer params) |
| n_iter (EM iterations) | 100 | 100 |
| Random restarts | 10 (pick best log-likelihood) | 10 |

**Crypto 3-state model typical parameters** (from BTC daily data 2017-2025):

| State | Label | Mean daily return | Daily volatility | Avg duration |
|-------|-------|-------------------|------------------|--------------|
| 0 | Bull | +0.15% to +0.35% | 1.5% to 2.5% | 45-90 days |
| 1 | Bear | -0.20% to -0.40% | 3.0% to 5.0% | 20-60 days |
| 2 | Sideways | -0.05% to +0.05% | 1.0% to 2.0% | 30-120 days |

### 1.3 Signal Generation from Regime Transitions

```python
from hmmlearn.hmm import GaussianHMM
import numpy as np

def generate_hmm_signal(prices, volumes, n_states=3, lookback=365):
    """
    Returns regime probabilities and trading signal.

    CRITICAL: Only use data available at time t (no look-ahead).
    Train on [t-lookback : t], predict at t.
    """
    returns = np.log(prices / prices.shift(1)).dropna()
    vol = returns.rolling(20).std()
    vol_ratio = volumes / volumes.ewm(span=20).mean()

    # Build observation matrix (only complete rows)
    obs = np.column_stack([returns, vol, vol_ratio])
    obs = obs[~np.isnan(obs).any(axis=1)]

    # Train on lookback window
    train = obs[-lookback:]

    # Fit with multiple random restarts
    best_model, best_score = None, -np.inf
    for seed in range(10):
        model = GaussianHMM(
            n_components=n_states,
            covariance_type="full",
            n_iter=100,
            random_state=seed,
        )
        model.fit(train)
        score = model.score(train)
        if score > best_score:
            best_model, best_score = model, score

    # Predict current regime (filtering, not smoothing)
    regime_probs = best_model.predict_proba(train)
    current_regime = regime_probs[-1]  # Probabilities at time t

    # Identify bull/bear states by mean return
    means = best_model.means_[:, 0]  # First feature = returns
    bull_state = np.argmax(means)
    bear_state = np.argmin(means)

    # Signal logic
    bull_prob = current_regime[bull_state]
    bear_prob = current_regime[bear_state]

    if bull_prob > 0.70:
        action = "buy"
        strength = min((bull_prob - 0.50) / 0.50, 1.0)
    elif bear_prob > 0.70:
        action = "sell"
        strength = min((bear_prob - 0.50) / 0.50, 1.0)
    else:
        action = "hold"
        strength = 0.0

    return action, strength, current_regime
```

**Signal rules**:

| Condition | Action | Strength |
|-----------|--------|----------|
| P(bull) > 0.70 | BUY | (P(bull) - 0.50) / 0.50 |
| P(bear) > 0.70 | SELL | (P(bear) - 0.50) / 0.50 |
| P(bull) > 0.50 and rising (vs 5d ago) | BUY (weak) | 0.3 |
| P(bear) > 0.50 and rising (vs 5d ago) | SELL (weak) | 0.3 |
| Max probability < 0.50 for any state | HOLD | 0.0 |

### 1.4 Expected Performance

- **Sharpe ratio**: 0.6-1.2 (academic: Ang & Timmermann 2012, Nystrup et al. 2017)
- **Crypto-specific**: BTC regime-switching models show Sharpe 0.8-1.5 in-sample, 0.4-0.9 OOS
- **Key advantage**: Dramatically reduces drawdowns by exiting before bear regimes fully develop
- **Typical hit rate**: 55-65% for regime classification accuracy

### 1.5 Key Pitfalls

1. **Label switching**: HMM states are unordered. After each refit, re-identify which state
   is bull/bear by examining fitted means. Never hardcode state indices.
2. **Overfitting with too many states**: BIC/AIC model selection is mandatory. For crypto,
   K=3 almost always wins over K=4 or K=5.
3. **Regime persistence bias**: HMMs tend to overestimate regime persistence (diagonal of A
   is too high). Cross-validate transition probabilities.
4. **Non-stationarity**: Crypto regime distributions shift over market cycles. Retrain monthly.
5. **Gaussian assumption**: Crypto returns are fat-tailed. Consider Student-t HMM (hmmlearn
   does not support this natively; use pomegranate or custom implementation).

### 1.6 Libraries

```
pip install hmmlearn==0.3.2  # Primary: Gaussian/GMM HMM
pip install pomegranate       # Alternative: supports custom distributions
pip install numpy pandas scikit-learn
```

### 1.7 Signal Framework Integration

```python
@register
class HMMRegimeStrategy(Strategy):
    name = "hmm_regime"

    def generate_signals(self) -> list[Signal]:
        # Fetch 2 years of daily OHLCV from Alpaca
        # Train HMM on [t-365 : t]
        # Predict regime at t
        # Map regime to Signal(action, strength)
        # Include regime_probs in Signal.data for downstream use
        ...
```

**Downstream value**: HMM regime output should be shared with other strategies via the
aggregator's `data` field. Mean reversion strategies should only fire in the sideways regime.
Trend following should only fire in bull/bear regimes.

---

## 2. STATISTICAL ARBITRAGE / PAIRS TRADING

### 2.1 Mathematical Framework

#### 2.1.1 Cointegration Test

Two price series X_t, Y_t are cointegrated if there exists a linear combination that is
stationary:

```
z_t = Y_t - beta * X_t - alpha

where:
  beta  = cointegration coefficient (hedge ratio)
  alpha = intercept
  z_t   = spread (should be stationary if cointegrated)
```

**Engle-Granger two-step procedure**:
1. Regress Y on X: Y_t = alpha + beta * X_t + epsilon_t
2. Test residuals epsilon_t for stationarity using ADF test
3. If ADF p-value < 0.05, the pair is cointegrated

**Johansen test** (preferred for multiple pairs):
- Tests for cointegration rank among N series simultaneously
- Provides both trace and maximum eigenvalue statistics
- Returns cointegration vectors directly

#### 2.1.2 Ornstein-Uhlenbeck (OU) Process

The spread z_t follows a mean-reverting OU process:

```
dz_t = theta * (mu - z_t) * dt + sigma * dW_t

where:
  theta = speed of mean reversion (higher = faster reversion)
  mu    = long-term mean of the spread
  sigma = volatility of the spread
  W_t   = Wiener process (Brownian motion)
```

**Discrete-time estimation** (AR(1) regression):

```
z_t = a + b * z_{t-1} + epsilon_t

theta = -ln(b) / dt          -- mean reversion speed
mu    = a / (1 - b)           -- long-term mean
half_life = ln(2) / theta     -- time for spread to revert halfway
sigma_eq = sigma / sqrt(2 * theta)  -- equilibrium standard deviation
```

### 2.2 Optimal Parameters for Crypto/Commodities

**Crypto pairs with historical cointegration**:

| Pair | Typical beta | Half-life (days) | ADF p-value range |
|------|-------------|-------------------|-------------------|
| BTC/ETH | 0.04-0.06 | 8-25 days | 0.01-0.10 (marginal) |
| ETH/SOL | 0.10-0.20 | 5-15 days | 0.01-0.08 |
| LINK/UNI | 0.8-1.5 | 3-10 days | 0.01-0.05 (strongest) |
| LTC/BCH | 0.5-1.2 | 5-12 days | 0.02-0.08 |

**Commodity pairs with strong cointegration**:

| Pair | Typical beta | Half-life (days) | ADF p-value range |
|------|-------------|-------------------|-------------------|
| GLD/SLV (UGL/AGQ) | 1.5-2.5 | 15-45 days | 0.001-0.05 |
| USO/UNG | 0.3-0.8 | 10-30 days | 0.01-0.10 (unstable) |
| GLD/BTC | varies widely | 20-60 days | 0.05-0.20 (weak) |

**Critical warning**: Crypto cointegration is unstable. Pairs that are cointegrated in one
market cycle often decouple in the next. Re-test cointegration monthly.

#### Entry/Exit Z-Score Rules

```
z_score = (spread - mean) / std

# Standard rules (Gatev et al. 2006, adapted for crypto volatility):
ENTRY_Z = 2.0     # Enter when |z| > 2.0 (crypto: use 1.5-2.0)
EXIT_Z  = 0.5     # Exit when |z| < 0.5 (return to mean)
STOP_Z  = 4.0     # Stop loss when |z| > 4.0 (cointegration breakdown)

# Position direction:
#   z > +ENTRY_Z  =>  SHORT spread (short Y, long X)
#   z < -ENTRY_Z  =>  LONG spread (long Y, short X)
```

### 2.3 Half-Life Estimation

```python
import numpy as np
from statsmodels.tsa.stattools import adfuller
from statsmodels.regression.linear_model import OLS
import statsmodels.api as sm

def estimate_half_life(spread):
    """Estimate mean-reversion half-life from OU process."""
    spread = spread.dropna()
    lag = spread.shift(1).dropna()
    delta = spread.diff().dropna()

    # Align
    lag = lag.iloc[1:]
    delta = delta.iloc[1:]  # already shifted by diff

    # Align indices
    common = lag.index.intersection(delta.index)
    lag = lag.loc[common]
    delta = delta.loc[common]

    # AR(1) regression: delta_z = a + b * z_{t-1}
    X = sm.add_constant(lag)
    model = OLS(delta, X).fit()

    b = model.params.iloc[1]  # coefficient on lagged spread

    if b >= 0:
        return np.inf  # Not mean-reverting

    half_life = -np.log(2) / b
    return half_life

def test_cointegration(y, x, significance=0.05):
    """Engle-Granger cointegration test."""
    # Step 1: OLS regression
    X = sm.add_constant(x)
    model = OLS(y, X).fit()
    residuals = model.resid
    beta = model.params.iloc[1]
    alpha = model.params.iloc[0]

    # Step 2: ADF test on residuals
    adf_stat, p_value, _, _, critical_values, _ = adfuller(residuals, maxlag=20)

    is_cointegrated = p_value < significance
    half_life = estimate_half_life(residuals)

    return {
        "cointegrated": is_cointegrated,
        "p_value": p_value,
        "adf_stat": adf_stat,
        "beta": beta,
        "alpha": alpha,
        "half_life": half_life,
        "residuals": residuals,
    }
```

### 2.4 Expected Performance

- **Sharpe ratio**: 0.8-1.5 for equity stat arb (Avellaneda & Lee 2010)
- **Crypto stat arb**: Higher Sharpe (1.0-2.0) but shorter-lived due to regime instability
- **Commodity stat arb**: Moderate Sharpe (0.6-1.2) but more stable relationships
- **Key metric**: Information ratio should exceed 0.5 after transaction costs
- **Transaction cost sensitivity**: 10 bps round-trip for crypto, 5 bps for commodity ETFs

### 2.5 Key Pitfalls

1. **Cointegration breakdown**: The #1 killer. Must re-test monthly and exit all positions
   if ADF p-value rises above 0.10.
2. **Non-constant hedge ratio**: Use rolling OLS (Kalman filter hedge ratio is better, see
   Section 3) to adapt beta over time.
3. **Stop-loss discipline**: Z-scores exceeding 4.0 indicate structural breakdown, not a
   bigger opportunity. Always exit.
4. **Half-life mismatch**: Only trade pairs with half-life between 5 and 60 days. Shorter
   means transaction costs eat the edge. Longer means capital is tied up too long.
5. **Survivorship bias**: When screening for cointegrated pairs, adjust for multiple testing
   (Bonferroni correction: p < 0.05/N where N = number of pairs tested).

### 2.6 Libraries

```
pip install statsmodels     # OLS, ADF test, Johansen test
pip install numpy pandas
pip install pykalman        # Kalman filter hedge ratio (optional)
```

### 2.7 Signal Framework Integration

```python
@register
class PairsTradingStrategy(Strategy):
    name = "pairs_trading"

    def generate_signals(self) -> list[Signal]:
        # For each candidate pair:
        #   1. Fetch prices from Alpaca
        #   2. Test cointegration (skip if p > 0.05)
        #   3. Compute spread and z-score
        #   4. Generate Signal for each leg
        # Signal.data should include:
        #   {"pair": "ETH/SOL", "z_score": 2.3, "half_life": 12, "adf_p": 0.02}
        ...
```

---

## 3. TREND FOLLOWING WITH ADAPTIVE FILTERS

### 3.1 Kalman Filter Trend Detection

The Kalman filter estimates a time-varying trend (hidden state) from noisy price observations.

**State-space model**:

```
State equation:     x_t = F * x_{t-1} + w_t,    w_t ~ N(0, Q)
Observation eq:     y_t = H * x_t + v_t,         v_t ~ N(0, R)

For trend estimation:
  x_t = [level_t, slope_t]'     -- hidden state (price level + trend)
  y_t = price_t                  -- observed price

  F = [[1, 1],                   -- state transition
       [0, 1]]
  H = [1, 0]                    -- observation matrix
  Q = [[q1, 0],                 -- process noise (controls smoothness)
       [0, q2]]                  -- q2 controls trend responsiveness
  R = r                          -- observation noise
```

**Key parameters**:
- Q/R ratio controls the bias-variance tradeoff:
  - High Q/R: Filter tracks price closely (noisy, responsive)
  - Low Q/R: Filter smooths aggressively (laggy, stable)
- For crypto: Q/R = 0.01-0.1 (more responsive due to strong trends)
- For commodities: Q/R = 0.001-0.01 (smoother, trends develop slowly)

```python
from pykalman import KalmanFilter
import numpy as np

def kalman_trend(prices, observation_noise=1.0, trend_noise=0.01):
    """
    Estimate trend using Kalman filter.
    Returns filtered level, slope, and slope uncertainty.
    """
    kf = KalmanFilter(
        transition_matrices=np.array([[1, 1], [0, 1]]),
        observation_matrices=np.array([[1, 0]]),
        transition_covariance=np.array([
            [observation_noise * 0.1, 0],
            [0, trend_noise]
        ]),
        observation_covariance=np.array([[observation_noise]]),
        initial_state_mean=np.array([prices.iloc[0], 0]),
        initial_state_covariance=np.eye(2) * 100,
    )

    state_means, state_covs = kf.filter(prices.values)

    levels = state_means[:, 0]
    slopes = state_means[:, 1]
    slope_vars = state_covs[:, 1, 1]  # Variance of slope estimate

    return levels, slopes, slope_vars

def kalman_signal(prices, slope_threshold=0.001):
    """Generate trend signal from Kalman slope."""
    levels, slopes, slope_vars = kalman_trend(prices)

    # Standardize slope by its uncertainty
    slope_z = slopes / np.sqrt(slope_vars + 1e-10)

    current_z = slope_z[-1]

    if current_z > 2.0:
        return "buy", min(current_z / 4.0, 1.0)
    elif current_z < -2.0:
        return "sell", min(abs(current_z) / 4.0, 1.0)
    else:
        return "hold", 0.0
```

### 3.2 Adaptive Moving Averages

#### KAMA (Kaufman Adaptive Moving Average)

Adjusts smoothing based on the efficiency ratio (signal/noise):

```
ER = |Price_t - Price_{t-n}| / sum(|Price_i - Price_{i-1}|, i=t-n+1 to t)

SC = [ER * (fast_SC - slow_SC) + slow_SC]^2

fast_SC = 2/(fast_period + 1)     -- default fast_period = 2
slow_SC = 2/(slow_period + 1)     -- default slow_period = 30

KAMA_t = KAMA_{t-1} + SC * (Price_t - KAMA_{t-1})
```

**Parameters for crypto**: n=10 (lookback for ER), fast=2, slow=30
**Parameters for commodities**: n=10, fast=2, slow=30 (same defaults work)

#### FRAMA (Fractal Adaptive Moving Average)

Uses the fractal dimension to adapt the smoothing constant:

```
D = (ln(N1 + N2) - ln(N3)) / ln(2)

where:
  N1 = (max - min) of first half of lookback
  N2 = (max - min) of second half
  N3 = (max - min) of full lookback

alpha = exp(-4.6 * (D - 1))     -- smoothing factor
FRAMA_t = alpha * Price_t + (1 - alpha) * FRAMA_{t-1}
```

**FRAMA vs KAMA for crypto**: FRAMA adapts faster to volatility regime changes, which makes
it slightly better for crypto where volatility clusters are extreme. KAMA is more robust
to noise and better for commodity ETFs.

### 3.3 Volatility-Adjusted Position Sizing

```
position_size = target_risk / (ATR_t / price_t)

where:
  target_risk   = fraction of portfolio to risk (e.g., 0.01 = 1%)
  ATR_t         = Average True Range over 14 periods
  price_t       = current price

# Normalize across assets with different volatility:
vol_weight_i = (1 / sigma_i) / sum(1 / sigma_j for all j)
```

**Crypto-specific ATR scaling**:
- BTC 14-day ATR is typically 3-8% of price
- ETH 14-day ATR is typically 4-10% of price
- GLD 14-day ATR is typically 1-3% of price
- Position sizes for crypto should be 0.3-0.5x of commodity positions to equalize risk

### 3.4 Expected Performance

- **Trend following Sharpe**: 0.4-0.8 historically (AQR, Hurst et al. 2017)
- **Crypto trend following**: Higher Sharpe (0.8-1.5) due to stronger trend persistence
- **Adaptive filters vs fixed MA**: 10-20% improvement in risk-adjusted returns
- **Key advantage**: Positive skewness -- small losses, large wins during strong trends
- **CTA-style portfolios**: Sharpe 0.5-1.0 across diversified commodity/crypto basket

### 3.5 Key Pitfalls

1. **Whipsaw in ranging markets**: Trend following loses money in sideways regimes (30-50%
   of the time). Combine with regime detection (Section 1/8).
2. **Parameter fragility**: If Sharpe changes dramatically with small parameter changes,
   the signal is overfitted. Test sensitivity across +/- 20% of each parameter.
3. **Kalman filter initialization**: Poor initial state estimates cause a burn-in period of
   50-100 observations. Discard signals during this period.
4. **Late entry/early exit**: All trend filters lag. Accept that you will miss the first
   15-25% of a move and give back 10-20% at the end.

### 3.6 Libraries

```
pip install pykalman         # Kalman filter
pip install ta                # Technical analysis (KAMA, ATR)
pip install numpy pandas
# FRAMA: No standard library; implement from formula above (~20 lines)
```

---

## 4. MACHINE LEARNING SIGNAL GENERATION

### 4.1 Problem Formulation

**Classification approach** (preferred over regression for trading signals):

```
Target variable y_t:
  y_t = 1   if forward_return(t, t+h) > threshold
  y_t = -1  if forward_return(t, t+h) < -threshold
  y_t = 0   if |forward_return(t, t+h)| <= threshold

where:
  h = holding period (5 days for swing trading, 1 day for daily)
  threshold = transaction_cost * 2 (minimum edge to overcome costs)

For crypto (0.10% round-trip cost): threshold = 0.20% (1-day), 1.0% (5-day)
For commodity ETFs (0.05% cost):    threshold = 0.10% (1-day), 0.5% (5-day)
```

### 4.2 Feature Engineering

#### OHLCV Features (always available)

```python
def build_ohlcv_features(df):
    """Build features from OHLCV data. All features use only past data."""
    features = {}

    # Returns at multiple horizons
    for d in [1, 2, 3, 5, 10, 21]:
        features[f'ret_{d}d'] = df['close'].pct_change(d)

    # Volatility features
    for w in [5, 10, 21, 63]:
        features[f'vol_{w}d'] = df['close'].pct_change().rolling(w).std()

    # Volume features
    features['vol_ratio_5_20'] = (
        df['volume'].rolling(5).mean() / df['volume'].rolling(20).mean()
    )
    features['vol_zscore'] = (
        (df['volume'] - df['volume'].rolling(60).mean())
        / df['volume'].rolling(60).std()
    )

    # Price position features
    for w in [20, 50, 100]:
        features[f'price_vs_sma_{w}'] = df['close'] / df['close'].rolling(w).mean() - 1

    # Range features
    features['high_low_range'] = (df['high'] - df['low']) / df['close']
    features['close_position'] = (
        (df['close'] - df['low']) / (df['high'] - df['low'] + 1e-10)
    )

    # RSI
    delta = df['close'].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    features['rsi_14'] = 100 - (100 / (1 + gain / (loss + 1e-10)))

    # MACD
    ema12 = df['close'].ewm(span=12).mean()
    ema26 = df['close'].ewm(span=26).mean()
    macd = ema12 - ema26
    signal_line = macd.ewm(span=9).mean()
    features['macd_hist'] = (macd - signal_line) / df['close']

    # Bollinger Band width
    sma20 = df['close'].rolling(20).mean()
    std20 = df['close'].rolling(20).std()
    features['bb_width'] = (2 * std20) / sma20
    features['bb_position'] = (df['close'] - sma20) / (std20 + 1e-10)

    return pd.DataFrame(features, index=df.index)
```

#### Sentiment Features (crypto-specific)

```python
def build_sentiment_features(fg_history):
    """Features from Fear & Greed index."""
    features = {}

    features['fg_current'] = fg_history['value']
    features['fg_7d_avg'] = fg_history['value'].rolling(7).mean()
    features['fg_30d_avg'] = fg_history['value'].rolling(30).mean()
    features['fg_momentum'] = fg_history['value'].diff(7)
    features['fg_zscore'] = (
        (fg_history['value'] - fg_history['value'].rolling(90).mean())
        / fg_history['value'].rolling(90).std()
    )

    return pd.DataFrame(features, index=fg_history.index)
```

#### On-Chain Features (if available via free APIs)

| Feature | Source | Signal meaning |
|---------|--------|---------------|
| Active addresses (7d MA) | Blockchain.com API | Network demand proxy |
| Exchange inflow/outflow | CryptoQuant free tier | Selling/buying pressure |
| Hash rate change (30d) | Blockchain.com API | Miner confidence |
| NVT ratio | Computed (mcap / tx_volume) | Valuation signal |
| MVRV ratio | Glassnode free tier (limited) | Market top/bottom indicator |

### 4.3 Walk-Forward Optimization

**This is non-negotiable. In-sample results are worthless.**

```python
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.metrics import accuracy_score, precision_score, f1_score
import numpy as np

def walk_forward_backtest(X, y,
                          train_window=252,    # 1 year training
                          test_window=21,      # 1 month OOS test
                          step=21,             # Retrain monthly
                          model_class=GradientBoostingClassifier,
                          model_params=None):
    """
    Walk-forward validation: train on past, test on future, slide forward.

    CRITICAL: No future data ever leaks into training.
    """
    if model_params is None:
        model_params = {
            'n_estimators': 100,
            'max_depth': 3,        # SHALLOW trees to prevent overfitting
            'min_samples_leaf': 50, # Large leaf size for regularization
            'learning_rate': 0.05,
            'subsample': 0.8,
        }

    predictions = []
    actuals = []
    dates = []

    for start in range(train_window, len(X) - test_window, step):
        # Training set: [start - train_window : start]
        X_train = X.iloc[start - train_window : start]
        y_train = y.iloc[start - train_window : start]

        # Test set: [start : start + test_window]
        X_test = X.iloc[start : start + test_window]
        y_test = y.iloc[start : start + test_window]

        # Drop rows with NaN
        valid_train = ~(X_train.isna().any(axis=1) | y_train.isna())
        X_train = X_train[valid_train]
        y_train = y_train[valid_train]

        valid_test = ~(X_test.isna().any(axis=1) | y_test.isna())
        X_test = X_test[valid_test]
        y_test = y_test[valid_test]

        if len(X_train) < 100 or len(X_test) < 5:
            continue

        # Fit model
        model = model_class(**model_params)
        model.fit(X_train, y_train)

        # Predict
        preds = model.predict(X_test)
        predictions.extend(preds)
        actuals.extend(y_test.values)
        dates.extend(y_test.index)

    return np.array(predictions), np.array(actuals), dates
```

### 4.4 Model Selection Guidelines

| Model | Pros | Cons | Best for |
|-------|------|------|----------|
| Random Forest | Robust, few hyperparams, handles noise | Lower accuracy than boosting | Initial exploration, feature importance |
| Gradient Boosting (XGBoost/LightGBM) | Best accuracy, handles feature interactions | Overfits easily, more hyperparams | Production signals |
| Logistic Regression | Interpretable, fast, regularizable | Misses nonlinear patterns | Baseline, regime detection |

**Hyperparameter guidelines to prevent overfitting**:

```python
# Gradient Boosting (conservative settings for financial data)
gbm_params = {
    'n_estimators': 100,       # NOT 500+; more trees = more overfitting
    'max_depth': 3,            # NEVER exceed 5 for financial data
    'min_samples_leaf': 50,    # At least 50 samples per leaf
    'learning_rate': 0.05,     # Low learning rate
    'subsample': 0.8,          # Bagging fraction
    'max_features': 0.5,       # Random feature selection
}

# Random Forest
rf_params = {
    'n_estimators': 200,
    'max_depth': 5,
    'min_samples_leaf': 30,
    'max_features': 'sqrt',
}
```

### 4.5 Feature Importance and Selection

```python
def select_features(model, X, threshold=0.02):
    """Keep only features with importance > threshold."""
    importances = model.feature_importances_
    selected = [f for f, imp in zip(X.columns, importances) if imp > threshold]
    return selected

# Rule of thumb: keep 10-20 features maximum for daily frequency trading
# More features = more overfitting risk
# If any single feature has importance > 30%, the model is likely fragile
```

### 4.6 Expected Performance

- **Academic ML trading Sharpe**: 0.5-1.0 (Gu et al. 2020, "Empirical Asset Pricing via ML")
- **Crypto ML signals**: 0.6-1.2 Sharpe reported, but high variance across time periods
- **Realistic expectation**: IC (information coefficient) of 0.02-0.05 per feature
- **Combination benefit**: ML ensemble of 15-20 features typically outperforms any single
  feature by 30-50% in risk-adjusted terms

### 4.7 Key Pitfalls

1. **Overfitting is the #1 risk**: If your in-sample Sharpe is 3.0 and OOS is 0.5, the
   model is overfit. Acceptable ratio: OOS Sharpe >= 50% of IS Sharpe.
2. **Look-ahead bias**: NEVER use future data in features. Common mistakes:
   - Using the full-sample mean/std for z-scoring (use expanding window instead)
   - Including contemporaneous volume with forward returns
   - Training on data that includes the test period
3. **Target leakage**: Forward returns must be computed from the NEXT bar's open, not the
   current bar's close (you cannot trade at the close).
4. **Non-stationarity**: Features must be stationary (returns, z-scores, ratios -- not
   raw prices or raw volume).
5. **Class imbalance**: If 80% of labels are "hold", the model will predict hold always.
   Use balanced class weights or oversample minority classes.

### 4.8 Libraries

```
pip install scikit-learn      # Random Forest, GBM
pip install xgboost           # XGBoost (optional, better performance)
pip install lightgbm          # LightGBM (optional, faster training)
pip install shap              # Feature importance explanation
pip install numpy pandas
```

---

## 5. VOLATILITY REGIME STRATEGIES

### 5.1 GARCH Models for Volatility Forecasting

**GARCH(1,1) model** (the workhorse of vol modeling):

```
r_t = mu + epsilon_t
epsilon_t = sigma_t * z_t,      z_t ~ N(0,1)
sigma_t^2 = omega + alpha * epsilon_{t-1}^2 + beta * sigma_{t-1}^2

Constraints:
  omega > 0, alpha >= 0, beta >= 0
  alpha + beta < 1 (stationarity)

Unconditional variance: sigma^2 = omega / (1 - alpha - beta)
Persistence:            P = alpha + beta (higher = slower vol decay)
Half-life of vol shock: ln(2) / ln(alpha + beta)
```

**Typical GARCH parameters**:

| Asset | alpha | beta | persistence | Vol half-life (days) |
|-------|-------|------|-------------|---------------------|
| BTC | 0.08-0.15 | 0.82-0.90 | 0.95-0.98 | 14-35 |
| ETH | 0.10-0.18 | 0.78-0.88 | 0.93-0.97 | 10-23 |
| GLD | 0.03-0.08 | 0.88-0.95 | 0.95-0.99 | 14-70 |
| USO | 0.05-0.12 | 0.85-0.93 | 0.95-0.98 | 14-35 |

```python
from arch import arch_model
import pandas as pd

def fit_garch_forecast(returns, horizon=5):
    """
    Fit GARCH(1,1) and forecast volatility h days ahead.

    Returns:
        current_vol: annualized current conditional volatility
        forecast_vol: annualized h-day-ahead volatility forecast
        vol_regime: 'high', 'normal', or 'low'
    """
    # Scale returns to percentage
    returns_pct = returns * 100

    model = arch_model(
        returns_pct,
        vol='Garch',
        p=1, q=1,
        dist='t',           # Student-t for fat tails (CRITICAL for crypto)
        rescale=False,
    )

    result = model.fit(diag='off', show_warning=False)

    # Current conditional volatility (annualized)
    current_vol = result.conditional_volatility.iloc[-1] * (252 ** 0.5) / 100

    # Forecast
    forecasts = result.forecast(horizon=horizon)
    forecast_var = forecasts.variance.iloc[-1].values  # h-step variance
    forecast_vol = (forecast_var[-1] ** 0.5) * (252 ** 0.5) / 100

    # Regime classification
    long_term_vol = returns.rolling(252).std().iloc[-1] * (252 ** 0.5)
    vol_ratio = current_vol / long_term_vol if long_term_vol > 0 else 1.0

    if vol_ratio > 1.5:
        regime = 'high'
    elif vol_ratio < 0.7:
        regime = 'low'
    else:
        regime = 'normal'

    return {
        'current_vol': current_vol,
        'forecast_vol': forecast_vol,
        'vol_regime': regime,
        'vol_ratio': vol_ratio,
        'persistence': result.params.get('alpha[1]', 0) + result.params.get('beta[1]', 0),
        'params': dict(result.params),
    }
```

### 5.2 Crypto Volatility Index (DVOL / Implied Vol Proxy)

Since there is no universally accessible free crypto VIX, construct a proxy:

```python
def crypto_vol_index(btc_returns, window_short=7, window_long=30):
    """
    Construct a BTC volatility index from realized vol.

    Uses the ratio of short-term to long-term realized vol as a
    "vol term structure" proxy.
    """
    short_vol = btc_returns.rolling(window_short).std() * (365 ** 0.5)
    long_vol = btc_returns.rolling(window_long).std() * (365 ** 0.5)

    # Vol term structure: > 1 means vol is elevated (contango)
    vol_term = short_vol / long_vol

    # Z-score relative to history
    vol_z = (short_vol - short_vol.rolling(90).mean()) / short_vol.rolling(90).std()

    return {
        'realized_vol_7d': short_vol,
        'realized_vol_30d': long_vol,
        'vol_term_structure': vol_term,
        'vol_zscore': vol_z,
    }
```

**Signal rules from volatility term structure**:

| Vol term structure | Vol z-score | Signal | Rationale |
|-------------------|-------------|--------|-----------|
| > 1.5 (backwardation) | > 2.0 | Reduce exposure | Vol spike, risk-off |
| > 1.2 | > 1.0 | Tighten stops | Elevated risk |
| 0.8 - 1.2 | -1.0 to 1.0 | Normal positioning | Calm market |
| < 0.8 (contango) | < -1.0 | Increase exposure | Vol compression, breakout coming |
| < 0.6 | < -2.0 | MAX exposure + buy vol | Extreme compression = imminent move |

### 5.3 Variance Risk Premium (VRP) Harvesting

The VRP is the difference between implied volatility and realized volatility:

```
VRP = IV^2 - RV^2

Crypto proxy (no options market needed):
  VRP_proxy = GARCH_forecast_vol - subsequent_realized_vol

  Since we cannot know future RV, use:
  VRP_signal = GARCH_5d_forecast - current_5d_realized_vol
```

**Strategy**: When VRP is high (GARCH forecasts much higher vol than currently realized),
sell options (or in our case, reduce position size because vol is expected to mean-revert
lower, which is bullish). When VRP is negative (realized > forecasted), increase hedging.

### 5.4 Vol-of-Vol Trading

```python
def vol_of_vol(returns, vol_window=21, vov_window=63):
    """Compute volatility of volatility."""
    realized_vol = returns.rolling(vol_window).std()
    vov = realized_vol.pct_change().rolling(vov_window).std()

    # High vol-of-vol = unstable regime, reduce exposure
    # Low vol-of-vol = stable regime, increase exposure
    vov_z = (vov - vov.rolling(252).mean()) / vov.rolling(252).std()

    return vov, vov_z
```

### 5.5 Expected Performance

- **GARCH-based timing Sharpe**: 0.3-0.6 standalone (Engle 2004)
- **Vol regime overlay**: Improves portfolio Sharpe by 0.1-0.3 when combined with other strategies
- **VRP harvesting Sharpe**: 0.5-1.0 in equity markets (Carr & Wu 2009), likely similar in crypto
- **Key advantage**: Reduces tail risk exposure, improves Calmar ratio (return/max drawdown)

### 5.6 Key Pitfalls

1. **GARCH estimation instability**: With < 500 daily observations, parameter estimates are
   unreliable. Use at least 2 years of data for crypto.
2. **Student-t vs Normal**: Always use Student-t distribution for crypto. Normal GARCH
   dramatically underestimates tail risk. For BTC, typical degrees of freedom = 3-6.
3. **Vol forecasts are biased high**: GARCH systematically overestimates vol in calm periods
   and underestimates it at the onset of crises. Use as a relative signal, not absolute.
4. **Regime change blindness**: Standard GARCH cannot detect structural breaks. Combine with
   HMM regime detection (Section 1) for regime-aware vol forecasting.

### 5.7 Libraries

```
pip install arch             # GARCH, EGARCH, GJR-GARCH
pip install numpy pandas scipy
```

---

## 6. ORDER FLOW & MARKET MICROSTRUCTURE

### 6.1 VWAP / TWAP Execution

**Volume-Weighted Average Price (VWAP)**:

```
VWAP = sum(Price_i * Volume_i) / sum(Volume_i)

For execution: break order into slices, execute proportional to historical
volume profile. Goal: achieve execution price close to VWAP.
```

**For our system**: Since we trade through Alpaca and positions are modest ($100K portfolio),
execution algorithms are less critical. Focus on avoiding market impact by:
- Placing limit orders at VWAP +/- 0.1% for entries
- Using time-weighted execution for orders > 5% of daily volume

**VWAP as a signal**:

```python
def vwap_signal(prices, volumes, window=20):
    """
    Price vs VWAP as a support/resistance signal.

    Price above VWAP = bullish (buyers in control)
    Price below VWAP = bearish (sellers in control)
    """
    typical_price = (prices['high'] + prices['low'] + prices['close']) / 3
    cum_tp_vol = (typical_price * volumes).rolling(window).sum()
    cum_vol = volumes.rolling(window).sum()
    vwap = cum_tp_vol / cum_vol

    deviation = (prices['close'] - vwap) / vwap

    return deviation  # Positive = above VWAP, negative = below
```

### 6.2 Order Book Imbalance Signals

**Order book imbalance (OBI)**:

```
OBI = (bid_volume - ask_volume) / (bid_volume + ask_volume)

Aggregated across N price levels:
  OBI_N = (sum(bid_vol_1..N) - sum(ask_vol_1..N)) / (sum(bid_vol_1..N) + sum(ask_vol_1..N))

Signal interpretation:
  OBI > +0.3:  Strong buying pressure, bullish
  OBI < -0.3:  Strong selling pressure, bearish
  |OBI| < 0.1: Balanced, no directional signal
```

**Limitation for our system**: Order book data requires exchange-specific WebSocket feeds
(Binance, Coinbase). Alpaca does not provide order book data for crypto. This strategy
requires additional data infrastructure:

```
# Data sources for order book:
# Crypto: Binance WebSocket (wss://stream.binance.com:9443/ws)
# Alternative: CCXT library to poll multiple exchanges
pip install ccxt  # Unified exchange API
```

### 6.3 Trade Flow Toxicity (VPIN)

**Volume-synchronized Probability of Informed Trading (VPIN)**:

```
VPIN = sum(|V_buy_i - V_sell_i|) / (n * V_bar)

where:
  V_buy, V_sell = estimated buy/sell volume per bar (using BVC*)
  V_bar = volume bucket size
  n = number of buckets in estimation window (typically 50)

*BVC (Bulk Volume Classification):
  V_buy = V * Phi(Z),  V_sell = V * (1 - Phi(Z))
  Z = (close - open) / sigma
  Phi = standard normal CDF
```

**VPIN interpretation**:
- VPIN > 0.7: High toxicity -- informed traders are active, danger zone
- VPIN 0.4-0.7: Normal trading activity
- VPIN < 0.4: Low toxicity -- safe to provide liquidity

**Crypto-specific**: VPIN spikes preceded the 2022 Luna/UST crash, FTX collapse, and most
major liquidation cascades. It is a leading indicator of crashes with 2-12 hour lead time.

```python
from scipy.stats import norm
import numpy as np

def compute_vpin(prices_open, prices_close, volumes, sigma, bucket_size, n_buckets=50):
    """
    Compute VPIN using Bulk Volume Classification.

    Parameters:
        prices_open: open prices per bar
        prices_close: close prices per bar
        volumes: volume per bar
        sigma: return standard deviation (rolling estimate)
        bucket_size: volume per bucket (typically daily_volume / 50)
        n_buckets: number of buckets for VPIN (default 50)
    """
    z = (prices_close - prices_open) / (sigma * prices_open + 1e-10)
    v_buy = volumes * norm.cdf(z)
    v_sell = volumes * (1 - norm.cdf(z))

    # Volume bucketing (simplified: use equal-volume bars)
    # In practice, aggregate bars until cumulative volume reaches bucket_size
    imbalance = np.abs(v_buy - v_sell)

    # Rolling VPIN over n_buckets
    vpin = imbalance.rolling(n_buckets).sum() / (volumes.rolling(n_buckets).sum() + 1e-10)

    return vpin
```

### 6.4 Bid-Ask Spread Analysis

```
Spread = (ask - bid) / mid
mid = (ask + bid) / 2

Effective spread = 2 * |execution_price - mid|

Spread signals:
  Widening spread = decreasing liquidity, potential stress
  Narrowing spread = improving liquidity, stable market

  Spread z-score > 2: Market stress, reduce new positions
  Spread z-score < -1: Exceptional liquidity, good entry conditions
```

### 6.5 Expected Performance

- **VWAP execution vs market orders**: Saves 5-20 bps on average for crypto
- **OBI signals Sharpe**: 0.3-0.8 at high frequency, decays rapidly at daily frequency
- **VPIN as risk indicator**: Not a standalone strategy; reduces drawdowns by 20-40% as overlay
- **Microstructure alpha at daily frequency**: Very limited. These signals are most valuable
  intraday (seconds to hours).

### 6.6 Key Pitfalls

1. **Data availability**: Most microstructure signals require tick data or order book snapshots
   that are not available through Alpaca. Plan for additional data costs.
2. **Latency sensitivity**: OBI and VPIN are most valuable at sub-second frequencies. At daily
   frequency, the edge is small (IC < 0.02).
3. **Exchange fragmentation**: Crypto trades across 50+ exchanges. OBI on one exchange may
   not reflect overall market sentiment.
4. **For our system**: The most practical microstructure signal is VWAP deviation (available
   from Alpaca bar data) and VPIN (computable from OHLCV). Order book signals require
   infrastructure beyond current scope.

### 6.7 Libraries

```
pip install ccxt              # Multi-exchange API (order book, trades)
pip install scipy             # Normal CDF for BVC
pip install numpy pandas
```

---

## 7. CROSS-ASSET MOMENTUM

### 7.1 Time-Series Momentum (TSMOM)

Each asset's own past return predicts its future return:

```
Signal_i = sign(r_{i, t-L:t})

where:
  r_{i, t-L:t} = return of asset i over lookback L

Position sizing:
  w_i = (sigma_target / sigma_i) * sign(r_{i, t-L:t})

where:
  sigma_target = portfolio-level target volatility (e.g., 10% annualized)
  sigma_i = asset i's realized volatility (60-day rolling)
```

**Moskowitz, Ooi, Pedersen (2012)**: TSMOM is profitable across all major asset classes with
lookbacks of 1-12 months. The effect is strongest at 12 months and weakest at 1 month.

**Parameters for crypto/commodities**:

| Parameter | Crypto | Commodity ETFs |
|-----------|--------|----------------|
| Lookback L | 7-30 days (faster) | 21-252 days (standard) |
| Optimal L | 21 days (crypto trends are shorter) | 63-126 days |
| Vol target | 20-30% annualized | 10-15% annualized |
| Rebalance frequency | Weekly | Monthly |
| Universe | BTC, ETH, SOL, AVAX, LINK | UGL, AGQ, USO, UNG, DBA |

```python
def tsmom_signal(returns, lookback=21, vol_window=60, vol_target=0.15):
    """
    Time-series momentum signal for a single asset.

    Returns:
        signal: +1 (long) or -1 (short/flat)
        weight: vol-adjusted position weight
    """
    # Momentum signal
    cum_return = (1 + returns).rolling(lookback).apply(lambda x: x.prod() - 1)
    signal = np.sign(cum_return)

    # Volatility scaling
    realized_vol = returns.rolling(vol_window).std() * (252 ** 0.5)
    weight = vol_target / (realized_vol + 1e-10)
    weight = weight.clip(upper=2.0)  # Cap leverage at 2x

    return signal, weight
```

### 7.2 Cross-Sectional Momentum

Rank assets by past returns. Go long the top N, short (or avoid) the bottom N:

```
For N assets over lookback L:
  1. Compute r_i for each asset i
  2. Rank assets by r_i
  3. Long top quintile, short bottom quintile (or just avoid in long-only)

For our long-only crypto/commodity system:
  - Rank all 10 crypto assets + 5 commodity ETFs by 21-day return
  - Allocate to top 3-5 assets
  - Zero allocation to bottom 5
```

**Cross-sectional momentum parameters**:

| Parameter | Value | Notes |
|-----------|-------|-------|
| Lookback | 21 days (crypto), 63 days (commodities) | |
| Skip last 1-5 days | Yes (skip last 5 days for crypto) | Short-term reversal effect |
| Number of longs | Top 3-5 | Concentration vs diversification |
| Rebalance | Weekly (crypto), monthly (commodities) | |
| Volatility adjust | Yes (inverse vol weighting) | Equalizes risk contribution |

### 7.3 Momentum Crashes and How to Avoid Them

Momentum strategies suffer periodic severe drawdowns ("momentum crashes") when:
1. Markets reverse sharply from prolonged trends (2009 equity reversal, 2022 crypto reversal)
2. Volatility spikes (momentum is short gamma)
3. Liquidity dries up (momentum portfolios are typically illiquid)

**Crash protection methods**:

```python
def momentum_with_crash_protection(returns, lookback=21, vol_window=60):
    """
    Momentum with dynamic risk management to avoid crashes.
    """
    signal, weight = tsmom_signal(returns, lookback, vol_window)

    # 1. Volatility scaling (Daniel & Moskowitz 2016)
    #    Reduce position when vol is high
    current_vol = returns.rolling(vol_window).std().iloc[-1] * (252 ** 0.5)
    long_term_vol = returns.rolling(252).std().iloc[-1] * (252 ** 0.5)
    vol_ratio = current_vol / (long_term_vol + 1e-10)

    if vol_ratio > 1.5:
        weight *= 0.5  # Halve exposure in high-vol regimes

    # 2. Momentum reversal filter
    #    If short-term momentum opposes long-term momentum, reduce
    short_mom = returns.rolling(5).sum().iloc[-1]
    long_mom = returns.rolling(lookback).sum().iloc[-1]

    if np.sign(short_mom) != np.sign(long_mom):
        weight *= 0.5  # Conflicting signals = reduce

    # 3. Drawdown-based scaling
    #    If strategy is in drawdown > 10%, reduce by half
    cum_returns = (1 + returns * signal.shift(1)).cumprod()
    peak = cum_returns.cummax()
    drawdown = (cum_returns - peak) / peak

    if drawdown.iloc[-1] < -0.10:
        weight *= 0.5

    return signal, weight
```

### 7.4 Dual Momentum (Absolute + Relative)

**Antonacci (2012)**: Combine absolute momentum (TSMOM) with relative momentum (cross-sectional):

```
1. Absolute momentum filter:
   If asset return over 12 months > T-bill return => eligible
   If not => move to cash/bonds

2. Relative momentum:
   Among eligible assets, select the top performers

For crypto/commodities:
  - Absolute filter: 21-day return > 0 (simpler, no T-bill proxy needed)
  - Relative: among assets with positive 21d return, select top 3
  - Cash equivalent: hold USDX or stablecoins
```

```python
def dual_momentum(returns_dict, lookback=21, top_n=3):
    """
    Dual momentum: absolute + relative.

    Parameters:
        returns_dict: {symbol: pd.Series of returns}
        lookback: lookback period for momentum
        top_n: number of assets to hold

    Returns:
        selected: list of (symbol, weight) tuples
    """
    scores = {}

    for symbol, rets in returns_dict.items():
        cum_ret = (1 + rets).rolling(lookback).apply(lambda x: x.prod() - 1)
        latest = cum_ret.iloc[-1]

        # Absolute momentum filter
        if latest > 0:
            scores[symbol] = latest

    if not scores:
        return [("CASH", 1.0)]  # All assets negative => 100% cash

    # Relative momentum: rank and select top N
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    selected = ranked[:top_n]

    # Equal weight among selected
    weight = 1.0 / len(selected)
    return [(sym, weight) for sym, _ in selected]
```

### 7.5 Expected Performance

- **TSMOM Sharpe**: 0.8-1.2 across diversified futures (Moskowitz et al. 2012)
- **Crypto TSMOM**: 1.0-2.0 in early years, declining to 0.5-1.0 as market matures
- **Dual momentum Sharpe**: 0.6-1.0 (Antonacci 2012)
- **Cross-sectional crypto momentum**: 0.5-1.2 (Liu et al. 2022)
- **Key advantage**: Diversification across asset classes smooths returns
- **Maximum drawdown**: 15-30% (vs 50-80% for buy-and-hold crypto)

### 7.6 Key Pitfalls

1. **Lookback sensitivity**: Crypto momentum has shorter half-life than traditional assets.
   Use 7-30 days, not the standard 6-12 months from equity literature.
2. **Transaction costs**: Cross-sectional momentum requires frequent rebalancing. At 10 bps
   per trade for crypto, turnover > 200%/month destroys the edge.
3. **Crowding**: As more quant funds trade crypto momentum, the signal decays. Monitor IC
   over time and retire if IC < 0.02.
4. **Momentum in crypto is more fragile**: The 2022 bear market destroyed 18+ months of
   momentum profits in weeks. Always pair with absolute momentum filter.

### 7.7 Libraries

```
pip install numpy pandas
pip install scipy             # Ranking, optimization
```

### 7.8 Signal Framework Integration

The existing `MomentumStrategy` in `trading/strategy/momentum.py` implements basic cross-
sectional momentum. Enhancements should include:
- Volatility-adjusted weights (inverse vol)
- Absolute momentum filter (skip assets with negative lookback return)
- Crash protection (vol-scaled position sizing)

---

## 8. MEAN REVERSION WITH REGIME AWARENESS

### 8.1 Core Principle

Mean reversion works in ranging/sideways markets and fails in trending markets. Trend
following works in trending markets and fails in ranging ones. The solution: detect the
regime first, then apply the appropriate strategy.

```
if regime == SIDEWAYS:
    apply_mean_reversion()
elif regime in (BULL, BEAR):
    apply_trend_following()
else:
    reduce_exposure()  # Uncertain regime
```

### 8.2 Regime Detection Methods (Practical)

#### Method 1: ADX-Based (Simplest)

```python
def detect_regime_adx(prices, adx_period=14, threshold=25):
    """
    ADX > threshold => trending, ADX < threshold => ranging.
    """
    # Compute ADX (using ta library)
    import ta
    adx = ta.trend.ADXIndicator(prices['high'], prices['low'], prices['close'],
                                 window=adx_period)
    adx_value = adx.adx().iloc[-1]

    if adx_value > threshold:
        return 'trending'
    else:
        return 'ranging'
```

#### Method 2: Hurst Exponent

```python
def hurst_exponent(prices, max_lag=100):
    """
    Hurst exponent H:
      H > 0.5: trending (persistent)
      H = 0.5: random walk
      H < 0.5: mean-reverting (anti-persistent)
    """
    lags = range(2, max_lag)
    tau = [np.std(np.subtract(prices[lag:], prices[:-lag])) for lag in lags]

    log_lags = np.log(lags)
    log_tau = np.log(tau)

    # Linear regression of log(tau) on log(lag)
    H = np.polyfit(log_lags, log_tau, 1)[0]

    return H

# Interpretation:
# H < 0.40: Strong mean reversion => trade mean reversion
# 0.40 <= H <= 0.60: Random walk => reduce exposure
# H > 0.60: Strong trending => trade trend following
```

#### Method 3: HMM Regime (from Section 1)

Use the HMM regime detector and share its output with the mean reversion strategy:

```python
@register
class RegimeAwareMeanReversionStrategy(Strategy):
    name = "regime_mean_reversion"

    def generate_signals(self) -> list[Signal]:
        # Step 1: Get regime from HMM or ADX
        regime = self._detect_regime()

        if regime != 'ranging':
            return [Signal(
                strategy=self.name,
                symbol=self.symbol,
                action="hold",
                strength=0.0,
                reason=f"Regime is {regime}, mean reversion disabled",
                data={"regime": regime},
            )]

        # Step 2: Apply mean reversion logic only in ranging regime
        z_score = self._compute_zscore()

        if z_score < -2.0:
            action = "buy"
            strength = min(abs(z_score) / 4.0, 1.0)
        elif z_score > 2.0:
            action = "sell"
            strength = min(abs(z_score) / 4.0, 1.0)
        else:
            action = "hold"
            strength = 0.0

        return [Signal(
            strategy=self.name,
            symbol=self.symbol,
            action=action,
            strength=strength,
            reason=f"Regime: {regime}, Z-score: {z_score:.2f}",
            data={"regime": regime, "z_score": z_score},
        )]
```

### 8.3 Mean Reversion Indicators for Ranging Markets

| Indicator | Entry | Exit | Best for |
|-----------|-------|------|----------|
| Bollinger Bands | Touch lower/upper band | Return to mean | Crypto, GLD |
| RSI(14) | < 30 (buy) / > 70 (sell) | RSI crosses 50 | All assets |
| Z-score of price | < -2.0 (buy) / > 2.0 (sell) | Z within +/- 0.5 | Pairs trading |
| Stochastic(14,3,3) | < 20 (buy) / > 80 (sell) | Cross 50 | Commodity ETFs |
| CCI(20) | < -100 (buy) / > 100 (sell) | Cross 0 | All |

### 8.4 Expected Performance

- **Mean reversion + regime filter Sharpe**: 0.8-1.4 (vs 0.3-0.6 without regime filter)
- **Win rate improvement**: From 40-50% (blind mean reversion) to 55-65% (regime-aware)
- **Key advantage**: Eliminates the worst drawdowns from mean-reverting in trending markets
- **Crypto**: Mean reversion works well during BTC consolidation phases (30-50% of time)

### 8.5 Key Pitfalls

1. **Regime detection lag**: All regime filters have a delay. By the time you detect
   "trending," the trend may be 20-30% underway. Accept some false starts.
2. **Transition regimes**: The shift from ranging to trending is the danger zone. If
   mean-reversion entries coincide with trend start, losses are large. Use tight stops.
3. **Parameter alignment**: Ensure the regime detection lookback and the mean reversion
   lookback are on compatible timescales. Example: 60-day Hurst + 20-day Bollinger is
   a mismatch (regime window too long for the signal window).

### 8.6 Libraries

```
pip install ta                # ADX, Bollinger, RSI, Stochastic
pip install hmmlearn          # HMM regime detection
pip install numpy pandas
```

---

## 9. FACTOR-BASED CRYPTO INVESTING

### 9.1 Value Factor

#### NVT Ratio (Network Value to Transactions)

```
NVT = Market Cap / Daily Transaction Volume (USD)

Interpretation:
  NVT < 20:   Undervalued (transaction volume justifies market cap)
  NVT 20-65:  Fair value
  NVT > 65:   Overvalued (speculative premium)
  NVT > 100:  Bubble territory

Signal NVT (smoothed):
  NVT_signal = Market Cap / EMA(Daily TX Volume, 28 days)
```

**Data source**: Blockchain.com API (free for BTC), CoinMetrics community API

#### MVRV Ratio (Market Value to Realized Value)

```
MVRV = Market Cap / Realized Cap

Realized Cap = sum(each UTXO * price when it last moved)

Interpretation:
  MVRV < 1.0:  Below realized value => STRONG BUY (historically 100% hit rate)
  MVRV 1.0-2.0: Fair value range
  MVRV 2.0-3.5: Overvalued, begin taking profits
  MVRV > 3.5:   Extreme overvaluation => SELL
```

**MVRV historical signals for BTC**:

| MVRV Level | Date | BTC Price | Subsequent Action |
|------------|------|-----------|-------------------|
| 0.85 | Nov 2022 | $15,500 | Rallied 380% in 14 months |
| 0.75 | Mar 2020 | $5,000 | Rallied 1200% to ATH |
| 3.7 | Nov 2021 | $67,000 | Dropped 77% over 13 months |
| 3.9 | Apr 2021 | $63,000 | Dropped 55% in 2 months |

### 9.2 Momentum Factor

(See Section 7 for detailed momentum implementation)

For factor-based framework:

```
Momentum score = 0.5 * ret_7d_zscore + 0.3 * ret_21d_zscore + 0.2 * ret_63d_zscore

where:
  ret_Nd_zscore = (asset_ret_N - cross_sectional_mean_ret_N) / cross_sectional_std_ret_N
```

### 9.3 Carry Factor (Staking Yield)

```
Carry = Annual Staking Yield (net of inflation)

Net carry = Staking APR - Token Inflation Rate

Examples (approximate, 2025-2026):
  ETH:  3.5% staking - 0.5% inflation = 3.0% net carry
  SOL:  6.5% staking - 5.0% inflation = 1.5% net carry
  AVAX: 8.0% staking - 5.0% inflation = 3.0% net carry
  DOT:  14%  staking - 10%  inflation = 4.0% net carry

Signal: Long high net carry, underweight low/negative carry
```

### 9.4 Volatility Factor

```
Low-vol anomaly (adapted for crypto):
  vol_score = -1 * realized_vol_60d  (negative = low vol is better)

  Low-vol crypto assets have historically outperformed on a risk-adjusted basis,
  similar to the low-vol anomaly in equities (Baker, Bradley, Wurgler 2011).
```

### 9.5 Liquidity Factor

```
Liquidity score = log(Average Daily Volume USD, 30d)

Amihud illiquidity ratio:
  ILLIQ_i = (1/D) * sum(|r_{i,d}| / Volume_{i,d})  over D days

Signal: Avoid the most illiquid assets. Liquidity premium exists in crypto.
Assets in the bottom 20% of liquidity underperform by 2-5% annually.
```

### 9.6 Multi-Factor Portfolio Construction

```python
def crypto_factor_scores(assets_data):
    """
    Compute composite factor score for each crypto asset.

    Parameters:
        assets_data: dict of {symbol: {nvt, mvrv, ret_21d, carry, vol_60d, adv_30d}}

    Returns:
        dict of {symbol: composite_score}
    """
    factor_weights = {
        'value': 0.25,       # NVT + MVRV
        'momentum': 0.30,    # 21-day return
        'carry': 0.15,       # Net staking yield
        'volatility': 0.15,  # Low-vol preferred
        'liquidity': 0.15,   # Higher liquidity preferred
    }

    # Z-score each factor cross-sectionally
    factors = {}
    for factor_name in ['value', 'momentum', 'carry', 'volatility', 'liquidity']:
        raw_scores = {sym: data[factor_name] for sym, data in assets_data.items()}
        mean = np.mean(list(raw_scores.values()))
        std = np.std(list(raw_scores.values())) + 1e-10
        factors[factor_name] = {sym: (v - mean) / std for sym, v in raw_scores.items()}

    # Composite score
    composite = {}
    for sym in assets_data:
        score = sum(
            factor_weights[f] * factors[f][sym]
            for f in factor_weights
        )
        composite[sym] = score

    return composite
```

### 9.7 Expected Performance

- **Crypto value (NVT) Sharpe**: 0.4-0.8 (limited backtest history)
- **MVRV timing Sharpe**: 0.6-1.2 (works well at extremes, useless in middle)
- **Crypto momentum factor**: 0.5-1.2 (Liu, Tsyvinski, Wu 2022)
- **Multi-factor composite**: Expected Sharpe 0.6-1.0, lower drawdowns than single factors
- **Key advantage**: Diversification across factors reduces strategy-specific risk

### 9.8 Key Pitfalls

1. **Data quality**: On-chain metrics (NVT, MVRV) depend on accurate chain data. Free APIs
   have limited history and coverage.
2. **Factor crowding**: As crypto quant grows, factor premia compress. The value factor is
   least crowded; momentum is most crowded.
3. **Short history**: Most crypto factor research is based on 5-8 years of data. This is
   insufficient for statistically robust conclusions. Weight academic rigor accordingly.
4. **Staking yield changes**: Carry factor is unstable as protocol economics change (ETH
   merge in 2022 fundamentally changed ETH carry).

### 9.9 Libraries

```
pip install numpy pandas
pip install requests          # On-chain data APIs
```

---

## 10. OPTIMAL POSITION SIZING

### 10.1 Kelly Criterion

**Full Kelly**:

```
f* = (p * b - q) / b

where:
  f* = optimal fraction of capital to bet
  p  = probability of winning
  q  = 1 - p = probability of losing
  b  = win/loss ratio (average win / average loss)

For continuous returns (Thorp 2006):
  f* = (mu - r) / sigma^2

where:
  mu    = expected return of the strategy
  r     = risk-free rate
  sigma = standard deviation of returns
```

**Example for a crypto strategy**:
```
mu = 15% annualized expected return
r = 5% risk-free rate
sigma = 40% annualized volatility

f* = (0.15 - 0.05) / 0.16 = 0.625 (62.5% of capital)
```

### 10.2 Fractional Kelly

**NEVER use full Kelly in practice.** Full Kelly leads to catastrophic drawdowns because
parameter estimates are uncertain.

```
f_practical = fraction * f_kelly

Recommended fractions:
  Conservative: 0.25 * Kelly (quarter Kelly)
  Moderate:     0.50 * Kelly (half Kelly)  <= RECOMMENDED
  Aggressive:   0.75 * Kelly (three-quarter Kelly)
```

**Why half Kelly**: The Sharpe ratio of a half-Kelly strategy is 75% of full Kelly, but
the maximum drawdown is only ~25% of full Kelly. This is a dramatically better
risk/reward tradeoff.

```python
def fractional_kelly(expected_return, volatility, risk_free_rate=0.05, fraction=0.5):
    """
    Compute fractional Kelly bet size.

    Parameters:
        expected_return: annualized expected return
        volatility: annualized volatility
        risk_free_rate: risk-free rate
        fraction: Kelly fraction (0.25 to 0.75, default 0.5)

    Returns:
        f: fraction of capital to allocate
    """
    if volatility <= 0:
        return 0.0

    full_kelly = (expected_return - risk_free_rate) / (volatility ** 2)
    f = fraction * full_kelly

    # Cap at reasonable bounds
    f = max(0.0, min(f, 0.50))  # Never exceed 50% in one position

    return f
```

### 10.3 Risk Parity

Allocate capital such that each asset contributes equally to portfolio risk:

```
w_i * sigma_i * corr_contribution_i = 1/N * sigma_portfolio

Simplified (assuming uncorrelated assets):
  w_i = (1 / sigma_i) / sum(1 / sigma_j for all j)

where sigma_i = realized volatility of asset i
```

```python
def risk_parity_weights(volatilities):
    """
    Compute risk parity weights from asset volatilities.
    Assumes zero correlation (inverse-vol weighting).

    Parameters:
        volatilities: dict of {symbol: annualized_vol}

    Returns:
        dict of {symbol: weight}
    """
    inv_vols = {sym: 1.0 / (vol + 1e-10) for sym, vol in volatilities.items()}
    total_inv_vol = sum(inv_vols.values())
    weights = {sym: iv / total_inv_vol for sym, iv in inv_vols.items()}
    return weights
```

**Risk parity for crypto + commodities**:

| Asset | Typical Vol | Inv-Vol Weight | Risk Parity Weight |
|-------|------------|----------------|-------------------|
| BTC | 60% | 1.67 | ~6% |
| ETH | 75% | 1.33 | ~5% |
| SOL | 90% | 1.11 | ~4% |
| GLD (UGL 2x) | 30% | 3.33 | ~12% |
| SLV (AGQ 2x) | 45% | 2.22 | ~8% |
| USO | 35% | 2.86 | ~10% |

**Note**: Crypto gets low allocation due to high vol. This is by design -- risk parity says
each dollar of risk should be equal across assets.

### 10.4 Inverse Volatility Weighting

Same as simplified risk parity above. Practically identical for uncorrelated assets:

```
w_i = (1/sigma_i) / sum(1/sigma_j)
```

**Implementation note**: Use 60-day realized vol for crypto, 90-day for commodities.
Update weights weekly.

### 10.5 Maximum Diversification

Maximize the diversification ratio:

```
DR = (w' * sigma) / sqrt(w' * Sigma * w)

where:
  w = weight vector
  sigma = vector of individual volatilities
  Sigma = covariance matrix

Maximize DR subject to:
  sum(w) = 1
  w_i >= 0
```

```python
from scipy.optimize import minimize
import numpy as np

def max_diversification_weights(returns_df, min_weight=0.02, max_weight=0.30):
    """
    Compute maximum diversification portfolio weights.

    Parameters:
        returns_df: DataFrame of asset returns (each column = one asset)
        min_weight: minimum weight per asset
        max_weight: maximum weight per asset
    """
    cov = returns_df.cov().values * 252  # Annualize
    vols = np.sqrt(np.diag(cov))
    n = len(vols)

    # Negative diversification ratio (we minimize)
    def neg_div_ratio(w):
        port_vol = np.sqrt(w @ cov @ w)
        weighted_vol = w @ vols
        return -weighted_vol / (port_vol + 1e-10)

    constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]
    bounds = [(min_weight, max_weight)] * n
    x0 = np.ones(n) / n

    result = minimize(neg_div_ratio, x0, method='SLSQP',
                      bounds=bounds, constraints=constraints)

    weights = dict(zip(returns_df.columns, result.x))
    return weights
```

### 10.6 Position Sizing Integration with Signal Strength

In our system, each Signal has a `strength` field (0.0 to 1.0). Integrate with position sizing:

```python
def compute_position_size(signal, portfolio_value, method='fractional_kelly'):
    """
    Map signal strength to position size.

    Parameters:
        signal: Signal object with .strength and .symbol
        portfolio_value: current portfolio value
        method: 'fractional_kelly', 'risk_parity', or 'fixed_fraction'
    """
    base_allocation = {
        'fractional_kelly': fractional_kelly(
            expected_return=signal.strength * 0.30,  # Scale strength to return
            volatility=get_asset_vol(signal.symbol),
        ),
        'risk_parity': risk_parity_weight(signal.symbol),
        'fixed_fraction': 0.10,  # 10% per position
    }[method]

    # Scale by signal strength
    allocation = base_allocation * signal.strength

    # Apply portfolio-level caps
    allocation = min(allocation, RISK['max_position_pct'])

    dollar_amount = portfolio_value * allocation
    return dollar_amount
```

### 10.7 Expected Impact

- **Kelly vs equal weight**: Kelly improves geometric growth by 20-50% over equal weight
- **Half Kelly vs full Kelly**: Reduces max drawdown by ~75% with only 25% Sharpe reduction
- **Risk parity vs equal weight**: Improves Sharpe by 0.1-0.3 for mixed crypto/commodity
- **Max diversification**: Best Sharpe improvement (0.2-0.4) but requires accurate covariance

### 10.8 Key Pitfalls

1. **Parameter uncertainty**: Kelly criterion is extremely sensitive to estimated mu and sigma.
   A 1% error in expected return can change allocation by 10-20%. Always use fractional Kelly.
2. **Correlation instability**: Crypto correlations spike to 0.8-0.9 during crises, destroying
   diversification exactly when you need it most. Use stressed correlations for sizing.
3. **Rebalancing costs**: Frequent rebalancing to maintain risk parity targets incurs
   transaction costs. Rebalance only when weights drift > 25% from target.
4. **Leverage constraints**: Risk parity for diversified portfolios often implies leverage
   (since low-vol assets need high weight). Our system caps at 2x (leveraged ETFs).
5. **Max position cap**: Regardless of Kelly or any other method, never exceed 33% of
   portfolio in a single position (per `RISK['max_position_pct']` in config).

### 10.9 Libraries

```
pip install numpy pandas scipy  # Core optimization
```

---

## 11. STRATEGY INTEGRATION MATRIX

### 11.1 How Strategies Complement Each Other

| Strategy | Market Regime | Time Horizon | Correlation with Others |
|----------|--------------|--------------|------------------------|
| HMM Regime | All (meta-strategy) | Daily | Informs all others |
| Stat Arb / Pairs | Ranging | 5-30 days | Low corr with momentum |
| Trend Following | Trending | 7-90 days | Anti-correlated with mean reversion |
| ML Signals | All | 1-5 days | Low corr (depends on features) |
| Vol Regime | All (overlay) | Daily | Informs position sizing |
| Microstructure | All | Intraday-daily | Low corr with everything |
| Cross-Asset Mom | Trending | 7-63 days | Correlated with trend following |
| Regime Mean Rev | Ranging | 5-20 days | Anti-correlated with momentum |
| Factor Crypto | All | 30-90 days | Low corr with technical signals |
| Position Sizing | All (meta) | Continuous | N/A (affects all sizing) |

### 11.2 Recommended Implementation Priority

| Priority | Strategy | Complexity | Expected Sharpe | Dependencies |
|----------|----------|-----------|-----------------|--------------|
| 1 | HMM Regime Detection | Medium | 0.6-1.2 | hmmlearn |
| 2 | Regime-Aware Mean Reversion | Low | 0.8-1.4 | HMM output |
| 3 | Adaptive Trend Following (Kalman) | Medium | 0.8-1.5 | pykalman |
| 4 | Volatility Regime Overlay | Medium | +0.2 improvement | arch |
| 5 | Pairs Trading (BTC/ETH, GLD/SLV) | Medium | 0.8-1.5 | statsmodels |
| 6 | Cross-Asset Momentum Enhancement | Low | 0.5-1.2 | existing code |
| 7 | ML Signal Generation | High | 0.5-1.0 | sklearn, feature eng |
| 8 | Factor-Based Crypto | Medium | 0.6-1.0 | on-chain data |
| 9 | Optimal Position Sizing | Low | +0.1-0.3 improvement | scipy |
| 10 | Microstructure Signals | High | 0.3-0.8 | exchange APIs |

### 11.3 Signal Aggregation Considerations

All new strategies must output `Signal` objects compatible with `trading.strategy.base.Signal`
and register via `@register` decorator. The `trading.strategy.aggregator.aggregate_signals()`
handles conflict resolution when strategies disagree.

Key integration points:
- HMM regime output should be cached and shared across strategies (avoid redundant computation)
- Volatility regime should inform `strength` scaling (reduce strength in high-vol regimes)
- Position sizing should be applied after aggregation, not within individual strategies
- `MAX_CRYPTO_EXPOSURE_PCT = 0.70` and `MAX_SINGLE_ASSET_SIGNALS = 4` caps from the
  aggregator remain in effect

### 11.4 Required Dependencies (Full List)

```
# Already in requirements.txt:
numpy
pandas
scikit-learn
requests
rich

# New dependencies for advanced strategies:
hmmlearn>=0.3.0          # HMM regime detection
pykalman>=0.9.5          # Kalman filter trend following
arch>=6.0                # GARCH volatility modeling
statsmodels>=0.14.0      # Cointegration, ADF tests, OLS
scipy>=1.11.0            # Optimization (position sizing)
ta>=0.11.0               # Technical analysis indicators
xgboost>=2.0.0           # Gradient boosting (optional, better than sklearn)
shap>=0.43.0             # ML model explanation (optional)
ccxt>=4.0.0              # Exchange API for microstructure (optional)
```

---

## APPENDIX: ACADEMIC REFERENCES

| Paper | Year | Relevance |
|-------|------|-----------|
| Ang & Timmermann, "Regime Changes and Financial Markets" | 2012 | HMM theory for finance |
| Nystrup et al., "Dynamic Portfolio Optimization Across Hidden Market Regimes" | 2017 | HMM + portfolio management |
| Gatev, Goetzmann, Rouwenhorst, "Pairs Trading" | 2006 | Foundational pairs trading |
| Avellaneda & Lee, "Statistical Arbitrage in the US Equities Market" | 2010 | Stat arb framework |
| Moskowitz, Ooi, Pedersen, "Time Series Momentum" | 2012 | TSMOM across asset classes |
| Daniel & Moskowitz, "Momentum Crashes" | 2016 | Momentum crash protection |
| Antonacci, "Risk Premia Harvesting Through Dual Momentum" | 2012 | Dual momentum |
| Gu, Kelly, Xiu, "Empirical Asset Pricing via Machine Learning" | 2020 | ML for alpha |
| Liu, Tsyvinski, Wu, "Common Risk Factors in Cryptocurrency" | 2022 | Crypto factor model |
| Engle, "Risk and Volatility: Econometric Models and Financial Practice" | 2004 | GARCH Nobel lecture |
| Carr & Wu, "Variance Risk Premiums" | 2009 | VRP harvesting |
| Baker, Bradley, Wurgler, "Benchmarks as Limits to Arbitrage" | 2011 | Low-vol anomaly |
| Thorp, "The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market" | 2006 | Kelly criterion |
| Easley, Lopez de Prado, O'Hara, "Flow Toxicity and Liquidity" | 2012 | VPIN |
| Hurst, Ooi, Pedersen, "A Century of Evidence on Trend-Following" | 2017 | CTA trend following |
