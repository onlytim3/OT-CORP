"""On-chain flow strategy: buy on exchange outflows + TVL growth."""
import logging

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

logger = logging.getLogger(__name__)

SUPPORTED_SYMBOLS = ["BTCUSDT", "ETHUSDT"]


@register
class OnchainFlowStrategy(Strategy):
    """Buy on exchange outflows combined with TVL growth."""

    name = "onchain_flow"

    def __init__(self):
        self._last_context: dict = {}

    def generate_signals(self) -> list[Signal]:
        """Generate on-chain flow signals for BTC and ETH."""
        from trading.data.onchain import get_tvl_change, get_exchange_netflow

        signals: list[Signal] = []
        tvl_change = get_tvl_change(hours=24)

        for symbol in SUPPORTED_SYMBOLS:
            base = symbol.replace("USDT", "").replace("USD", "")
            if base not in ("BTC", "ETH"):
                continue

            netflow = get_exchange_netflow(base, hours=24)
            action = "hold"
            strength = 0.0
            data = {"tvl_change_24h": tvl_change, "exchange_netflow": netflow, "base_coin": base}

            if tvl_change is not None:
                if tvl_change > 0.02:
                    action = "buy"
                    strength = min(abs(tvl_change) * 10, 0.8)
                elif tvl_change < -0.02:
                    action = "sell"
                    strength = min(abs(tvl_change) * 10, 0.8)

            if netflow is not None:
                if netflow < -0.05 and action == "buy":
                    strength = min(strength * 1.3, 1.0)
                elif netflow > 0.05 and action == "sell":
                    strength = min(strength * 1.3, 1.0)
                elif netflow > 0.05 and action == "buy":
                    strength *= 0.5

            self._last_context[symbol] = data

            if action != "hold" and strength > 0.1:
                reason_parts = []
                if tvl_change is not None:
                    reason_parts.append(f"TVL 24h change: {tvl_change:+.2%}")
                if netflow is not None:
                    reason_parts.append(f"exchange netflow: {netflow:+.2%}")
                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    action=action,
                    strength=round(strength, 3),
                    reason=" | ".join(reason_parts) or "On-chain flow signal",
                    data=data,
                ))

        return signals

    def get_market_context(self) -> dict:
        """Return current on-chain flow context."""
        return {"onchain_flows": self._last_context}
