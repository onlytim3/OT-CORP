"""Funding rate AR(1) forecasting strategy."""
import logging
from typing import Optional

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

logger = logging.getLogger(__name__)

SIGNAL_THRESHOLD = 0.0003


def _ar1_forecast(rates: list[float]) -> Optional[float]:
    """Simple AR(1) forecast."""
    if len(rates) < 10:
        return None
    n = len(rates)
    mean_x = sum(rates[:-1]) / (n - 1)
    mean_y = sum(rates[1:]) / (n - 1)
    num = sum((rates[i] - mean_x) * (rates[i+1] - mean_y) for i in range(n-1))
    den = sum((rates[i] - mean_x) ** 2 for i in range(n-1))
    if den == 0:
        return mean_y
    beta = num / den
    alpha = mean_y - beta * mean_x
    return alpha + beta * rates[-1]


@register
class FundingForecastStrategy(Strategy):
    """Buy/sell based on AR(1) funding rate forecast divergence."""

    name = "funding_forecast"

    def __init__(self):
        self.default_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        self._last_context: dict = {}

    def generate_signals(self) -> list[Signal]:
        """Generate signals based on funding rate AR(1) forecast."""
        signals: list[Signal] = []
        try:
            from trading.execution.aster_client import get_aster_funding_rate
        except ImportError:
            return signals

        for symbol in self.default_symbols:
            try:
                rates_data = get_aster_funding_rate(symbol)
                if not rates_data:
                    continue
                if isinstance(rates_data, list):
                    rates = [float(r.get("fundingRate", 0)) for r in rates_data if r.get("fundingRate")]
                elif isinstance(rates_data, (int, float)):
                    continue
                else:
                    continue

                forecast = _ar1_forecast(rates)
                if forecast is None:
                    continue

                current_rate = rates[-1] if rates else 0
                predicted_move = forecast - current_rate

                action = "hold"
                strength = 0.0
                if predicted_move > SIGNAL_THRESHOLD:
                    action = "buy"
                    strength = min(abs(predicted_move) / SIGNAL_THRESHOLD * 0.3, 0.8)
                elif predicted_move < -SIGNAL_THRESHOLD:
                    action = "sell"
                    strength = min(abs(predicted_move) / SIGNAL_THRESHOLD * 0.3, 0.8)

                self._last_context[symbol] = {
                    "current_funding_rate": current_rate,
                    "forecast": forecast,
                    "predicted_move": predicted_move,
                }

                if action != "hold" and strength > 0.1:
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=symbol,
                        action=action,
                        strength=round(strength, 3),
                        reason=f"Funding AR(1) forecast: current={current_rate:.6f}, predicted={forecast:.6f}, move={predicted_move:.6f}",
                        data={"current_funding_rate": current_rate, "forecast": forecast, "predicted_move": predicted_move},
                    ))
            except Exception as e:
                logger.warning(f"Funding forecast error for {symbol}: {e}")

        return signals

    def get_market_context(self) -> dict:
        """Return current funding rate context."""
        return {"funding_rates": self._last_context}
