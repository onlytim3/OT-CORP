"""Pairs Trading Strategy — statistical arbitrage on cointegrated crypto pairs.

Uses a simplified Engle-Granger approach: regress log prices, compute spread,
test mean-reversion via half-life, and trade z-score extremes.
Lightweight — numpy only, no statsmodels dependency.
"""

import logging

import numpy as np
import pandas as pd

from trading.config import CRYPTO_SYMBOLS
from trading.data.crypto import get_ohlc
from trading.strategy.base import Signal, Strategy
from trading.strategy.indicators import z_score
from trading.strategy.registry import register

log = logging.getLogger(__name__)

PAIRS_TRADING = {
    "pairs": [("ethereum", "solana"), ("litecoin", "bitcoin-cash"), ("chainlink", "uniswap")],
    "lookback_days": 180,
    "z_entry": 2.0,
    "z_exit": 0.5,
    "z_stop": 4.0,
    "zscore_window": 30,
    "min_half_life": 5,
    "max_half_life": 60,
}


def _compute_half_life(spread: pd.Series) -> float:
    """Estimate mean-reversion half-life from AR(1) regression on the spread.

    Fits: spread_diff = alpha + beta * spread_lag + error
    Half-life = -ln(2) / ln(1 + beta)

    Returns inf if the spread is not mean-reverting.
    """
    spread_clean = spread.dropna()
    if len(spread_clean) < 10:
        return float("inf")

    lag = spread_clean.shift(1).dropna()
    diff = spread_clean.diff().dropna()

    # Align: both start from index 1 onward
    lag = lag.iloc[-len(diff):]

    if lag.std() == 0:
        return float("inf")

    # OLS: diff = alpha + beta * lag
    # Using np.polyfit (degree 1) for the lag->diff relationship
    beta, _alpha = np.polyfit(lag.values, diff.values, 1)

    # beta should be negative for mean-reversion
    if beta >= 0:
        return float("inf")

    # phi = 1 + beta (the AR(1) coefficient)
    phi = 1 + beta
    if phi <= 0 or phi >= 1:
        return float("inf")

    half_life = -np.log(2) / np.log(phi)
    return float(half_life)


def _analyze_pair(coin_y: str, coin_x: str, config: dict) -> dict:
    """Run cointegration analysis on a single pair.

    Returns a dict with spread z-score, half-life, and raw analysis data.
    Raises ValueError if data is insufficient.
    """
    days = config["lookback_days"]
    window = config["zscore_window"]

    ohlc_y = get_ohlc(coin_y, days)
    ohlc_x = get_ohlc(coin_x, days)

    if ohlc_y.empty or ohlc_x.empty:
        raise ValueError(f"Empty OHLC data for {coin_y} or {coin_x}")

    # Align on shared dates
    min_len = min(len(ohlc_y), len(ohlc_x))
    if min_len < window + 10:
        raise ValueError(f"Insufficient aligned data: {min_len} rows (need {window + 10})")

    close_y = ohlc_y["close"].iloc[-min_len:].reset_index(drop=True)
    close_x = ohlc_x["close"].iloc[-min_len:].reset_index(drop=True)

    log_y = np.log(close_y)
    log_x = np.log(close_x)

    # OLS regression: log_y = beta * log_x + alpha
    beta, alpha = np.polyfit(log_x.values, log_y.values, 1)

    # Spread (residuals)
    spread = log_y - (beta * log_x + alpha)

    # Half-life check
    half_life = _compute_half_life(spread)

    # Rolling z-score of the spread
    spread_z = z_score(spread, window)
    current_z = float(spread_z.iloc[-1]) if not pd.isna(spread_z.iloc[-1]) else 0.0

    return {
        "coin_y": coin_y,
        "coin_x": coin_x,
        "beta": round(float(beta), 4),
        "alpha": round(float(alpha), 4),
        "half_life": round(half_life, 1),
        "current_z": round(current_z, 3),
        "spread_std": round(float(spread.std()), 4),
        "price_y": round(float(close_y.iloc[-1]), 2),
        "price_x": round(float(close_x.iloc[-1]), 2),
    }


