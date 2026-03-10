"""Fear & Greed Multi-Timeframe — tiered entry using daily, 7d, and 30d averages."""

from trading.config import FG_MULTI_TIMEFRAME
from trading.data.sentiment import get_fear_greed
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register


@register
class FGMultiTimeframeStrategy(Strategy):
    """Enhanced F&G with multi-timeframe confirmation for higher conviction entries."""

    name = "fg_multi_timeframe"

    def __init__(self):
        self.extreme_fear = FG_MULTI_TIMEFRAME["extreme_fear"]
        self.fear = FG_MULTI_TIMEFRAME["fear"]
        self.greed = FG_MULTI_TIMEFRAME["greed"]
        self.extreme_greed = FG_MULTI_TIMEFRAME["extreme_greed"]
        self.symbol = FG_MULTI_TIMEFRAME["symbol"]
        self._last_fg = None

    def generate_signals(self) -> list[Signal]:
        try:
            fg = get_fear_greed(limit=30)
        except Exception:
            return [Signal(
                strategy=self.name,
                symbol=self.symbol,
                action="hold",
                strength=0.0,
                reason="Failed to fetch Fear & Greed data",
            )]

        self._last_fg = fg
        current = fg["current"]
        value = current["value"]
        history = fg["history"]

        # Multi-timeframe averages
        avg_7d = history["value"].tail(7).mean() if len(history) >= 7 else value
        avg_30d = history["value"].mean() if len(history) > 0 else value

        data = {
            "fg_daily": value,
            "fg_7d_avg": round(avg_7d, 1),
            "fg_30d_avg": round(avg_30d, 1),
            "classification": current["classification"],
        }

        signals = []

        # Tier 1: Extreme fear across all timeframes
        if value <= self.extreme_fear and avg_7d <= self.fear:
            signals.append(Signal(
                strategy=self.name,
                symbol=self.symbol,
                action="buy",
                strength=1.0,
                reason=f"F&G {value} (extreme fear) + 7d avg {avg_7d:.0f} — max conviction buy",
                data=data,
            ))

        # Tier 2: Fear with confirming average
        elif value <= self.fear and avg_7d <= self.fear + 10:
            strength = (self.fear - value) / self.fear
            signals.append(Signal(
                strategy=self.name,
                symbol=self.symbol,
                action="buy",
                strength=max(strength, 0.3),
                reason=f"F&G {value} (fear) + 7d avg {avg_7d:.0f} — moderate buy",
                data=data,
            ))

        # Tier 1: Extreme greed across all timeframes
        elif value >= self.extreme_greed and avg_7d >= self.greed:
            signals.append(Signal(
                strategy=self.name,
                symbol=self.symbol,
                action="sell",
                strength=1.0,
                reason=f"F&G {value} (extreme greed) + 7d avg {avg_7d:.0f} — max conviction sell",
                data=data,
            ))

        # Tier 2: Greed with confirming average
        elif value >= self.greed and avg_7d >= self.greed - 10:
            strength = (value - self.greed) / (100 - self.greed)
            signals.append(Signal(
                strategy=self.name,
                symbol=self.symbol,
                action="sell",
                strength=max(strength, 0.3),
                reason=f"F&G {value} (greed) + 7d avg {avg_7d:.0f} — moderate sell",
                data=data,
            ))

        else:
            signals.append(Signal(
                strategy=self.name,
                symbol=self.symbol,
                action="hold",
                strength=0.0,
                reason=f"F&G {value} ({current['classification']}) + 7d avg {avg_7d:.0f} — neutral",
                data=data,
            ))

        return signals

    def get_market_context(self) -> dict:
        if self._last_fg is None:
            try:
                self._last_fg = get_fear_greed(limit=30)
            except Exception:
                return {"strategy": self.name}

        current = self._last_fg["current"]
        history = self._last_fg["history"]
        return {
            "strategy": self.name,
            "fg_daily": current["value"],
            "classification": current["classification"],
            "7d_avg": round(history["value"].tail(7).mean(), 1) if len(history) >= 7 else current["value"],
            "30d_avg": round(history["value"].mean(), 1) if len(history) > 0 else current["value"],
            "extreme_fear_threshold": self.extreme_fear,
            "extreme_greed_threshold": self.extreme_greed,
        }
