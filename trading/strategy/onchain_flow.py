"""On-chain flow strategy: buy on exchange outflows + TVL growth."""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STRATEGY_NAME = "onchain_flow"
SUPPORTED_SYMBOLS = ["BTCUSDT", "ETHUSDT"]


def generate_signals(symbols: list[str] | None = None) -> list[dict]:
    """Generate on-chain flow signals for BTC and ETH."""
    from trading.data.onchain import get_tvl_change, get_exchange_netflow
    signals = []
    check_symbols = symbols or SUPPORTED_SYMBOLS
    tvl_change = get_tvl_change(hours=24)
    for symbol in check_symbols:
        base = symbol.replace("USDT", "").replace("USD", "")
        if base not in ("BTC", "ETH"):
            continue
        netflow = get_exchange_netflow(base, hours=24)
        action = "HOLD"
        strength = 0.0
        data = {"tvl_change_24h": tvl_change, "exchange_netflow": netflow, "base_coin": base}
        if tvl_change is not None:
            if tvl_change > 0.02:
                action = "BUY"
                strength = min(abs(tvl_change) * 10, 0.8)
            elif tvl_change < -0.02:
                action = "SELL"
                strength = min(abs(tvl_change) * 10, 0.8)
        if netflow is not None:
            if netflow < -0.05 and action == "BUY":
                strength = min(strength * 1.3, 1.0)
            elif netflow > 0.05 and action == "SELL":
                strength = min(strength * 1.3, 1.0)
            elif netflow > 0.05 and action == "BUY":
                strength *= 0.5
        if action != "HOLD" and strength > 0.1:
            signals.append({
                "symbol": symbol, "action": action, "strength": round(strength, 3),
                "strategy": STRATEGY_NAME, "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": data,
            })
    return signals