@register
class PairsTradingStrategy(Strategy):
    """Statistical arbitrage on cointegrated crypto pairs.

    Trades mean-reversion of the spread between paired assets using
    z-score thresholds. Only enters when half-life confirms the pair
    is genuinely mean-reverting.
    """

    name = "pairs_trading"

    def __init__(self):
        self.config = PAIRS_TRADING
        self._last_context = {}

    def generate_signals(self) -> list[Signal]:
        signals = []
        context_data = {}

        for coin_y, coin_x in self.config["pairs"]:
            sym_y = CRYPTO_SYMBOLS.get(coin_y)
            sym_x = CRYPTO_SYMBOLS.get(coin_x)

            if not sym_y or not sym_x:
                log.warning("Pairs trading: missing symbol mapping for %s or %s", coin_y, coin_x)
                continue

            pair_label = f"{coin_y}/{coin_x}"

            try:
                analysis = _analyze_pair(coin_y, coin_x, self.config)
                context_data[pair_label] = analysis

                half_life = analysis["half_life"]
                current_z = analysis["current_z"]
                abs_z = abs(current_z)

                # Validate mean-reversion via half-life
                if not (self.config["min_half_life"] <= half_life <= self.config["max_half_life"]):
                    reason = (
                        f"{pair_label} half-life {half_life:.0f}d outside "
                        f"[{self.config['min_half_life']}, {self.config['max_half_life']}] — "
                        f"skipping (z={current_z:.2f})"
                    )
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_y, action="hold",
                        strength=0.0, reason=reason,
                        data={"pair": pair_label, "half_life": half_life, "z_score": current_z},
                    ))
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_x, action="hold",
                        strength=0.0, reason=reason,
                        data={"pair": pair_label, "half_life": half_life, "z_score": current_z},
                    ))
                    continue

                strength = min(abs_z / 4.0, 1.0)

                if abs_z > self.config["z_stop"]:
                    # Cointegration breakdown — do not trade
                    reason = f"{pair_label} z={current_z:.2f} exceeds stop ({self.config['z_stop']}) — cointegration breakdown"
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_y, action="hold",
                        strength=0.0, reason=reason,
                        data={"pair": pair_label, "z_score": current_z},
                    ))
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_x, action="hold",
                        strength=0.0, reason=reason,
                        data={"pair": pair_label, "z_score": current_z},
                    ))

                elif current_z > self.config["z_entry"]:
                    # Spread is high: short the spread (sell Y, buy X)
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_y, action="sell",
                        strength=strength,
                        reason=f"{pair_label} z={current_z:.2f} > {self.config['z_entry']} — short spread (sell {coin_y})",
                        data={"pair": pair_label, "z_score": current_z, "side": "short_spread", "half_life": half_life},
                    ))
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_x, action="buy",
                        strength=strength,
                        reason=f"{pair_label} z={current_z:.2f} > {self.config['z_entry']} — short spread (buy {coin_x})",
                        data={"pair": pair_label, "z_score": current_z, "side": "short_spread", "half_life": half_life},
                    ))

                elif current_z < -self.config["z_entry"]:
                    # Spread is low: long the spread (buy Y, sell X)
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_y, action="buy",
                        strength=strength,
                        reason=f"{pair_label} z={current_z:.2f} < -{self.config['z_entry']} — long spread (buy {coin_y})",
                        data={"pair": pair_label, "z_score": current_z, "side": "long_spread", "half_life": half_life},
                    ))
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_x, action="sell",
                        strength=strength,
                        reason=f"{pair_label} z={current_z:.2f} < -{self.config['z_entry']} — long spread (sell {coin_x})",
                        data={"pair": pair_label, "z_score": current_z, "side": "long_spread", "half_life": half_life},
                    ))

                elif abs_z < self.config["z_exit"]:
                    # Near equilibrium — exit / hold
                    reason = f"{pair_label} z={current_z:.2f} within exit band — hold"
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_y, action="hold",
                        strength=0.0, reason=reason,
                        data={"pair": pair_label, "z_score": current_z},
                    ))
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_x, action="hold",
                        strength=0.0, reason=reason,
                        data={"pair": pair_label, "z_score": current_z},
                    ))

                else:
                    # Between exit and entry — no action
                    reason = f"{pair_label} z={current_z:.2f} between thresholds — hold"
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_y, action="hold",
                        strength=0.0, reason=reason,
                        data={"pair": pair_label, "z_score": current_z},
                    ))
                    signals.append(Signal(
                        strategy=self.name, symbol=sym_x, action="hold",
                        strength=0.0, reason=reason,
                        data={"pair": pair_label, "z_score": current_z},
                    ))

            except Exception as e:
                log.error("Pairs trading error for %s: %s", pair_label, e)
                reason = f"{pair_label} analysis failed: {e}"
                signals.append(Signal(
                    strategy=self.name, symbol=sym_y, action="hold",
                    strength=0.0, reason=reason,
                ))
                signals.append(Signal(
                    strategy=self.name, symbol=sym_x, action="hold",
                    strength=0.0, reason=reason,
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "pairs": self._last_context}
