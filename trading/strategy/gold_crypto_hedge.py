"""Gold-Crypto Hedge Strategy — exploits correlation and decorrelation between gold and Bitcoin.

Both XAU and BTC are tradeable as AsterDex perpetual futures. The strategy detects
regime shifts in the gold-BTC relationship and generates signals based on:

1. Correlation regime changes (risk-off vs. inflation hedge)
2. Gold/BTC ratio z-score mean reversion (overextension detection)
3. Relative performance divergence (contrarian entries)

Core thesis: gold and BTC alternate between positive correlation (both acting as
inflation hedges) and negative correlation (flight-to-safety favours gold over BTC).
Regime transitions are tradeable.
"""

import logging

import numpy as np
import pandas as pd

from trading.config import ASTER_SYMBOLS
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

GOLD_CRYPTO_HEDGE = {
    "correlation_window": 30,           # days for rolling correlation
    "ratio_zscore_window": 30,          # window for gold/BTC ratio z-score
    "z_entry": 1.5,                     # enter mean-reversion when ratio z exceeds this
    "z_strong": 2.5,                    # elevated z — higher conviction signal
    "correlation_regime_threshold": 0.0, # below this = decorrelation regime
    "outperformance_pct": 0.15,         # 15% relative outperformance triggers signal
    "min_data_points": 20,              # minimum usable rows after returns
    "data_limit": 60,                   # kline rows to fetch
}

# Symbols
_GOLD_ASTER = ASTER_SYMBOLS.get("gold", "XAUUSDT")
_BTC_ASTER = ASTER_SYMBOLS.get("bitcoin", "BTCUSDT")
_SILVER_ASTER = ASTER_SYMBOLS.get("silver", "XAGUSDT")

_GOLD_SIGNAL = ASTER_SYMBOLS.get("gold", "XAU/USD")
_BTC_SIGNAL = ASTER_SYMBOLS.get("bitcoin", "BTC/USD")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_daily_closes(symbol: str, limit: int) -> pd.Series | None:
    """Fetch daily close prices from AsterDex klines. Returns None on failure."""
    try:
        from trading.execution.aster_client import get_aster_klines
    except ImportError:
        log.error("Cannot import get_aster_klines — aster_client unavailable")
        return None

    try:
        df = get_aster_klines(symbol, interval="1d", limit=limit)
    except Exception as e:
        log.warning("Failed to fetch klines for %s: %s", symbol, e)
        return None

    if df.empty or len(df) < 2:
        log.warning("Insufficient kline data for %s (%d rows)", symbol, len(df))
        return None

    return df["close"]


def _rolling_correlation(returns_a: pd.Series, returns_b: pd.Series,
                         window: int) -> pd.Series:
    """Compute rolling Pearson correlation between two return series."""
    return returns_a.rolling(window=window).corr(returns_b)


def _ratio_zscore(ratio: pd.Series, window: int) -> pd.Series:
    """Compute rolling z-score of a ratio series."""
    rolling_mean = ratio.rolling(window=window).mean()
    rolling_std = ratio.rolling(window=window).std()
    # Guard against zero std
    rolling_std = rolling_std.replace(0, np.nan)
    return (ratio - rolling_mean) / rolling_std


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

