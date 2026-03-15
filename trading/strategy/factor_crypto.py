"""Multi-Factor Crypto Strategy — cross-sectional factor scoring across coins.

Ranks coins by a composite of momentum and inverse-volatility factors,
z-scored cross-sectionally. Buys the top-ranked coins, sells when a coin
drops out of top-N or falls below sell threshold.

v3: Added rebalance cooldown (only trade every 7 bars), minimum score
change threshold to avoid churning, and momentum confirmation (only buy
if 7-day momentum is positive).
"""

import logging

import numpy as np
import pandas as pd

from trading.config import CRYPTO_SYMBOLS
from trading.data.crypto import get_historical_prices, get_ohlc
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

FACTOR_CRYPTO = {
    "coins": [
        "bitcoin", "ethereum", "solana", "chainlink",
        "avalanche-2", "uniswap", "aave", "litecoin",
    ],
    "lookback_days": 90,
    "momentum_weight": 0.55,
    "volatility_weight": 0.30,
    "volume_weight": 0.15,
    "top_n_buy": 2,
    "sell_threshold": -0.3,
    "min_data_points": 30,
    "rebalance_cooldown": 7,      # v3: only rebalance every 7 bars
    "min_score_change": 0.3,      # v3: minimum composite score change to trigger trade
}

_MIN_COINS_FOR_ZSCORE = 3


def _cross_sectional_zscore(values: dict[str, float]) -> dict[str, float]:
    """Z-score a dict of {coin_id: raw_value} across all coins."""
    arr = np.array(list(values.values()))
    mean = arr.mean()
    std = arr.std(ddof=0)
    if std == 0:
        return {k: 0.0 for k in values}
    return {k: (v - mean) / std for k, v in values.items()}


