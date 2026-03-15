"""Regime-Aware Mean Reversion Strategy.

Detects market regime via ADX, then applies Bollinger Band mean reversion
when the market is ranging (ADX < threshold).

v2: Loosened entry filters — scoring system instead of AND gate.
ADX threshold raised to 30, BB narrowed to 1.5 std, RSI widened to 40/60.
Uses additive scoring across BB position, RSI, and z-score instead of
requiring all conditions simultaneously.
"""

import logging

import numpy as np
import pandas as pd

from trading.config import CRYPTO_SYMBOLS
from trading.data.crypto import get_ohlc
from trading.strategy.base import Signal, Strategy
from trading.strategy.indicators import bollinger_bands, rsi, z_score
from trading.strategy.registry import register

log = logging.getLogger(__name__)

REGIME_MEAN_REVERSION = {
    "coins": ["bitcoin", "ethereum", "solana"],
    "lookback_days": 90,
    "adx_period": 14,
    "adx_threshold": 30,          # v2: raised from 25 — more ranging opportunities
    "bb_period": 20,
    "bb_std": 1.5,                # v2: narrowed from 2.0 — easier to touch bands
    "rsi_oversold": 40,           # v2: widened from 30
    "rsi_overbought": 60,         # v2: widened from 70
    "min_data_points": 40,
    "min_score_threshold": 0.4,   # v2: minimum score to trigger signal
}


