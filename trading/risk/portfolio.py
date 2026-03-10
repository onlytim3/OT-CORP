"""Portfolio allocation and rebalancing logic."""

from trading.config import RISK
from trading.db.store import get_positions
from trading.strategy.base import Signal


def calculate_order_size(signal: Signal, portfolio_value: float, num_signals: int = 1) -> float:
    """Calculate how much to allocate to a trade.

    Divides available allocation evenly among actionable signals,
    capped by max position size.
    """
    max_per_position = portfolio_value * RISK["max_position_pct"]
    cash_reserve = portfolio_value * RISK["min_cash_reserve_pct"]
    available = portfolio_value - cash_reserve

    # Check existing positions
    positions = get_positions()
    positions_value = sum(p["qty"] * (p["current_price"] or p["avg_cost"]) for p in positions)
    free_capital = available - positions_value

    if free_capital <= 0:
        return 0.0

    # Split free capital among signals, capped by max position size
    per_signal = min(free_capital / max(num_signals, 1), max_per_position)

    # Scale by signal strength
    order_value = per_signal * signal.strength

    # Minimum order size — don't place $1 orders
    if order_value < 5.0:
        return 0.0

    return round(order_value, 2)


def get_rebalance_targets(signals: list[Signal], portfolio_value: float) -> list[dict]:
    """Given signals, compute target orders for rebalancing.

    Returns list of {symbol, action, value, reason}.
    """
    buy_signals = [s for s in signals if s.action == "buy"]
    sell_signals = [s for s in signals if s.action == "sell"]
    orders = []

    # Process sells first to free up capital
    for signal in sell_signals:
        positions = get_positions()
        pos = next((p for p in positions if p["symbol"] == signal.symbol), None)
        if pos and pos["qty"] > 0:
            sell_value = pos["qty"] * (pos["current_price"] or pos["avg_cost"])
            orders.append({
                "symbol": signal.symbol,
                "action": "sell",
                "value": sell_value,
                "qty": pos["qty"],
                "reason": signal.reason,
            })

    # Then process buys
    for signal in buy_signals:
        order_value = calculate_order_size(signal, portfolio_value, len(buy_signals))
        if order_value > 0:
            orders.append({
                "symbol": signal.symbol,
                "action": "buy",
                "value": order_value,
                "qty": None,  # Calculated at execution time based on current price
                "reason": signal.reason,
            })

    return orders