@register
class FactorCryptoStrategy(Strategy):
    """Cross-sectional multi-factor ranking of crypto assets.

    v3: Rebalance cooldown, minimum score change, momentum confirmation.
    """

    name = "factor_crypto"

    def __init__(self):
        cfg = FACTOR_CRYPTO
        self.coins = cfg["coins"]
        self.lookback_days = cfg["lookback_days"]
        self.momentum_w = cfg["momentum_weight"]
        self.volatility_w = cfg["volatility_weight"]
        self.volume_w = cfg["volume_weight"]
        self.top_n = cfg["top_n_buy"]
        self.sell_threshold = cfg["sell_threshold"]
        self.min_points = cfg["min_data_points"]
        self.rebalance_cooldown = cfg["rebalance_cooldown"]
        self.min_score_change = cfg["min_score_change"]
        self._last_context: dict = {}
        self._held_coins: set[str] = set()
        self._last_scores: dict[str, float] = {}  # v3: track previous scores
        self._bars_since_rebalance: int = 0        # v3: cooldown counter

    # ------------------------------------------------------------------
    # Factor computations
    # ------------------------------------------------------------------

    @staticmethod
    def _momentum(prices: pd.Series) -> float | None:
        """21-day return."""
        if len(prices) < 21:
            return None
        return (prices.iloc[-1] / prices.iloc[-21]) - 1.0

    @staticmethod
    def _short_momentum(prices: pd.Series) -> float | None:
        """7-day return for confirmation."""
        if len(prices) < 7:
            return None
        return (prices.iloc[-1] / prices.iloc[-7]) - 1.0

    @staticmethod
    def _realized_vol(prices: pd.Series) -> float | None:
        """60-day annualized realized volatility."""
        if len(prices) < 60:
            return None
        log_ret = np.log(prices.iloc[-60:] / prices.iloc[-60:].shift(1)).dropna()
        if len(log_ret) < 10:
            return None
        return float(log_ret.std() * np.sqrt(365))

    @staticmethod
    def _volume_trend(volume: pd.Series) -> float | None:
        """Ratio of 5-day avg volume to 20-day avg volume."""
        if len(volume) < 20:
            return None
        avg_5 = volume.iloc[-5:].mean()
        avg_20 = volume.iloc[-20:].mean()
        if avg_20 == 0:
            return None
        return float(avg_5 / avg_20)

    # ------------------------------------------------------------------
    # Main signal generation
    # ------------------------------------------------------------------

    def generate_signals(self) -> list[Signal]:
        self._bars_since_rebalance += 1

        # Collect raw factor values per coin
        raw_momentum: dict[str, float] = {}
        raw_vol: dict[str, float] = {}
        raw_vt: dict[str, float] = {}
        short_mom: dict[str, float] = {}  # v3: 7-day momentum
        coin_prices: dict[str, float] = {}

        for coin_id in self.coins:
            symbol = CRYPTO_SYMBOLS.get(coin_id)
            if not symbol:
                continue

            try:
                ohlc = get_ohlc(coin_id, self.lookback_days)
                if ohlc.empty or len(ohlc) < self.min_points:
                    continue
                close = ohlc["close"]
            except Exception as e:
                log.warning("Failed to fetch OHLC for %s: %s", coin_id, e)
                continue

            volume = None
            try:
                hist = get_historical_prices(coin_id, self.lookback_days)
                if not hist.empty and "volume" in hist.columns:
                    volume = hist["volume"]
            except Exception:
                pass

            mom = self._momentum(close)
            smom = self._short_momentum(close)
            vol = self._realized_vol(close)
            vt = self._volume_trend(volume) if volume is not None else None

            if mom is not None:
                raw_momentum[coin_id] = mom
            if smom is not None:
                short_mom[coin_id] = smom
            if vol is not None:
                raw_vol[coin_id] = vol
            if vt is not None:
                raw_vt[coin_id] = vt

            coin_prices[coin_id] = float(close.iloc[-1])

        # Need enough coins for cross-sectional scoring
        scored_coins = set(raw_momentum) & set(raw_vol)
        if len(scored_coins) < _MIN_COINS_FOR_ZSCORE:
            self._last_context = {"error": f"Only {len(scored_coins)} coins with data"}
            return [Signal(
                strategy=self.name, symbol="BTC/USD", action="hold",
                strength=0.0,
                reason=f"Insufficient data: {len(scored_coins)} coins, need {_MIN_COINS_FOR_ZSCORE}",
            )]

        # Cross-sectional z-scores
        z_mom = _cross_sectional_zscore({c: raw_momentum[c] for c in scored_coins})
        z_vol_raw = _cross_sectional_zscore({c: raw_vol[c] for c in scored_coins})
        z_vol = {c: -v for c, v in z_vol_raw.items()}

        coins_with_vol = scored_coins & set(raw_vt)
        if len(coins_with_vol) >= _MIN_COINS_FOR_ZSCORE:
            z_vt = _cross_sectional_zscore({c: raw_vt[c] for c in coins_with_vol})
        else:
            z_vt = {}

        # Composite score
        composite: dict[str, float] = {}
        for coin_id in scored_coins:
            if coin_id in z_vt:
                score = (
                    self.momentum_w * z_mom[coin_id]
                    + self.volatility_w * z_vol[coin_id]
                    + self.volume_w * z_vt[coin_id]
                )
            else:
                total_w = self.momentum_w + self.volatility_w
                score = (
                    (self.momentum_w / total_w) * z_mom[coin_id]
                    + (self.volatility_w / total_w) * z_vol[coin_id]
                )
            composite[coin_id] = score

        ranked = sorted(composite.items(), key=lambda x: x[1], reverse=True)

        # v3: Check if cooldown allows rebalancing
        in_cooldown = self._bars_since_rebalance < self.rebalance_cooldown

        # Determine target positions
        top_coins = {c for c, s in ranked[:self.top_n] if s > 0}
        bottom_coins = {c for c, s in ranked if s < self.sell_threshold}
        dropped_coins = self._held_coins - top_coins

        # v3: Check if score changes are significant enough to trade
        signals: list[Signal] = []
        context_data: dict[str, dict] = {}
        made_trade = False

        for coin_id, score in ranked:
            symbol = CRYPTO_SYMBOLS[coin_id]
            strength = min(abs(score) / 2.0, 1.0)
            price = coin_prices.get(coin_id, 0)

            detail = {
                "composite": round(score, 3),
                "z_momentum": round(z_mom[coin_id], 3),
                "z_volatility": round(z_vol[coin_id], 3),
                "z_volume_trend": round(z_vt.get(coin_id, 0), 3),
                "price": round(price, 2),
            }
            context_data[coin_id] = detail

            prev_score = self._last_scores.get(coin_id, 0)
            score_change = abs(score - prev_score)

            if in_cooldown:
                # During cooldown, only emit holds
                signals.append(Signal(
                    strategy=self.name, symbol=symbol, action="hold",
                    strength=0.0,
                    reason=f"{coin_id} cooldown ({self._bars_since_rebalance}/{self.rebalance_cooldown})",
                    data=detail,
                ))
            elif coin_id in top_coins and coin_id not in self._held_coins:
                # v3: Only buy if score change is significant AND short momentum confirms
                smom_val = short_mom.get(coin_id, 0)
                if score_change >= self.min_score_change and smom_val > 0:
                    signals.append(Signal(
                        strategy=self.name, symbol=symbol, action="buy",
                        strength=strength,
                        reason=f"{coin_id} top-{self.top_n} composite={score:.2f} 7d-mom={smom_val:.3f}",
                        data=detail,
                    ))
                    made_trade = True
                else:
                    signals.append(Signal(
                        strategy=self.name, symbol=symbol, action="hold",
                        strength=0.0,
                        reason=f"{coin_id} top-ranked but score change {score_change:.2f} < {self.min_score_change} or 7d-mom negative",
                        data=detail,
                    ))
            elif coin_id in bottom_coins or coin_id in dropped_coins:
                if coin_id in self._held_coins:
                    sell_reason = "below threshold" if coin_id in bottom_coins else "dropped from top-N"
                    signals.append(Signal(
                        strategy=self.name, symbol=symbol, action="sell",
                        strength=max(strength, 0.4),
                        reason=f"{coin_id} {sell_reason} composite={score:.2f}",
                        data=detail,
                    ))
                    made_trade = True
                else:
                    signals.append(Signal(
                        strategy=self.name, symbol=symbol, action="hold",
                        strength=0.0,
                        reason=f"{coin_id} composite={score:.2f} — not held, no action",
                        data=detail,
                    ))
            else:
                signals.append(Signal(
                    strategy=self.name, symbol=symbol, action="hold",
                    strength=0.0,
                    reason=f"{coin_id} composite={score:.2f} — neutral",
                    data=detail,
                ))

        # Update state
        if made_trade:
            self._bars_since_rebalance = 0
            self._held_coins = top_coins.copy()
        self._last_scores = composite.copy()
        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