def _compute_adx(high: pd.Series, low: pd.Series, close: pd.Series,
                 period: int = 14) -> pd.Series:
    """Compute Average Directional Index from OHLC data."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    plus_dm = high - prev_high
    minus_dm = prev_low - low
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = true_range.ewm(span=period, adjust=False).mean()
    smooth_plus_dm = plus_dm.ewm(span=period, adjust=False).mean()
    smooth_minus_dm = minus_dm.ewm(span=period, adjust=False).mean()

    plus_di = 100 * smooth_plus_dm / atr.replace(0, np.nan)
    minus_di = 100 * smooth_minus_dm / atr.replace(0, np.nan)

    di_sum = plus_di + minus_di
    dx = (plus_di - minus_di).abs() / di_sum.replace(0, np.nan) * 100
    adx = dx.ewm(span=period, adjust=False).mean()

    return adx


@register
class RegimeMeanReversionStrategy(Strategy):
    """Mean reversion that only trades in ranging (low-ADX) markets.

    v2: Uses scoring system instead of AND gate. Each indicator contributes
    a score component, and the total determines signal strength.
    """

    name = "regime_mean_reversion"

    def __init__(self):
        cfg = REGIME_MEAN_REVERSION
        self.coins = cfg["coins"]
        self.lookback_days = cfg["lookback_days"]
        self.adx_period = cfg["adx_period"]
        self.adx_threshold = cfg["adx_threshold"]
        self.bb_period = cfg["bb_period"]
        self.bb_std = cfg["bb_std"]
        self.rsi_oversold = cfg["rsi_oversold"]
        self.rsi_overbought = cfg["rsi_overbought"]
        self.min_data_points = cfg["min_data_points"]
        self.min_score = cfg["min_score_threshold"]
        self._last_context = {}

    def generate_signals(self) -> list[Signal]:
        signals = []
        context_data = {}

        for coin_id in self.coins:
            try:
                signal = self._analyze_coin(coin_id, context_data)
                signals.append(signal)
            except Exception as e:
                sym = CRYPTO_SYMBOLS.get(coin_id, f"{coin_id}/USD")
                log.warning("regime_mean_reversion error for %s: %s", coin_id, e)
                signals.append(Signal(
                    strategy=self.name, symbol=sym, action="hold",
                    strength=0.0, reason=f"{coin_id} analysis error: {e}",
                ))

        self._last_context = context_data
        return signals

    def _analyze_coin(self, coin_id: str, context_data: dict) -> Signal:
        alpaca_symbol = CRYPTO_SYMBOLS.get(coin_id)
        if not alpaca_symbol:
            return Signal(
                strategy=self.name, symbol=f"{coin_id}/USD", action="hold",
                strength=0.0, reason=f"{coin_id} not in CRYPTO_SYMBOLS",
            )

        ohlc = get_ohlc(coin_id, self.lookback_days)
        if ohlc.empty or len(ohlc) < self.min_data_points:
            count = len(ohlc) if not ohlc.empty else 0
            return Signal(
                strategy=self.name, symbol=alpaca_symbol, action="hold",
                strength=0.0,
                reason=f"{coin_id} insufficient data ({count} candles, need {self.min_data_points})",
            )

        close = ohlc["close"]
        high = ohlc["high"]
        low = ohlc["low"]
        price = float(close.iloc[-1])

        # Regime detection
        adx_series = _compute_adx(high, low, close, self.adx_period)
        current_adx = float(adx_series.dropna().iloc[-1]) if not adx_series.dropna().empty else 0.0

        # Indicators
        rsi_series = rsi(close)
        current_rsi = float(rsi_series.iloc[-1])
        upper, middle, lower, bandwidth = bollinger_bands(close, self.bb_period, self.bb_std)
        z = z_score(close, self.bb_period)
        current_z = float(z.iloc[-1]) if not z.dropna().empty else 0.0

        current_upper = float(upper.iloc[-1])
        current_lower = float(lower.iloc[-1])
        current_middle = float(middle.iloc[-1])

        context_data[coin_id] = {
            "price": round(price, 2),
            "adx": round(current_adx, 1),
            "regime": "trending" if current_adx > self.adx_threshold else "ranging",
            "rsi": round(current_rsi, 1),
            "z_score": round(current_z, 2),
            "bb_upper": round(current_upper, 2),
            "bb_lower": round(current_lower, 2),
        }

        # Trending regime: hold
        if current_adx > self.adx_threshold:
            return Signal(
                strategy=self.name, symbol=alpaca_symbol, action="hold",
                strength=0.0,
                reason=f"{coin_id} ADX {current_adx:.0f} > {self.adx_threshold} — trending regime",
                data={"adx": round(current_adx, 1), "regime": "trending", "coin": coin_id},
            )

        # v2: Scoring system instead of AND gate
        action, strength, reason = self._score_mean_reversion(
            coin_id, price, current_rsi, current_z,
            current_upper, current_lower, current_middle,
        )

        return Signal(
            strategy=self.name, symbol=alpaca_symbol,
            action=action, strength=strength, reason=reason,
            data={
                "adx": round(current_adx, 1),
                "rsi": round(current_rsi, 1),
                "z_score": round(current_z, 2),
                "regime": "ranging",
                "coin": coin_id,
            },
        )

    def _score_mean_reversion(self, coin_id: str, price: float,
                               current_rsi: float, current_z: float,
                               upper: float, lower: float,
                               middle: float) -> tuple[str, float, str]:
        """Score mean reversion using additive scoring across indicators.

        Each indicator contributes independently:
        - BB position: how far price is beyond the band (0-0.4)
        - RSI: how oversold/overbought (0-0.3)
        - Z-score: statistical extremity (0-0.3)

        Total score determines action and strength.
        """
        bb_range = upper - lower if upper != lower else 1.0
        buy_score = 0.0
        sell_score = 0.0
        components = []

        # --- BB position component (0-0.4) ---
        if price <= lower:
            bb_depth = min((lower - price) / bb_range + 0.2, 0.4)
            buy_score += bb_depth
            components.append(f"BB-low={bb_depth:.2f}")
        elif price <= middle:
            # Price below middle but above lower — mild buy signal
            bb_proximity = (middle - price) / (middle - lower) if middle != lower else 0
            bb_component = bb_proximity * 0.2
            buy_score += bb_component
            if bb_component > 0.05:
                components.append(f"BB-mid={bb_component:.2f}")
        elif price >= upper:
            bb_depth = min((price - upper) / bb_range + 0.2, 0.4)
            sell_score += bb_depth
            components.append(f"BB-high={bb_depth:.2f}")
        elif price >= middle:
            bb_proximity = (price - middle) / (upper - middle) if upper != middle else 0
            bb_component = bb_proximity * 0.2
            sell_score += bb_component
            if bb_component > 0.05:
                components.append(f"BB-mid={bb_component:.2f}")

        # --- RSI component (0-0.3) ---
        if current_rsi < self.rsi_oversold:
            rsi_score = min((self.rsi_oversold - current_rsi) / self.rsi_oversold * 0.3, 0.3)
            buy_score += rsi_score
            components.append(f"RSI={current_rsi:.0f}")
        elif current_rsi > self.rsi_overbought:
            rsi_score = min((current_rsi - self.rsi_overbought) / (100 - self.rsi_overbought) * 0.3, 0.3)
            sell_score += rsi_score
            components.append(f"RSI={current_rsi:.0f}")

        # --- Z-score component (0-0.3) ---
        if current_z < -1.0:
            z_component = min(abs(current_z) / 3.0 * 0.3, 0.3)
            buy_score += z_component
            components.append(f"z={current_z:.1f}")
        elif current_z > 1.0:
            z_component = min(abs(current_z) / 3.0 * 0.3, 0.3)
            sell_score += z_component
            components.append(f"z={current_z:.1f}")

        # --- Decision ---
        comp_str = ", ".join(components) if components else "no extremes"

        if buy_score >= self.min_score and buy_score > sell_score:
            strength = min(buy_score, 1.0)
            return ("buy", round(strength, 3),
                    f"{coin_id} ranging buy: score={buy_score:.2f} ({comp_str})")

        if sell_score >= self.min_score and sell_score > buy_score:
            strength = min(sell_score, 1.0)
            return ("sell", round(strength, 3),
                    f"{coin_id} ranging sell: score={sell_score:.2f} ({comp_str})")

        return ("hold", 0.0,
                f"{coin_id} ranging, score too low: buy={buy_score:.2f} sell={sell_score:.2f} ({comp_str})")

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