@register
class GoldCryptoHedgeStrategy(Strategy):
    """Hedge strategy trading gold vs. BTC based on correlation regime changes.

    Regimes detected:
      - Decorrelation (corr < 0): risk-off → sell BTC, buy gold
      - Co-movement (corr > 0, both rising): inflation hedge → buy both
      - BTC overextension (ratio z < -z_entry): BTC outperforming → reduce BTC, add gold
      - Gold overextension (ratio z > z_entry): fear trade → contrarian buy BTC
    """

    name = "gold_crypto_hedge"

    def __init__(self):
        self._config = GOLD_CRYPTO_HEDGE
        self._last_context: dict = {}

    def generate_signals(self) -> list[Signal]:
        cfg = self._config

        # ------------------------------------------------------------------
        # 1. Fetch data
        # ------------------------------------------------------------------
        gold_closes = _fetch_daily_closes(_GOLD_ASTER, cfg["data_limit"])
        btc_closes = _fetch_daily_closes(_BTC_ASTER, cfg["data_limit"])

        if gold_closes is None or btc_closes is None:
            reason = "Gold-Crypto Hedge: unable to fetch kline data for gold or BTC"
            log.warning(reason)
            return [Signal(
                strategy=self.name, symbol=_BTC_SIGNAL,
                action="hold", strength=0.0, reason=reason,
            )]

        # Align lengths
        min_len = min(len(gold_closes), len(btc_closes))
        gold_closes = gold_closes.iloc[-min_len:].reset_index(drop=True)
        btc_closes = btc_closes.iloc[-min_len:].reset_index(drop=True)

        # ------------------------------------------------------------------
        # 2. Compute returns
        # ------------------------------------------------------------------
        gold_returns = gold_closes.pct_change().dropna()
        btc_returns = btc_closes.pct_change().dropna()

        if len(gold_returns) < cfg["min_data_points"]:
            reason = (
                f"Gold-Crypto Hedge: insufficient data after returns "
                f"({len(gold_returns)} < {cfg['min_data_points']})"
            )
            return [Signal(
                strategy=self.name, symbol=_BTC_SIGNAL,
                action="hold", strength=0.0, reason=reason,
            )]

        # Align return series (should already be aligned, but be safe)
        align_len = min(len(gold_returns), len(btc_returns))
        gold_returns = gold_returns.iloc[-align_len:].reset_index(drop=True)
        btc_returns = btc_returns.iloc[-align_len:].reset_index(drop=True)

        # ------------------------------------------------------------------
        # 3. Rolling correlation
        # ------------------------------------------------------------------
        corr_window = cfg["correlation_window"]
        rolling_corr = _rolling_correlation(gold_returns, btc_returns, corr_window)
        current_corr = float(rolling_corr.iloc[-1]) if not pd.isna(rolling_corr.iloc[-1]) else 0.0

        # ------------------------------------------------------------------
        # 4. Gold/BTC price ratio and z-score
        # ------------------------------------------------------------------
        # Use aligned close prices (skip first row to match returns length)
        gold_aligned = gold_closes.iloc[-align_len:].reset_index(drop=True)
        btc_aligned = btc_closes.iloc[-align_len:].reset_index(drop=True)
        ratio = gold_aligned / btc_aligned
        ratio_z = _ratio_zscore(ratio, cfg["ratio_zscore_window"])
        current_ratio_z = float(ratio_z.iloc[-1]) if not pd.isna(ratio_z.iloc[-1]) else 0.0
        current_ratio = float(ratio.iloc[-1])

        # Recent returns (last N days where N = correlation_window)
        lookback = min(corr_window, len(gold_returns))
        gold_period_return = float(
            (gold_aligned.iloc[-1] / gold_aligned.iloc[-lookback] - 1)
        ) if lookback > 1 else 0.0
        btc_period_return = float(
            (btc_aligned.iloc[-1] / btc_aligned.iloc[-lookback] - 1)
        ) if lookback > 1 else 0.0

        # ------------------------------------------------------------------
        # 5. Build shared signal data
        # ------------------------------------------------------------------
        signal_data = {
            "correlation": round(current_corr, 4),
            "ratio": round(current_ratio, 6),
            "ratio_zscore": round(current_ratio_z, 3),
            "gold_return": round(gold_period_return, 4),
            "btc_return": round(btc_period_return, 4),
            "correlation_window": corr_window,
        }

        self._last_context = {
            **signal_data,
            "gold_price": round(float(gold_aligned.iloc[-1]), 2),
            "btc_price": round(float(btc_aligned.iloc[-1]), 2),
            "data_points": align_len,
        }

        # ------------------------------------------------------------------
        # 6. Generate signals based on regime detection
        # ------------------------------------------------------------------
        signals: list[Signal] = []
        threshold = cfg["correlation_regime_threshold"]
        z_entry = cfg["z_entry"]
        z_strong = cfg["z_strong"]

        # --- Regime A: Ratio z-score mean reversion (highest priority) ---
        abs_z = abs(current_ratio_z)

        if abs_z >= z_entry:
            strength = min(abs_z / (z_strong + 0.5), 0.95)
            strength = round(max(strength, 0.3), 2)

            if current_ratio_z > z_entry:
                # Gold/BTC ratio expanded: gold outperforming (fear trade)
                # Contrarian: buy BTC, reduce gold
                signals.append(Signal(
                    strategy=self.name, symbol=_BTC_SIGNAL,
                    action="buy", strength=strength,
                    reason=(
                        f"Gold/BTC ratio z={current_ratio_z:.2f} > {z_entry} — "
                        f"gold fear trade overextended, contrarian buy BTC"
                    ),
                    data={**signal_data, "regime": "gold_overextension"},
                ))
                signals.append(Signal(
                    strategy=self.name, symbol=_GOLD_SIGNAL,
                    action="sell", strength=round(strength * 0.7, 2),
                    reason=(
                        f"Gold/BTC ratio z={current_ratio_z:.2f} > {z_entry} — "
                        f"gold overextended vs BTC, reduce gold"
                    ),
                    data={**signal_data, "regime": "gold_overextension"},
                ))

            else:
                # Gold/BTC ratio contracted: BTC outperforming
                # Reduce BTC, add gold (mean reversion)
                signals.append(Signal(
                    strategy=self.name, symbol=_GOLD_SIGNAL,
                    action="buy", strength=strength,
                    reason=(
                        f"Gold/BTC ratio z={current_ratio_z:.2f} < -{z_entry} — "
                        f"BTC overextended vs gold, add gold hedge"
                    ),
                    data={**signal_data, "regime": "btc_overextension"},
                ))
                signals.append(Signal(
                    strategy=self.name, symbol=_BTC_SIGNAL,
                    action="sell", strength=round(strength * 0.7, 2),
                    reason=(
                        f"Gold/BTC ratio z={current_ratio_z:.2f} < -{z_entry} — "
                        f"BTC overextended vs gold, reduce BTC"
                    ),
                    data={**signal_data, "regime": "btc_overextension"},
                ))

            return signals

        # --- Regime B: Correlation regime change ---
        if current_corr < threshold:
            # Decorrelation / negative correlation: risk-off environment
            # Gold is the safe haven; BTC sells off
            corr_strength = min(abs(current_corr) * 1.5, 0.8)
            corr_strength = round(max(corr_strength, 0.25), 2)

            if gold_period_return > 0 and btc_period_return < 0:
                # Classic risk-off: gold up, BTC down — strong signal
                signals.append(Signal(
                    strategy=self.name, symbol=_GOLD_SIGNAL,
                    action="buy", strength=corr_strength,
                    reason=(
                        f"Decorrelation regime (corr={current_corr:.2f}): "
                        f"gold +{gold_period_return:.1%}, BTC {btc_period_return:.1%} — buy gold"
                    ),
                    data={**signal_data, "regime": "risk_off"},
                ))
                signals.append(Signal(
                    strategy=self.name, symbol=_BTC_SIGNAL,
                    action="sell", strength=round(corr_strength * 0.8, 2),
                    reason=(
                        f"Decorrelation regime (corr={current_corr:.2f}): "
                        f"risk-off flight to gold — sell BTC"
                    ),
                    data={**signal_data, "regime": "risk_off"},
                ))
            else:
                # Decorrelated but no clear risk-off pattern — mild signal
                signals.append(Signal(
                    strategy=self.name, symbol=_GOLD_SIGNAL,
                    action="buy", strength=round(corr_strength * 0.5, 2),
                    reason=(
                        f"Decorrelation regime (corr={current_corr:.2f}): "
                        f"gold/BTC diverging — mild gold preference"
                    ),
                    data={**signal_data, "regime": "decorrelation"},
                ))
                signals.append(Signal(
                    strategy=self.name, symbol=_BTC_SIGNAL,
                    action="hold", strength=0.0,
                    reason=(
                        f"Decorrelation regime (corr={current_corr:.2f}): "
                        f"no clear risk-off pattern — hold BTC"
                    ),
                    data={**signal_data, "regime": "decorrelation"},
                ))

            return signals

        # --- Regime C: Positive correlation, both rising — inflation hedge ---
        if current_corr > threshold and gold_period_return > 0 and btc_period_return > 0:
            # Both assets rising together = inflation/debasement hedge regime
            co_strength = min(current_corr * 0.8, 0.7)
            co_strength = round(max(co_strength, 0.2), 2)

            signals.append(Signal(
                strategy=self.name, symbol=_GOLD_SIGNAL,
                action="buy", strength=co_strength,
                reason=(
                    f"Inflation hedge regime (corr={current_corr:.2f}): "
                    f"gold +{gold_period_return:.1%}, BTC +{btc_period_return:.1%} — buy both"
                ),
                data={**signal_data, "regime": "inflation_hedge"},
            ))
            signals.append(Signal(
                strategy=self.name, symbol=_BTC_SIGNAL,
                action="buy", strength=co_strength,
                reason=(
                    f"Inflation hedge regime (corr={current_corr:.2f}): "
                    f"gold and BTC co-moving higher — buy both"
                ),
                data={**signal_data, "regime": "inflation_hedge"},
            ))

            return signals

        # --- Default: no clear regime — hold ---
        signals.append(Signal(
            strategy=self.name, symbol=_GOLD_SIGNAL,
            action="hold", strength=0.0,
            reason=(
                f"Gold-Crypto Hedge: no actionable regime "
                f"(corr={current_corr:.2f}, ratio_z={current_ratio_z:.2f})"
            ),
            data={**signal_data, "regime": "neutral"},
        ))
        signals.append(Signal(
            strategy=self.name, symbol=_BTC_SIGNAL,
            action="hold", strength=0.0,
            reason=(
                f"Gold-Crypto Hedge: no actionable regime "
                f"(corr={current_corr:.2f}, ratio_z={current_ratio_z:.2f})"
            ),
            data={**signal_data, "regime": "neutral"},
        ))

        return signals

    def get_market_context(self) -> dict:
        return {
            "strategy": self.name,
            **self._last_context,
        }
