"""
Market hours checker for US equity/ETF trading.

Determines whether NYSE is currently open so the execution layer can gate
ETF order submission while still allowing 24/7 crypto trades.

No external dependencies -- uses only stdlib datetime and zoneinfo.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

NYSE_OPEN = time(9, 30)   # 9:30 AM Eastern
NYSE_CLOSE = time(16, 0)  # 4:00 PM Eastern

# NYSE holidays for 2026.
# Sources: NYSE Rule 7.2 and historical calendar.
US_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),   # New Year's Day
        date(2026, 1, 19),  # Martin Luther King Jr. Day (3rd Monday of Jan)
        date(2026, 2, 16),  # Presidents' Day (3rd Monday of Feb)
        date(2026, 4, 3),   # Good Friday
        date(2026, 5, 25),  # Memorial Day (last Monday of May)
        date(2026, 6, 19),  # Juneteenth National Independence Day
        date(2026, 7, 3),   # Independence Day observed (Jul 4 is Saturday)
        date(2026, 9, 7),   # Labor Day (1st Monday of Sep)
        date(2026, 11, 26), # Thanksgiving Day (4th Thursday of Nov)
        date(2026, 12, 25), # Christmas Day
    }
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_market_open(now: datetime | None = None) -> bool:
    """Return True if NYSE is currently in a regular trading session.

    Parameters
    ----------
    now : datetime, optional
        Override the current time (useful for testing). Must be
        timezone-aware. Defaults to ``datetime.now(UTC)``.
    """
    if now is None:
        now = datetime.now(UTC)

    now_et = now.astimezone(ET)

    # Weekend check (Monday=0 .. Sunday=6).
    if now_et.weekday() >= 5:
        return False

    # Holiday check.
    if now_et.date() in US_HOLIDAYS_2026:
        return False

    # Regular session window.
    current_time = now_et.time()
    return NYSE_OPEN <= current_time < NYSE_CLOSE


def is_crypto_symbol(symbol: str) -> bool:
    """Return True if *symbol* looks like a crypto pair (contains ``/``).

    Examples: ``"BTC/USD"``, ``"ETH/USDT"``.
    """
    return "/" in symbol


def is_etf_symbol(symbol: str) -> bool:
    """Return True if *symbol* is a plain ticker with no slash.

    Plain tickers are assumed to be equities or ETFs that trade on NYSE/
    NASDAQ and are therefore subject to market-hours constraints.

    Examples: ``"UGL"``, ``"AGQ"``, ``"SPY"``.
    """
    return "/" not in symbol


def can_trade_now(symbol: str, now: datetime | None = None) -> tuple[bool, str]:
    """Decide whether an order for *symbol* may be submitted right now.

    Returns
    -------
    (allowed, reason) : tuple[bool, str]
        *allowed* is ``True`` when the order can be sent immediately.
        When ``False``, the caller should queue the order rather than
        submit it -- but signal generation should still proceed.
    """
    if is_crypto_symbol(symbol):
        return True, "Crypto markets are 24/7"

    if is_market_open(now):
        return True, "NYSE is open"

    return (
        False,
        "NYSE is closed \u2014 ETF orders will queue until next open at 9:30 AM ET",
    )


def next_market_open(now: datetime | None = None) -> datetime:
    """Return the next NYSE opening bell as a UTC-aware datetime.

    If the market is currently open the *next* open (i.e. tomorrow or the
    next valid trading day) is returned -- not the current session's open.

    Parameters
    ----------
    now : datetime, optional
        Override the current time (must be timezone-aware).
    """
    if now is None:
        now = datetime.now(UTC)

    now_et = now.astimezone(ET)

    # Start searching from the next calendar day if we are already past
    # today's open, or from today if we have not yet reached 9:30 AM ET.
    candidate = now_et.date()
    if now_et.time() >= NYSE_OPEN:
        candidate += timedelta(days=1)

    # Walk forward until we land on a valid trading day.
    for _ in range(10):  # At most a long-weekend + holidays span.
        if candidate.weekday() < 5 and candidate not in US_HOLIDAYS_2026:
            open_et = datetime.combine(candidate, NYSE_OPEN, tzinfo=ET)
            return open_et.astimezone(UTC)
        candidate += timedelta(days=1)

    # Fallback -- should never be reached with a 10-day lookahead.
    raise RuntimeError("Could not determine next market open within 10 days")
