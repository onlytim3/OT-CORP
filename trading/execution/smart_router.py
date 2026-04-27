"""Smart order routing -- compare venues and select the best execution path.

Currently supports Bybit (perpetual futures) as the primary venue.
Alpaca comparison is logged but Bybit is always preferred for perps.
This module provides the foundation for multi-venue routing when additional
venues are integrated.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Venue constants
VENUE_BYBIT = "asterdex"
VENUE_ALPACA = "alpaca"


def compare_venues(symbol: str) -> dict:
    """Compare execution quality across available venues for a symbol.

    Fetches bid/ask from Bybit and (if available) Alpaca, computing
    spread and depth metrics for each.

    Args:
        symbol: Alpaca-style symbol (e.g. "BTC/USD").

    Returns:
        Dict with per-venue metrics: {venue: {bid, ask, spread_bps, depth}}.
    """
    result = {}

    # Bybit (always available for crypto perps)
    try:
        from trading.execution.router import _to_bybit
        from trading.execution.bybit_client import get_bybit_book_ticker

        bybit_sym = _to_bybit(symbol)
        book = get_bybit_book_ticker(bybit_sym)
        bid = float(book.get("bidPrice", 0))
        ask = float(book.get("askPrice", 0))
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0
        spread_bps = (ask - bid) / mid * 10000 if mid > 0 else 0

        result[VENUE_BYBIT] = {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_bps": spread_bps,
            "bid_depth": float(book.get("bidQty", 0)),
            "ask_depth": float(book.get("askQty", 0)),
            "available": True,
        }
    except Exception as e:
        log.debug("Bybit venue check failed for %s: %s", symbol, e)
        result[VENUE_BYBIT] = {"available": False, "error": str(e)}

    # Alpaca (logged for comparison only -- not used for execution)
    result[VENUE_ALPACA] = {
        "available": False,
        "reason": "Alpaca perp routing not implemented -- Bybit preferred",
    }

    # Log comparison
    if result[VENUE_BYBIT].get("available"):
        aster_spread = result[VENUE_BYBIT]["spread_bps"]
        log.debug(
            "Venue comparison for %s: Bybit spread=%.1fbps",
            symbol, aster_spread,
        )

    return result


def route_order(symbol: str, side: str, qty: float) -> dict:
    """Determine the best venue for order execution.

    Current behavior: always routes to Bybit for perpetual futures.
    Logs venue comparison for future optimization.

    Args:
        symbol: Trading symbol.
        side: 'buy' or 'sell'.
        qty: Order quantity.

    Returns:
        Dict with: venue (str), reason (str), venue_data (dict from compare_venues).
    """
    venues = compare_venues(symbol)

    # Current policy: Bybit for all perps
    chosen = VENUE_BYBIT
    reason = "Bybit is the primary venue for perpetual futures"

    # Log the routing decision
    aster_data = venues.get(VENUE_BYBIT, {})
    if aster_data.get("available"):
        spread = aster_data.get("spread_bps", 0)
        log.info(
            "Routing %s %s %.6f to %s (spread=%.1fbps)",
            side.upper(), symbol, qty, chosen, spread,
        )
    else:
        log.warning(
            "Routing %s %s to %s despite venue check failure",
            side.upper(), symbol, chosen,
        )

    return {
        "venue": chosen,
        "reason": reason,
        "venue_data": venues,
    }


def get_preferred_venue(symbol: str) -> str:
    """Quick lookup for the preferred venue for a symbol.

    Returns the venue string (e.g. "asterdex") without performing
    a full venue comparison. Used to set preferred_venue in Signal.data.
    """
    # All crypto perps go to Bybit
    return VENUE_BYBIT
