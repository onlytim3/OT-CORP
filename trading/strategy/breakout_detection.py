"""Breakout Detection Strategy — Donchian Channel breakouts with trend + volume filters.

Adapted Turtle Trading system for crypto. Detects range breakouts on close-based
Donchian Channels, requires EMA trend alignment and volume surge confirmation
to avoid false breakouts.

v3: Added 50-period EMA trend filter (only buy breakouts in uptrend, sell in
downtrend), volume surge confirmation (current volume > 1.5x 20-bar avg),
and ATR-based profit target tracking.
"""

import logging

import numpy as np

from trading.config import CRYPTO_SYMBOLS
from trading.data.crypto import get_ohlc
from trading.strategy.base import Signal, Strategy
from trading.strategy.indicators import atr, bollinger_bands
from trading.strategy.registry import register

log = logging.getLogger(__name__)

BREAKOUT_DETECTION = {
    "coins": ["bitcoin", "ethereum", "solana"],
    "lookback_days": 90,
    "donchian_period": 10,
    "atr_period": 14,
    "atr_multiplier": 2.5,
    "squeeze_percentile": 20,
    "min_data_points": 30,
    "ema_trend_period": 50,       # v3: trend filter
    "volume_surge_mult": 1.5,     # v3: volume must be 1.5x average
}


def _ema_series(prices, period: int):
    """Compute EMA as a numpy array."""
    n = len(prices)
    ema = np.zeros(n)
    mult = 2.0 / (period + 1)
    ema[0] = float(prices[0])
    for i in range(1, n):
        ema[i] = (float(prices[i]) - ema[i - 1]) * mult + ema[i - 1]
    return ema


