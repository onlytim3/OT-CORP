"""Shared technical indicators for strategy calculations.

All functions operate on pandas Series and return pandas Series.
Used by RSI Divergence, EMA Crossover, Bollinger Squeeze, and ratio strategies.
"""

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (0-100). Handles division by zero gracefully."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    # Avoid division by zero: when avg_loss is 0, RSI is 100
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    # Fill NaN where avg_loss was 0: use 100 (pure uptrend) or 50 (no movement)
    fill = pd.Series(np.where(avg_gain > 0, 100.0, 50.0), index=result.index)
    result = result.fillna(fill)
    return result


def bollinger_bands(series: pd.Series, period: int = 20, num_std: float = 2.0):
    """Bollinger Bands.

    Returns (upper, middle, lower, bandwidth) as four pd.Series.
    Bandwidth is (upper - lower) / middle — useful for squeeze detection.
    """
    middle = sma(series, period)
    std = series.rolling(window=period).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    bandwidth = (upper - lower) / middle
    return upper, middle, lower, bandwidth


def z_score(series: pd.Series, period: int) -> pd.Series:
    """Rolling z-score over a given lookback period. Handles zero std."""
    mean = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    # Avoid division by zero when std is 0 (flat price)
    return (series - mean) / std.replace(0, np.nan)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range."""
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(span=period, adjust=False).mean()


def detect_divergence(price: pd.Series, indicator: pd.Series, lookback: int = 14) -> str | None:
    """Detect bullish or bearish divergence between price and an oscillator.

    Bullish divergence: price makes a lower low, but indicator makes a higher low.
    Bearish divergence: price makes a higher high, but indicator makes a lower high.

    Returns 'bullish', 'bearish', or None.
    """
    if len(price) < lookback or len(indicator) < lookback:
        return None

    recent = price.iloc[-lookback:]
    recent_ind = indicator.iloc[-lookback:]

    half = lookback // 2
    if half < 2:
        return None

    # Split into first-half and second-half windows
    first_price = recent.iloc[:half]
    second_price = recent.iloc[half:]
    first_ind = recent_ind.iloc[:half]
    second_ind = recent_ind.iloc[half:]

    # Bullish: second-half price low < first-half price low,
    #          but second-half indicator low > first-half indicator low
    if (second_price.min() < first_price.min() and
            second_ind.min() > first_ind.min()):
        return "bullish"

    # Bearish: second-half price high > first-half price high,
    #          but second-half indicator high < first-half indicator high
    if (second_price.max() > first_price.max() and
            second_ind.max() < first_ind.max()):
        return "bearish"

    return None
