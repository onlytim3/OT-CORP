"""Fear & Greed Mean Reversion Strategy — buy extreme fear, sell extreme greed."""

from trading.config import MEAN_REVERSION
from trading.data.sentiment import get_fear_greed, classify_fear_greed
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register


@register
class MeanReversionStrategy(Strategy):
    """Buy BTC when Fear & Greed is extreme fear, sell on extreme greed."""

    name = "mean_reversion"

    def __init__(self):
        self.buy_threshold = MEAN_REVERSION["fear_buy_threshold"]
        self.sell_threshold = MEAN_REVERSION["greed_sell_threshold"]
        self.symbol = MEAN_REVERSION["symbol"]
        self._last_fg = None

    def generate_signals(self) -> list[Signal]:
        fg = get_fear_greed(limit=30)
        self._last_fg = fg
        current = fg["current"]
        value = current["value"]
        history = fg["history"]

        # Calculate 7d average for trend
        recent_avg = history["value"].tail(7).mean() if len(history) >= 7 else value

        signals = []

        if value <= self.buy_threshold:
            # Extreme fear — BUY signal
            strength = (self.buy_threshold - value) / self.buy_threshold
            signals.append(Signal(
                strategy=self.name,
                symbol=self.symbol,
                action="buy",
                strength=min(strength, 1.0),
                reason=f"Fear & Greed at {value} ({current['classification']}) — contrarian buy",
                data={
                    "fear_greed": value,
                    "classification": current["classification"],
                    "7d_avg": round(recent_avg, 1),
                    "threshold": self.buy_threshold,
                },
            ))
        elif value >= self.sell_threshold:
            # Extreme greed — SELL signal
            strength = (value - self.sell_threshold) / (100 - self.sell_threshold)
            signals.append(Signal(
                strategy=self.name,
                symbol=self.symbol,
                action="sell",
                strength=min(strength, 1.0),
                reason=f"Fear & Greed at {value} ({current['classification']}) — contrarian sell",
                data={
                    "fear_greed": value,
                    "classification": current["classification"],
                    "7d_avg": round(recent_avg, 1),
                    "threshold": self.sell_threshold,
                },
            ))
        else:
            signals.append(Signal(
                strategy=self.name,
                symbol=self.symbol,
                action="hold",
                strength=0.0,
                reason=f"Fear & Greed at {value} ({current['classification']}) — neutral zone",
                data={
                    "fear_greed": value,
                    "classification": current["classification"],
                    "7d_avg": round(recent_avg, 1),
                },
            ))

        return signals

    def get_market_context(self) -> dict:
        if self._last_fg is None:
            fg = get_fear_greed(limit=30)
            self._last_fg = fg
        current = self._last_fg["current"]
        history = self._last_fg["history"]
        return {
            "strategy": self.name,
            "fear_greed_value": current["value"],
            "classification": current["classification"],
            "7d_avg": round(history["value"].tail(7).mean(), 1) if len(history) >= 7 else current["value"],
            "30d_avg": round(history["value"].mean(), 1) if len(history) > 0 else current["value"],
            "buy_threshold": self.buy_threshold,
            "sell_threshold": self.sell_threshold,
        }