@register
class BreakoutDetectionStrategy(Strategy):
    """Buy on upper Donchian breakout (in uptrend with volume), sell on lower."""

    name = "breakout_detection"

    def __init__(self):
        cfg = BREAKOUT_DETECTION
        self.coins = cfg["coins"]
        self.lookback_days = cfg["lookback_days"]
        self.donchian_period = cfg["donchian_period"]
        self.atr_period = cfg["atr_period"]
        self.atr_multiplier = cfg["atr_multiplier"]
        self.squeeze_percentile = cfg["squeeze_percentile"]
        self.min_data_points = cfg["min_data_points"]
        self.ema_period = cfg["ema_trend_period"]
        self.vol_surge_mult = cfg["volume_surge_mult"]
        self._last_context = {}

    def generate_signals(self) -> list[Signal]:
        signals = []
        context_data = {}

        for coin_id in self.coins:
            try:
                signal, ctx = self._analyze_coin(coin_id)
                signals.append(signal)
                if ctx:
                    context_data[coin_id] = ctx
            except Exception as e:
                sym = CRYPTO_SYMBOLS.get(coin_id, "UNKNOWN/USD")
                log.warning("breakout_detection error for %s: %s", coin_id, e)
                signals.append(Signal(
                    strategy=self.name, symbol=sym, action="hold",
                    strength=0.0, reason=f"{coin_id} breakout error: {e}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}

    def _analyze_coin(self, coin_id: str) -> tuple[Signal, dict | None]:
        symbol = CRYPTO_SYMBOLS.get(coin_id)
        if not symbol:
            return (
                Signal(
                    strategy=self.name, symbol="UNKNOWN/USD", action="hold",
                    strength=0.0, reason=f"{coin_id} not in CRYPTO_SYMBOLS",
                ),
                None,
            )

        ohlc = get_ohlc(coin_id, self.lookback_days)
        if ohlc.empty or len(ohlc) < self.min_data_points:
            n = len(ohlc) if not ohlc.empty else 0
            return (
                Signal(
                    strategy=self.name, symbol=symbol, action="hold",
                    strength=0.0,
                    reason=f"{coin_id} insufficient data ({n}/{self.min_data_points} points)",
                ),
                None,
            )

        close = ohlc["close"]
        high = ohlc["high"]
        low = ohlc["low"]
        price = float(close.iloc[-1])

        # --- Donchian Channels (close-based, exclude current bar) ---
        prev_close = close.shift(1)
        upper_channel = prev_close.rolling(window=self.donchian_period).max()
        lower_channel = prev_close.rolling(window=self.donchian_period).min()
        middle_channel = (upper_channel + lower_channel) / 2

        dc_upper = float(upper_channel.iloc[-1])
        dc_lower = float(lower_channel.iloc[-1])
        dc_middle = float(middle_channel.iloc[-1])

        if np.isnan(dc_upper) or np.isnan(dc_lower):
            return (
                Signal(
                    strategy=self.name, symbol=symbol, action="hold",
                    strength=0.0, reason=f"{coin_id} Donchian NaN",
                ),
                None,
            )

        # --- v3: EMA trend filter ---
        close_arr = close.values.astype(float)
        trend = "neutral"
        if len(close_arr) >= self.ema_period:
            ema_vals = _ema_series(close_arr, self.ema_period)
            ema_current = ema_vals[-1]
            if price > ema_current * 1.01:  # 1% above EMA = uptrend
                trend = "up"
            elif price < ema_current * 0.99:  # 1% below EMA = downtrend
                trend = "down"

        # --- v3: Volume confirmation ---
        volume_confirmed = True  # Default to true if no volume data
        if "volume" in ohlc.columns:
            vol = ohlc["volume"]
            if len(vol) >= 20:
                avg_vol_20 = float(vol.iloc[-20:].mean())
                current_vol = float(vol.iloc[-1])
                if avg_vol_20 > 0:
                    volume_confirmed = current_vol >= avg_vol_20 * self.vol_surge_mult

        # --- Squeeze detection ---
        _, _, _, bandwidth = bollinger_bands(close, period=self.donchian_period)
        bw_valid = bandwidth.dropna()
        squeeze = False
        if len(bw_valid) >= self.min_data_points:
            current_bw = float(bw_valid.iloc[-1])
            threshold = float(np.nanpercentile(bw_valid.values, self.squeeze_percentile))
            squeeze = current_bw <= threshold

        # --- ATR ---
        atr_series = atr(high, low, close, period=self.atr_period)
        current_atr = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
        stop_distance = self.atr_multiplier * current_atr

        ctx = {
            "price": round(price, 2),
            "donchian_upper": round(dc_upper, 2),
            "donchian_lower": round(dc_lower, 2),
            "donchian_middle": round(dc_middle, 2),
            "squeeze": squeeze,
            "trend": trend,
            "volume_confirmed": volume_confirmed,
            "atr": round(current_atr, 2),
        }

        # --- Signal logic with v3 filters ---
        if price >= dc_upper:
            # v3: Only take upside breakout if trend is up AND volume confirms
            if trend == "down":
                return (
                    Signal(
                        strategy=self.name, symbol=symbol, action="hold",
                        strength=0.0,
                        reason=f"{coin_id} upside breakout rejected: trend is down",
                        data={**ctx, "coin": coin_id, "rejected": "trend_filter"},
                    ),
                    ctx,
                )
            if not volume_confirmed:
                return (
                    Signal(
                        strategy=self.name, symbol=symbol, action="hold",
                        strength=0.0,
                        reason=f"{coin_id} upside breakout rejected: low volume",
                        data={**ctx, "coin": coin_id, "rejected": "volume_filter"},
                    ),
                    ctx,
                )

            strength = self._compute_strength(
                price, dc_upper, dc_lower, current_atr, stop_distance, squeeze, "up",
            )
            reason = f"{coin_id} upside breakout above {dc_upper:.0f} (trend={trend})"
            if squeeze:
                reason += " +squeeze"
            return (
                Signal(
                    strategy=self.name, symbol=symbol, action="buy",
                    strength=strength, reason=reason,
                    data={**ctx, "coin": coin_id, "breakout": "upper"},
                ),
                ctx,
            )

        if price <= dc_lower:
            # v3: Only take downside breakout if trend is not strongly up
            if trend == "up":
                return (
                    Signal(
                        strategy=self.name, symbol=symbol, action="hold",
                        strength=0.0,
                        reason=f"{coin_id} downside breakout rejected: trend is up",
                        data={**ctx, "coin": coin_id, "rejected": "trend_filter"},
                    ),
                    ctx,
                )

            strength = self._compute_strength(
                price, dc_upper, dc_lower, current_atr, stop_distance, squeeze, "down",
            )
            reason = f"{coin_id} downside breakout below {dc_lower:.0f} (trend={trend})"
            if squeeze:
                reason += " +squeeze"
            return (
                Signal(
                    strategy=self.name, symbol=symbol, action="sell",
                    strength=strength, reason=reason,
                    data={**ctx, "coin": coin_id, "breakout": "lower"},
                ),
                ctx,
            )

        return (
            Signal(
                strategy=self.name, symbol=symbol, action="hold",
                strength=0.0,
                reason=f"{coin_id} price {price:.0f} in range [{dc_lower:.0f}, {dc_upper:.0f}]",
                data={**ctx, "coin": coin_id},
            ),
            ctx,
        )

    def _compute_strength(self, price, dc_upper, dc_lower, current_atr,
                           stop_distance, squeeze, direction) -> float:
        channel_width = dc_upper - dc_lower
        if channel_width <= 0:
            return 0.3

        if direction == "up":
            overshoot = price - dc_upper
        else:
            overshoot = dc_lower - price
        base = min(0.4 + (overshoot / channel_width) * 0.4, 0.8)

        if squeeze:
            base = min(base + 0.15, 0.95)

        if current_atr > 0:
            distance_to_stop = stop_distance
            if distance_to_stop <= current_atr:
                base *= 0.7

        return round(max(min(base, 1.0), 0.0), 2)
