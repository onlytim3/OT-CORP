"""Kalman Trend Strategy — adaptive trend following using a Kalman filter.

Uses a two-state Kalman filter (level + slope) on daily close prices to
estimate trend direction and strength. Signals fire when the slope
z-score exceeds a threshold, with hysteresis to avoid whipsaws.

v2: Fixed Q matrix (reduced level noise, kept trend noise low), added
hysteresis (enter at z>3, exit at z<1), removed vol scaling that was
suppressing best signals, added minimum holding period of 5 bars.
"""

import logging

import numpy as np
import pandas as pd

from trading.config import CRYPTO_SYMBOLS
from trading.data.crypto import get_ohlc
from trading.strategy.base import Signal, Strategy
from trading.strategy.indicators import atr
from trading.strategy.registry import register

log = logging.getLogger(__name__)

KALMAN_TREND = {
    "coins": ["bitcoin", "ethereum", "solana"],
    "lookback_days": 180,
    "observation_noise": 5.0,     # v2: increased from 1.0 — smoother filter
    "trend_noise": 0.001,         # v2: decreased from 0.01 — less responsive slope
    "entry_z_threshold": 3.0,     # v2: raised from 2.0 for higher conviction
    "exit_z_threshold": 1.0,      # v2: new — hysteresis exit
    "min_holding_bars": 5,        # v2: new — minimum holding period
    "min_data_points": 60,
}


