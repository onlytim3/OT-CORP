"""Post-trade fill quality analysis.

Records every fill's slippage relative to the mid-price at signal time,
then provides rolling averages and a sizing penalty for symbols with
consistently poor execution.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def record_fill(
    symbol: str,
    mid_at_signal: float,
    fill_price: float,
    qty: float,
    side: str,
    notional: Optional[float] = None,
    volume_ratio: Optional[float] = None,
) -> None:
    """Record a fill in the fill_quality table for slippage tracking.

    Args:
        symbol: Trading symbol (Alpaca or Bybit format).
        mid_at_signal: Mid-price at the time the signal was generated.
        fill_price: Actual execution price.
        qty: Filled quantity.
        side: 'buy' or 'sell'.
        notional: Dollar value of the fill (optional, computed if missing).
        volume_ratio: Order size / recent volume ratio (optional).
    """
    if mid_at_signal <= 0 or fill_price <= 0:
        log.debug("Skipping fill record: invalid prices (mid=%.4f, fill=%.4f)",
                  mid_at_signal, fill_price)
        return

    # Slippage: positive = adverse (paid more for buy, received less for sell)
    if side.lower() == "buy":
        slippage_bps = (fill_price - mid_at_signal) / mid_at_signal * 10000
    else:
        slippage_bps = (mid_at_signal - fill_price) / mid_at_signal * 10000

    if notional is None:
        notional = fill_price * qty

    try:
        from trading.db.store import get_db
        now = datetime.now(timezone.utc).isoformat()

        with get_db() as conn:
            conn.execute(
                """INSERT INTO fill_quality
                   (timestamp, symbol, side, mid_price, fill_price, slippage_bps, notional, volume_ratio)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, symbol, side.lower(), mid_at_signal, fill_price,
                 slippage_bps, notional, volume_ratio),
            )

        log.info(
            "Fill recorded: %s %s %.6f @ $%.4f (mid=$%.4f, slippage=%.1fbps)",
            side.upper(), symbol, qty, fill_price, mid_at_signal, slippage_bps,
        )
    except Exception as e:
        log.warning("Failed to record fill for %s: %s", symbol, e)


def get_avg_slippage(symbol: str, lookback: int = 20) -> float:
    """Get the rolling average slippage in bps for a symbol.

    Args:
        symbol: Trading symbol.
        lookback: Number of recent fills to average over.

    Returns:
        Average slippage in basis points. Returns 0.0 if no data.
    """
    try:
        from trading.db.store import get_db

        with get_db() as conn:
            row = conn.execute(
                """SELECT AVG(slippage_bps) as avg_slip
                   FROM (
                       SELECT slippage_bps FROM fill_quality
                       WHERE symbol = ?
                       ORDER BY timestamp DESC
                       LIMIT ?
                   )""",
                (symbol, lookback),
            ).fetchone()

        if row and row["avg_slip"] is not None:
            return float(row["avg_slip"])
        return 0.0

    except Exception as e:
        log.debug("Failed to get avg slippage for %s: %s", symbol, e)
        return 0.0


def get_slippage_penalty(symbol: str, lookback: int = 20) -> float:
    """Compute a sizing penalty based on historical slippage.

    The penalty scales linearly: 50bps average slippage = 100% penalty (0.3 cap).
    This is used as a multiplier reduction: effective_size *= (1.0 - penalty).

    Args:
        symbol: Trading symbol.
        lookback: Number of recent fills to average.

    Returns:
        Penalty factor between 0.0 (no penalty) and 0.3 (max penalty).
    """
    avg = get_avg_slippage(symbol, lookback)
    # Only penalize adverse slippage (positive bps)
    if avg <= 0:
        return 0.0
    penalty = min(avg / 50.0, 0.3)
    if penalty > 0.05:
        log.info("Slippage penalty for %s: %.1f%% (avg slippage=%.1fbps)",
                 symbol, penalty * 100, avg)
    return penalty


def get_fill_quality_summary(symbol: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Get recent fill quality records for the API endpoint.

    Args:
        symbol: Filter by symbol (optional, returns all if None).
        limit: Max records to return.

    Returns:
        List of fill quality dicts.
    """
    try:
        from trading.db.store import get_db

        with get_db() as conn:
            if symbol:
                rows = conn.execute(
                    """SELECT * FROM fill_quality
                       WHERE symbol = ?
                       ORDER BY timestamp DESC LIMIT ?""",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM fill_quality
                       ORDER BY timestamp DESC LIMIT ?""",
                    (limit,),
                ).fetchall()

        return [dict(r) for r in rows]

    except Exception as e:
        log.warning("Failed to get fill quality summary: %s", e)
        return []
