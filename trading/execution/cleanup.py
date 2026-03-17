"""Close dust positions below minimum USD threshold."""
import logging
log = logging.getLogger(__name__)

def close_dust_positions(threshold_usd: float = 5.0):
    """Close positions below a minimum USD threshold."""
    from trading.execution.router import _get_positions
    from trading.db.store import log_action

    positions = _get_positions()
    if not positions:
        return []

    closed = []
    for pos in positions:
        value = abs(pos.get("market_value", 0))
        if 0 < value < threshold_usd:
            try:
                # Import here to avoid circular imports
                from trading.execution.router import close_position
                result = close_position(pos["symbol"])
                closed.append(pos["symbol"])
                log_action("cleanup", "close_dust",
                          symbol=pos["symbol"],
                          details=f"Closed dust position: ${value:.2f}")
                log.info("Closed dust position %s: $%.2f", pos["symbol"], value)
            except Exception as e:
                log_action("error", "dust_cleanup_failed",
                          symbol=pos["symbol"], details=str(e))
                log.warning("Failed to close dust %s: %s", pos["symbol"], e)

    if closed:
        log.info("Dust cleanup: closed %d positions: %s", len(closed), ", ".join(closed))
    return closed