def kalman_filter(
    prices: np.ndarray,
    obs_noise: float,
    trend_noise: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Run a two-state Kalman filter on a 1-D price series.

    State vector: [level, slope]
    """
    n = len(prices)
    levels = np.zeros(n)
    slopes = np.zeros(n)
    slope_vars = np.zeros(n)

    F = np.array([[1.0, 1.0],
                  [0.0, 1.0]])
    H = np.array([[1.0, 0.0]])

    # v2: Fixed Q matrix — level noise proportional to obs_noise but much smaller,
    # trend noise very small for smooth slope estimates
    Q = np.array([[obs_noise * 0.01, 0.0],   # v2: was 0.1, now 0.01
                  [0.0, trend_noise]])

    R = np.array([[obs_noise]])

    x = np.array([prices[0], 0.0])
    P = np.array([[obs_noise, 0.0],
                  [0.0, trend_noise * 10]])  # v2: wider initial slope uncertainty

    for t in range(n):
        x_pred = F @ x
        P_pred = F @ P @ F.T + Q

        y = prices[t] - H @ x_pred
        S = H @ P_pred @ H.T + R
        K = P_pred @ H.T @ np.linalg.inv(S)

        x = x_pred + (K @ y).flatten()
        P = (np.eye(2) - K @ H) @ P_pred

        levels[t] = x[0]
        slopes[t] = x[1]
        slope_vars[t] = P[1, 1]

    return levels, slopes, slope_vars


@register
class KalmanTrendStrategy(Strategy):
    """Adaptive trend following using a Kalman filter on daily prices.

    v2: Added hysteresis (enter z>3, exit z<1), fixed Q matrix,
    removed vol scaling, added minimum holding period.
    """

    name = "kalman_trend"

    def __init__(self):
        cfg = KALMAN_TREND
        self.coins = cfg["coins"]
        self.lookback_days = cfg["lookback_days"]
        self.obs_noise = cfg["observation_noise"]
        self.trend_noise = cfg["trend_noise"]
        self.entry_z = cfg["entry_z_threshold"]
        self.exit_z = cfg["exit_z_threshold"]
        self.min_holding = cfg["min_holding_bars"]
        self.min_data_points = cfg["min_data_points"]
        self._last_context: dict = {}
        # Track positions: {coin_id: {"direction": "long"/"short", "bars_held": int}}
        self._positions: dict[str, dict] = {}

    def generate_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        context_data: dict = {}

        for coin_id in self.coins:
            try:
                alpaca_symbol = CRYPTO_SYMBOLS.get(coin_id)
                if not alpaca_symbol:
                    continue

                ohlc = get_ohlc(coin_id, self.lookback_days)
                if ohlc.empty or len(ohlc) < self.min_data_points:
                    signals.append(Signal(
                        strategy=self.name, symbol=alpaca_symbol, action="hold",
                        strength=0.0,
                        reason=f"{coin_id} insufficient data",
                    ))
                    continue

                close = ohlc["close"].values.astype(float)
                levels, slopes, slope_vars = kalman_filter(close, self.obs_noise, self.trend_noise)

                # Standardize slope by its uncertainty
                final_slope = slopes[-1]
                final_slope_var = slope_vars[-1]
                if final_slope_var <= 0:
                    slope_z = 0.0
                else:
                    slope_z = final_slope / np.sqrt(final_slope_var)

                current_price = close[-1]

                # v2: Hysteresis-based signal logic
                pos = self._positions.get(coin_id)
                action, strength, reason = self._decide_action(
                    coin_id, slope_z, pos,
                )

                # Update position tracking
                if action == "buy":
                    self._positions[coin_id] = {"direction": "long", "bars_held": 0}
                elif action == "sell":
                    if pos and pos["direction"] == "long":
                        # Closing long position
                        self._positions.pop(coin_id, None)
                    else:
                        self._positions[coin_id] = {"direction": "short", "bars_held": 0}
                elif pos:
                    pos["bars_held"] = pos.get("bars_held", 0) + 1

                context_data[coin_id] = {
                    "price": round(float(current_price), 2),
                    "kalman_level": round(float(levels[-1]), 2),
                    "slope": round(float(final_slope), 6),
                    "slope_z": round(float(slope_z), 2),
                    "position": pos["direction"] if pos else "flat",
                    "action": action,
                }

                signals.append(Signal(
                    strategy=self.name,
                    symbol=alpaca_symbol,
                    action=action,
                    strength=round(max(strength, 0.0), 4),
                    reason=reason,
                    data={
                        "coin": coin_id,
                        "slope_z": round(float(slope_z), 2),
                    },
                ))

            except Exception as e:
                sym = CRYPTO_SYMBOLS.get(coin_id, "BTC/USD")
                log.warning("kalman_trend error for %s: %s", coin_id, e)
                signals.append(Signal(
                    strategy=self.name, symbol=sym, action="hold",
                    strength=0.0, reason=f"{coin_id} Kalman filter error: {e}",
                ))

        self._last_context = context_data
        return signals

    def _decide_action(self, coin_id: str, slope_z: float,
                       pos: dict | None) -> tuple[str, float, str]:
        """Decide action using hysteresis bands.

        Entry: |slope_z| > entry_z (3.0)
        Exit:  |slope_z| < exit_z (1.0) AND held >= min_holding bars
        """
        abs_z = abs(slope_z)

        if pos is None:
            # No position — look for entry
            if slope_z > self.entry_z:
                strength = min(slope_z / 5.0, 1.0)
                return ("buy", strength,
                        f"{coin_id} Kalman uptrend entry z={slope_z:.2f} > {self.entry_z}")
            elif slope_z < -self.entry_z:
                strength = min(abs_z / 5.0, 1.0)
                return ("sell", strength,
                        f"{coin_id} Kalman downtrend entry z={slope_z:.2f} < -{self.entry_z}")
            else:
                return ("hold", 0.0,
                        f"{coin_id} Kalman z={slope_z:.2f} — no entry signal")

        # Have a position — check for exit
        bars_held = pos.get("bars_held", 0)
        direction = pos["direction"]

        if bars_held < self.min_holding:
            # Minimum holding period not met
            return ("hold", 0.0,
                    f"{coin_id} holding {direction} ({bars_held}/{self.min_holding} bars)")

        if direction == "long":
            if slope_z < self.exit_z:
                # Trend weakened — exit long
                return ("sell", 0.5,
                        f"{coin_id} exit long: z={slope_z:.2f} < {self.exit_z}")
            else:
                return ("hold", 0.0,
                        f"{coin_id} holding long, z={slope_z:.2f} still above exit")
        else:  # short
            if slope_z > -self.exit_z:
                # Downtrend weakened — exit short (buy to cover)
                return ("buy", 0.5,
                        f"{coin_id} exit short: z={slope_z:.2f} > -{self.exit_z}")
            else:
                return ("hold", 0.0,
                        f"{coin_id} holding short, z={slope_z:.2f} still below exit")

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
