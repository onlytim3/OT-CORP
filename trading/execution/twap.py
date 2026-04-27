"""TWAP (Time-Weighted Average Price) execution engine.

Splits large orders into smaller slices executed over time to minimize
market impact. Uses limit-with-fallback for each slice.

When to use TWAP: orders whose notional exceeds 5% of the symbol's
recent 1-hour volume are considered "large" and benefit from slicing.
"""

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)


def should_use_twap(notional: float, symbol: str, threshold_pct: float = 0.05) -> bool:
    """Return True if the order is large enough to warrant TWAP execution.

    An order is "large" when its notional exceeds `threshold_pct` (default 5%)
    of the symbol's recent 1-hour quote volume.

    Args:
        notional: Dollar value of the order.
        symbol: Alpaca-style symbol (e.g. "BTC/USD") or Bybit symbol.
        threshold_pct: Fraction of 1h volume above which TWAP is used.

    Returns:
        True if TWAP is recommended, False otherwise.
        Returns False on data failure (fail-open: execute normally).
    """
    if notional <= 0:
        return False

    try:
        from trading.execution.router import _to_bybit
        from trading.execution.bybit_client import get_bybit_klines

        bybit_sym = _to_bybit(symbol)
        df = get_bybit_klines(bybit_sym, interval="1h", limit=2)
        if df is None or df.empty:
            return False

        # Use the most recent completed candle's quote volume
        recent_quote_vol = float(df["quote_volume"].iloc[-1])
        if recent_quote_vol <= 0:
            return False

        ratio = notional / recent_quote_vol
        if ratio > threshold_pct:
            log.info(
                "TWAP recommended for %s: order $%.2f is %.1f%% of 1h volume ($%.0f)",
                symbol, notional, ratio * 100, recent_quote_vol,
            )
            return True
        return False

    except Exception as e:
        log.debug("TWAP check failed for %s, skipping TWAP: %s", symbol, e)
        return False


def execute_twap(
    symbol: str,
    side: str,
    total_qty: float,
    slices: int = 3,
    interval: int = 60,
    stop_loss_price: Optional[float] = None,
    leverage: int = 1,
) -> dict:
    """Execute a large order using TWAP -- split into slices over time.

    Each slice is submitted as a limit-with-fallback order. The function
    blocks for up to `slices * interval` seconds.

    Args:
        symbol: Alpaca-style or Bybit symbol.
        side: 'buy' or 'sell'.
        total_qty: Total quantity to execute.
        slices: Number of child orders (default 3).
        interval: Seconds between slices (default 60).
        stop_loss_price: Optional SL -- only placed after the final slice.
        leverage: Leverage multiplier passed to each child order.

    Returns:
        Dict with: total_filled, avg_fill_price, slippage_bps, slices_executed,
        child_orders (list of individual order results).
    """
    from trading.execution.router import place_limit_with_fallback, _to_bybit
    from trading.execution.bybit_client import get_bybit_book_ticker

    if slices < 1:
        slices = 1
    slice_qty = total_qty / slices

    child_orders = []
    total_filled = 0.0
    weighted_price_sum = 0.0  # sum of (fill_price * fill_qty) for VWAP

    # Capture mid price at start for slippage calculation
    try:
        bybit_sym = _to_bybit(symbol)
        book = get_bybit_book_ticker(bybit_sym)
        bid = float(book.get("bidPrice", 0))
        ask = float(book.get("askPrice", 0))
        initial_mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0
    except Exception:
        initial_mid = 0

    log.info(
        "TWAP START: %s %s qty=%.6f in %d slices, interval=%ds",
        side.upper(), symbol, total_qty, slices, interval,
    )

    for i in range(slices):
        # Adjust last slice to fill remaining quantity exactly
        remaining = total_qty - total_filled
        this_qty = min(slice_qty, remaining)
        if this_qty <= 0:
            break

        log.info("TWAP slice %d/%d: %s %.6f %s", i + 1, slices, side, this_qty, symbol)

        # Only attach stop-loss to the final slice
        sl = stop_loss_price if (i == slices - 1) else None

        try:
            order = place_limit_with_fallback(
                symbol=symbol,
                side=side,
                qty=this_qty,
                timeout=max(interval - 5, 10),
                leverage=leverage,
                stop_loss_price=sl,
            )
            child_orders.append(order)

            filled = float(order.get("filled_qty", 0))
            price = float(order.get("filled_avg_price", 0))
            if filled > 0 and price > 0:
                total_filled += filled
                weighted_price_sum += filled * price
                log.info(
                    "TWAP slice %d/%d filled: %.6f @ $%.4f",
                    i + 1, slices, filled, price,
                )
            else:
                log.warning("TWAP slice %d/%d: no fill (status=%s)", i + 1, slices, order.get("status"))

        except Exception as e:
            log.error("TWAP slice %d/%d failed: %s", i + 1, slices, e)
            child_orders.append({"status": "error", "reason": str(e)})

        # Wait before next slice (skip wait after last slice)
        if i < slices - 1 and total_filled < total_qty:
            time.sleep(interval)

    # Calculate results
    avg_fill_price = (weighted_price_sum / total_filled) if total_filled > 0 else 0

    # Slippage in basis points vs initial mid
    slippage_bps = 0.0
    if initial_mid > 0 and avg_fill_price > 0:
        if side.lower() == "buy":
            slippage_bps = (avg_fill_price - initial_mid) / initial_mid * 10000
        else:
            slippage_bps = (initial_mid - avg_fill_price) / initial_mid * 10000

    log.info(
        "TWAP DONE: %s %s filled=%.6f/%.6f avg_price=$%.4f slippage=%.1fbps",
        side.upper(), symbol, total_filled, total_qty, avg_fill_price, slippage_bps,
    )

    return {
        "total_filled": total_filled,
        "avg_fill_price": avg_fill_price,
        "slippage_bps": slippage_bps,
        "slices_executed": len(child_orders),
        "child_orders": child_orders,
        "id": child_orders[-1].get("id") if child_orders else None,
        "status": "filled" if total_filled > 0 else "rejected",
        "symbol": symbol,
        "side": side,
        "qty": total_qty,
        "filled_qty": total_filled,
        "filled_avg_price": avg_fill_price,
    }
