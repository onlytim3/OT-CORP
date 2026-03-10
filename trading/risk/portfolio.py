"""Portfolio allocation — volatility-adjusted sizing + per-strategy budgets.

v2: Adds ATR-based volatility scaling and strategy risk budgets.
"""

import logging

import numpy as np

from trading.config import RISK
from trading.db.store import get_positions
from trading.strategy.base import Signal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-strategy risk budget — fraction of total capital each strategy can deploy
# ---------------------------------------------------------------------------
STRATEGY_BUDGETS: dict[str, float] = {
    "momentum": 0.20,
    "mean_reversion": 0.15,
    "gold_btc": 0.10,
    "rsi_divergence": 0.10,
    "ema_crossover": 0.10,
    "bollinger_squeeze": 0.08,
    "btc_eth_ratio": 0.08,
    "tips_yield": 0.07,
    "fg_multi_timeframe": 0.07,
    "dxy_dollar": 0.05,
}
# Sum = 1.00 (full budget allocation)

DEFAULT_BUDGET = 0.05  # Fallback for unknown strategies


def _get_strategy_budget(strategy_name: str) -> float:
    """Return the risk budget fraction for a strategy."""
    return STRATEGY_BUDGETS.get(strategy_name, DEFAULT_BUDGET)


def _estimate_volatility(symbol: str) -> float:
    """Estimate annualized volatility using recent price data.

    Returns a volatility multiplier:
      - Low vol (< 30% annual) → 1.2 (size up)
      - Normal vol (30-60%)    → 1.0 (normal)
      - High vol (60-100%)     → 0.7 (size down)
      - Very high vol (>100%)  → 0.4 (minimal size)
    """
    try:
        if "/" in symbol:
            # Crypto — use CoinGecko OHLC
            from trading.data.crypto import get_ohlc
            from trading.config import CRYPTO_SYMBOLS
            reverse_map = {v: k for k, v in CRYPTO_SYMBOLS.items()}
            coin_id = reverse_map.get(symbol)
            if not coin_id:
                return 1.0
            df = get_ohlc(coin_id, days=14)
            if df.empty or len(df) < 5:
                return 1.0
            closes = df["close"].values
        else:
            # ETF — use yfinance
            from trading.data.commodities import get_etf_history
            df = get_etf_history(symbol, period="1mo")
            if df.empty or len(df) < 5:
                return 1.0
            closes = df["Close"].values

        # Calculate daily returns and ATR-style volatility
        returns = np.diff(closes) / closes[:-1]
        daily_vol = np.std(returns)

        # Annualize: crypto trades 365 days, ETFs trade 252 days
        ann_factor = np.sqrt(365) if "/" in symbol else np.sqrt(252)
        annual_vol = daily_vol * ann_factor

        # Scale position inversely to volatility
        if annual_vol < 0.30:
            return 1.2  # Low vol — slightly larger position
        elif annual_vol < 0.60:
            return 1.0  # Normal
        elif annual_vol < 1.00:
            return 0.7  # High vol — smaller position
        else:
            return 0.4  # Very high vol — minimal position

    except Exception as e:
        log.debug("Volatility estimation failed for %s: %s", symbol, e)
        return 1.0  # Default to no adjustment


def calculate_order_size(
    signal: Signal,
    portfolio_value: float,
    num_signals: int = 1,
    use_vol_sizing: bool = True,
    use_strategy_budgets: bool = True,
) -> float:
    """Calculate how much to allocate to a trade.

    Factors:
      1. Per-strategy risk budget (caps how much a strategy can deploy)
      2. Available free capital after positions + cash reserve
      3. Signal strength scaling
      4. Volatility adjustment (ATR-based) — size down in high vol
      5. Even split among concurrent buy signals
      6. Hard cap at max_position_pct

    Returns dollar amount to trade, or 0.0 if too small.
    """
    max_per_position = portfolio_value * RISK["max_position_pct"]
    cash_reserve = portfolio_value * RISK["min_cash_reserve_pct"]
    available = portfolio_value - cash_reserve

    # Check existing positions
    positions = get_positions()
    positions_value = sum(
        p["qty"] * (p["current_price"] or p["avg_cost"]) for p in positions
    )
    free_capital = available - positions_value

    if free_capital <= 0:
        return 0.0

    # Strategy budget cap
    if use_strategy_budgets:
        # Extract base strategy name (e.g., "momentum" from "momentum+ema_crossover")
        base_strategy = signal.strategy.split("+")[0] if signal.strategy else ""
        budget_pct = _get_strategy_budget(base_strategy)
        strategy_cap = portfolio_value * budget_pct

        # How much has this strategy already deployed?
        strategy_deployed = sum(
            p["qty"] * (p["current_price"] or p["avg_cost"])
            for p in positions
            if p.get("strategy", "").startswith(base_strategy)
        )
        strategy_remaining = max(strategy_cap - strategy_deployed, 0)
    else:
        strategy_remaining = free_capital

    # Effective ceiling = min(free capital, strategy budget remaining, max position)
    ceiling = min(free_capital, strategy_remaining, max_per_position)
    if ceiling <= 0:
        log.debug("No budget remaining for %s", signal.strategy)
        return 0.0

    # Split among concurrent buy signals
    per_signal = ceiling / max(num_signals, 1)

    # Scale by signal strength (0.0 – 1.0)
    order_value = per_signal * signal.strength

    # Volatility adjustment — scale position inversely to vol
    if use_vol_sizing and signal.action == "buy":
        vol_mult = _estimate_volatility(signal.symbol)
        order_value *= vol_mult
        log.debug(
            "Vol adjustment for %s: %.2fx → $%.2f",
            signal.symbol, vol_mult, order_value,
        )

    # Hard cap
    order_value = min(order_value, max_per_position)

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
                "qty": None,
                "reason": signal.reason,
            })

    return orders
