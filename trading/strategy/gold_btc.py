"""Gold/BTC Ratio Strategy — trade divergences between gold and bitcoin."""

import numpy as np
import pandas as pd

from trading.config import GOLD_BTC
from trading.data.crypto import get_historical_prices
from trading.data.commodities import get_etf_history
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register


@register
class GoldBTCStrategy(Strategy):
    """Trade the Gold/BTC ratio when it deviates from its mean."""

    name = "gold_btc"

    def __init__(self):
        self.std_threshold = GOLD_BTC["std_dev_threshold"]
        self.lookback = GOLD_BTC["lookback_days"]
        self.gold_symbol = GOLD_BTC["gold_symbol"]
        self.btc_symbol = GOLD_BTC["btc_symbol"]
        self._last_context = None

    def _get_ratio_data(self) -> pd.DataFrame | None:
        """Get the Gold/BTC price ratio over the lookback period."""
        try:
            # Get gold ETF history
            gold = get_etf_history(self.gold_symbol, period="3mo")
            if gold.empty:
                return None

            # Get BTC history
            btc = get_historical_prices("bitcoin", days=90)
            if btc.empty:
                return None

            # Align dates
            gold_daily = gold["Close"].resample("D").last().dropna()
            btc_daily = btc["price"].resample("D").last().dropna()

            # Create ratio DataFrame
            combined = pd.DataFrame({"gold": gold_daily, "btc": btc_daily}).dropna()
            if len(combined) < self.lookback:
                return None

            # Normalize: gold price / BTC price (scaled for readability)
            combined["ratio"] = combined["gold"] / combined["btc"] * 10000
            return combined

        except Exception:
            return None

    def generate_signals(self) -> list[Signal]:
        data = self._get_ratio_data()
        if data is None or len(data) < self.lookback:
            return [Signal(
                strategy=self.name,
                symbol=self.btc_symbol,
                action="hold",
                strength=0.0,
                reason="Insufficient data for Gold/BTC ratio analysis",
            )]

        recent = data.tail(self.lookback)
        current_ratio = recent["ratio"].iloc[-1]
        mean_ratio = recent["ratio"].mean()
        std_ratio = recent["ratio"].std()

        if std_ratio == 0:
            return []

        z_score = (current_ratio - mean_ratio) / std_ratio

        self._last_context = {
            "current_ratio": round(current_ratio, 4),
            "mean_ratio": round(mean_ratio, 4),
            "std_ratio": round(std_ratio, 4),
            "z_score": round(z_score, 2),
            "gold_price": round(data["gold"].iloc[-1], 2),
            "btc_price": round(data["btc"].iloc[-1], 2),
        }

        signals = []

        if z_score > self.std_threshold:
            # Gold outperforming BTC — ratio too high — buy BTC, reduce gold
            signals.append(Signal(
                strategy=self.name,
                symbol=self.btc_symbol,
                action="buy",
                strength=min((z_score - self.std_threshold) / 2, 1.0),
                reason=f"Gold/BTC ratio z-score {z_score:.1f} — BTC undervalued vs gold, buy BTC",
                data=self._last_context,
            ))
            signals.append(Signal(
                strategy=self.name,
                symbol=self.gold_symbol,
                action="sell",
                strength=min((z_score - self.std_threshold) / 2, 1.0),
                reason=f"Gold/BTC ratio z-score {z_score:.1f} — reduce gold exposure",
                data=self._last_context,
            ))

        elif z_score < -self.std_threshold:
            # BTC outperforming gold — ratio too low — buy gold, reduce BTC
            signals.append(Signal(
                strategy=self.name,
                symbol=self.gold_symbol,
                action="buy",
                strength=min((abs(z_score) - self.std_threshold) / 2, 1.0),
                reason=f"Gold/BTC ratio z-score {z_score:.1f} — gold undervalued vs BTC, buy gold",
                data=self._last_context,
            ))
            signals.append(Signal(
                strategy=self.name,
                symbol=self.btc_symbol,
                action="sell",
                strength=min((abs(z_score) - self.std_threshold) / 2, 1.0),
                reason=f"Gold/BTC ratio z-score {z_score:.1f} — reduce BTC exposure",
                data=self._last_context,
            ))

        else:
            signals.append(Signal(
                strategy=self.name,
                symbol=self.btc_symbol,
                action="hold",
                strength=0.0,
                reason=f"Gold/BTC ratio z-score {z_score:.1f} — within normal range",
                data=self._last_context,
            ))

        return signals

    def get_market_context(self) -> dict:
        if self._last_context is None:
            self._get_ratio_data()
        return {
            "strategy": self.name,
            **(self._last_context or {}),
            "std_threshold": self.std_threshold,
            "lookback_days": self.lookback,
        }
