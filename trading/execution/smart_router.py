"""Smart order routing -- compare venues and select the best execution path.

Currently supports AsterDex (perpetual futures) as the primary venue.
Alpaca comparison is logged but AsterDex is always preferred for perps.
This module provides the foundation for multi-venue routing when additional
venues are integrated.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Venue constants
VENUE_ASTER = "asterdex"
VENUE_ALPACA = "alpaca"


def compare_venues(symbol: str) -> dict:
    """Compare execution quality across available venues for a symbol.

    Fetches bid/ask from AsterDex and (if available) Alpaca, computing
    spread and depth metrics for each.

    Args:
        symbol: Alpaca-style symbol (e.g. "BTC/USD").

    Returns:
        Dict with per-venue metrics: {venue: {bid, ask, spread_bps, depth}}.
    """
    result = {}

    # AsterDex (always available for crypto perps)
    try:
        from trading.execution.router import _to_aster
        from trading.execution.aster_client import get_aster_book_ticker

        aster_sym = _to_aster(symbol)
        book = get_aster_book_ticker(aster_sym)
        bid = float(book.get("bidPrice", 0))
        ask = float(book.get("askPrice", 0))
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0
        spread_bps = (ask - bid) / mid * 10000 if mid > 0 else 0

        result[VENUE_ASTER] = {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_bps": spread_bps,
            "bid_depth": float(book.get("bidQty", 0)),
            "ask_depth": float(book.get("askQty", 0)),
            "available": True,
        }
    except Exception as e:
        log.debug("AsterDex venue check failed for %s: %s", symbol, e)
        result[VENUE_ASTER] = {"available": False, "error": str(e)}

    # Alpaca (logged for comparison only -- not used for execution)
    result[VENUE_ALPACA] = {
        "available": False,
        "reason": "Alpaca perp routing not implemented -- AsterDex preferred",
    }

    # Log comparison
    if result[VENUE_ASTER].get("available"):
        aster_spread = result[VENUE_ASTER]["spread_bps"]
        log.debug(
            "Venue comparison for %s: AsterDex spread=%.1fbps",
            symbol, aster_spread,
        )

    return result


def route_order(symbol: str, side: str, qty: float) -> dict:
    """Determine the best venue for order execution.

    Current behavior: always routes to AsterDex for perpetual futures.
    Logs venue comparison for future optimization.

    Args:
        symbol: Trading symbol.
        side: 'buy' or 'sell'.
        qty: Order quantity.

    Returns:
        Dict with: venue (str), reason (str), venue_data (dict from compare_venues).
    """
    venues = compare_venues(symbol)

    # Current policy: AsterDex for all perps
    chosen = VENUE_ASTER
    reason = "AsterDex is the primary venue for perpetual futures"

    # Log the routing decision
    aster_data = venues.get(VENUE_ASTER, {})
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
    # All crypto perps go to AsterDex
    return VENUE_ASTER
