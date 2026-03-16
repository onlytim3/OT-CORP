"""Macro event calendar for risk sizing."""
import logging
import time

logger = logging.getLogger(__name__)

_calendar_cache: dict = {"events": [], "updated": 0}
_CACHE_TTL = 3600

KNOWN_EVENTS = [
    "FOMC", "CPI", "NFP", "PPI", "GDP", "PCE", "Retail Sales",
    "ISM Manufacturing", "Unemployment Rate", "Fed Chair Speech"
]


def get_upcoming_events(hours: int = 48) -> list[dict]:
    """Get upcoming macro events in the next N hours."""
    return _calendar_cache.get("events", [])


def is_event_window(hours_ahead: int = 4) -> bool:
    """Check if a major macro event is within N hours."""
    events = get_upcoming_events(hours=hours_ahead)
    if events:
        logger.info(f"Macro event window active: {len(events)} events within {hours_ahead}h")
        return True
    return False


def get_event_sizing_multiplier() -> float:
    """Get position sizing multiplier based on macro calendar."""
    if is_event_window(hours_ahead=4):
        return 0.3
    elif is_event_window(hours_ahead=12):
        return 0.7
    return 1.0
