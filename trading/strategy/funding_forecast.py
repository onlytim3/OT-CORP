"""Funding rate AR(1) forecasting strategy."""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

STRATEGY_NAME = "funding_forecast"
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


def generate_signals(symbols: list[str] | None = None) -> list[dict]:
    """Generate signals based on funding rate AR(1) forecast."""
    signals = []
    try:
        from trading.execution.aster_client import get_aster_funding_rate
    except ImportError:
        return signals
    default_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    check_symbols = symbols or default_symbols
    for symbol in check_symbols:
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
            action = "HOLD"
            strength = 0.0
            if predicted_move > SIGNAL_THRESHOLD:
                action = "BUY"
                strength = min(abs(predicted_move) / SIGNAL_THRESHOLD * 0.3, 0.8)
            elif predicted_move < -SIGNAL_THRESHOLD:
                action = "SELL"
                strength = min(abs(predicted_move) / SIGNAL_THRESHOLD * 0.3, 0.8)
            if action != "HOLD" and strength > 0.1:
                signals.append({
                    "symbol": symbol, "action": action, "strength": round(strength, 3),
                    "strategy": STRATEGY_NAME, "timestamp": datetime.now(timezone.utc).isoformat(),
                    "data": {"current_funding_rate": current_rate, "forecast": forecast, "predicted_move": predicted_move},
                })
        except Exception as e:
            logger.warning(f"Funding forecast error for {symbol}: {e}")
    return signals
