"""GARCH Volatility Regime Strategy — vol-aware trend following.

Uses GARCH(1,1) conditional volatility to identify vol regimes, then
combines with simple momentum for directional signals. Low vol environments
favor trend-following entries; high vol environments favor risk-off.

v3: Loosened trend confirmation — uses 20-day momentum instead of EMA
crossover (which was too slow). Widened vol thresholds to NORMAL band
(1.2/0.8 instead of 2.0/0.5). Added momentum strength scaling.
"""

import logging

import numpy as np

from trading.config import BYBIT_SYMBOLS
from trading.data.crypto import get_ohlc
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

GARCH_VOLATILITY = {
    "coins": ["bitcoin", "ethereum", "solana"],
    "lookback_days": 365,
    "omega": 0.00001,
    "alpha": 0.10,
    "beta": 0.85,
    "high_vol_threshold": 1.3,    # v3: lowered from 2.0
    "low_vol_threshold": 0.8,     # v3: raised from 0.5
    "momentum_period": 20,        # v3: 20-day momentum instead of EMA crossover
    "min_data_points": 100,
}


def _fit_garch(returns, omega=0.00001, alpha=0.10, beta=0.85, n_iter=100):
    """Simple GARCH(1,1) conditional volatility estimation."""
    T = len(returns)
    sigma2 = np.zeros(T)
    sigma2[0] = np.var(returns)

    for t in range(1, T):
        sigma2[t] = omega + alpha * returns[t - 1] ** 2 + beta * sigma2[t - 1]

    return np.sqrt(sigma2)


@register
class GARCHVolatilityStrategy(Strategy):
    """Trade based on GARCH vol regimes + momentum direction.

    v3: Low vol + positive momentum → buy. High vol + negative momentum → sell.
    Normal vol → hold. Uses wider vol bands and simpler momentum check.
    """

    name = "garch_volatility"

    def __init__(self):
        cfg = GARCH_VOLATILITY
        self.coins = cfg["coins"]
        self.lookback_days = cfg["lookback_days"]
        self.high_vol_thresh = cfg["high_vol_threshold"]
        self.low_vol_thresh = cfg["low_vol_threshold"]
        self.mom_period = cfg["momentum_period"]
        self.min_data_points = cfg["min_data_points"]
        self._last_context = {}

    def generate_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        context_data: dict = {}

        for coin_id in self.coins:
            try:
                bybit_symbol = BYBIT_SYMBOLS.get(coin_id)
                if not bybit_symbol:
                    continue

                ohlc = get_ohlc(coin_id, self.lookback_days)
                if ohlc.empty or len(ohlc) < self.min_data_points:
                    row_count = len(ohlc) if not ohlc.empty else 0
                    signals.append(Signal(
                        strategy=self.name, symbol=bybit_symbol,
                        action="hold", strength=0.0,
                        reason=f"{coin_id} insufficient data ({row_count} rows)",
                    ))
                    continue

                close = ohlc["close"]
                prices = close.values.astype(float)
                price = float(prices[-1])

                # --- GARCH vol regime ---
                returns = np.diff(prices) / prices[:-1]
                if len(returns) < self.min_data_points:
                    signals.append(Signal(
                        strategy=self.name, symbol=bybit_symbol,
                        action="hold", strength=0.0,
                        reason=f"{coin_id} insufficient returns data",
                    ))
                    continue

                garch_vol = _fit_garch(
                    returns,
                    omega=GARCH_VOLATILITY["omega"],
                    alpha=GARCH_VOLATILITY["alpha"],
                    beta=GARCH_VOLATILITY["beta"],
                )
                current_garch_vol = float(garch_vol[-1]) * np.sqrt(365)

                window = min(252, len(returns))
                long_term_vol = float(np.std(returns[-window:])) * np.sqrt(365)

                if long_term_vol <= 0:
                    signals.append(Signal(
                        strategy=self.name, symbol=bybit_symbol,
                        action="hold", strength=0.0,
                        reason=f"{coin_id} zero long-term vol",
                    ))
                    continue

                # Safe division — zero check is above
                vol_ratio = current_garch_vol / long_term_vol

                # Vol regime classification
                if vol_ratio > self.high_vol_thresh:
                    vol_regime = "HIGH"
                elif vol_ratio < self.low_vol_thresh:
                    vol_regime = "LOW"
                else:
                    vol_regime = "NORMAL"

                # --- v3: Simple momentum check ---
                if len(prices) > self.mom_period:
                    momentum = (prices[-1] / prices[-self.mom_period] - 1.0)
                else:
                    momentum = 0.0

                # --- Combined signal ---
                action = "hold"
                strength = 0.0

                if vol_regime == "LOW" and momentum > 0.02:
                    # Low vol + positive momentum → buy
                    action = "buy"
                    mom_strength = min(abs(momentum) / 0.15, 1.0)
                    vol_strength = (self.low_vol_thresh - vol_ratio) / self.low_vol_thresh
                    strength = min(0.3 + 0.4 * mom_strength + 0.3 * max(vol_strength, 0), 1.0)
                elif vol_regime == "LOW" and momentum < -0.02:
                    # Low vol + negative momentum → could be start of trend down
                    action = "sell"
                    mom_strength = min(abs(momentum) / 0.15, 1.0)
                    strength = min(0.3 + 0.4 * mom_strength, 0.8)
                elif vol_regime == "HIGH" and momentum < -0.02:
                    # High vol + negative momentum → sell (risk-off)
                    action = "sell"
                    mom_strength = min(abs(momentum) / 0.15, 1.0)
                    vol_strength = (vol_ratio - self.high_vol_thresh) / self.high_vol_thresh
                    strength = min(0.4 + 0.3 * mom_strength + 0.3 * max(vol_strength, 0), 1.0)
                elif vol_regime == "HIGH" and momentum > 0.05:
                    # High vol + strong positive momentum → contrarian buy
                    action = "buy"
                    strength = min(0.3 + abs(momentum) / 0.2, 0.7)  # Cap lower for risk

                context_data[coin_id] = {
                    "price": round(price, 2),
                    "vol_ratio": round(vol_ratio, 3),
                    "garch_regime": vol_regime,
                    "momentum_20d": round(momentum, 4),
                    "current_garch_vol": round(current_garch_vol, 4),
                    "long_term_vol": round(long_term_vol, 4),
                }

                reason = (
                    f"{coin_id} GARCH {vol_regime} (ratio {vol_ratio:.2f}), "
                    f"20d mom {momentum:+.2%}"
                )

                signals.append(Signal(
                    strategy=self.name, symbol=bybit_symbol,
                    action=action, strength=round(strength, 3),
                    reason=reason,
                    data={
                        "coin": coin_id,
                        "vol_ratio": round(vol_ratio, 3),
                        "garch_regime": vol_regime,
                        "momentum": round(momentum, 4),
                    },
                ))

            except Exception as e:
                sym = BYBIT_SYMBOLS.get(coin_id, "BTC/USD")
                signals.append(Signal(
                    strategy=self.name, symbol=sym,
                    action="hold", strength=0.0,
                    reason=f"{coin_id} GARCH error: {e}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
