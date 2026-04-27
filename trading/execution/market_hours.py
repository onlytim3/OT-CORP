"""
Market hours checker for US equity/ETF trading.

Determines whether NYSE is currently open so the execution layer can gate
ETF order submission while still allowing 24/7 crypto trades.

No external dependencies -- uses only stdlib datetime and zoneinfo.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

NYSE_OPEN = time(9, 30)   # 9:30 AM Eastern
NYSE_CLOSE = time(16, 0)  # 4:00 PM Eastern

# Per-year cache so holidays are computed at most once per year.
_holidays_cache: dict[int, set[date]] = {}


# ---------------------------------------------------------------------------
# Holiday computation (NYSE Rule 7.2)
# ---------------------------------------------------------------------------


def _compute_easter(year: int) -> date:
    """Return Easter Sunday for *year* using the Anonymous Gregorian algorithm.

    Reference: Meeus, *Astronomical Algorithms*, Chapter 9.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observe(d: date) -> date:
    """Shift a holiday to observed date per NYSE rules.

    If the holiday falls on Saturday it is observed on the preceding Friday.
    If it falls on Sunday it is observed on the following Monday.
    """
    wd = d.weekday()  # Mon=0 .. Sun=6
    if wd == 5:       # Saturday -> Friday
        return d - timedelta(days=1)
    if wd == 6:       # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the *n*-th occurrence of *weekday* in the given month.

    *weekday* follows Python convention (Monday=0 .. Sunday=6).
    *n* is 1-based (1 = first, 2 = second, ...).
    """
    first = date(year, month, 1)
    # Days until the first target weekday in the month.
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of *weekday* in the given month."""
    # Start from the last day of the month.
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def get_nyse_holidays(year: int) -> set[date]:
    """Compute and return the set of NYSE market holidays for *year*.

    Results are cached so repeated calls for the same year are free.
    """
    if year in _holidays_cache:
        return _holidays_cache[year]

    holidays: set[date] = set()

    # New Year's Day -- Jan 1 (observed Mon if Sun; Fri if Sat).
    holidays.add(_observe(date(year, 1, 1)))

    # Martin Luther King Jr. Day -- 3rd Monday of January.
    holidays.add(_nth_weekday(year, 1, 0, 3))

    # Presidents' Day -- 3rd Monday of February.
    holidays.add(_nth_weekday(year, 2, 0, 3))

    # Good Friday -- Friday before Easter Sunday.
    easter = _compute_easter(year)
    holidays.add(easter - timedelta(days=2))

    # Memorial Day -- last Monday of May.
    holidays.add(_last_weekday(year, 5, 0))

    # Juneteenth -- June 19 (observed).
    holidays.add(_observe(date(year, 6, 19)))

    # Independence Day -- July 4 (observed).
    holidays.add(_observe(date(year, 7, 4)))

    # Labor Day -- 1st Monday of September.
    holidays.add(_nth_weekday(year, 9, 0, 1))

    # Thanksgiving Day -- 4th Thursday of November.
    holidays.add(_nth_weekday(year, 11, 3, 4))

    # Christmas Day -- Dec 25 (observed).
    holidays.add(_observe(date(year, 12, 25)))

    _holidays_cache[year] = holidays
    return holidays


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def _holidays_for_date(d: date) -> set[date]:
    """Return NYSE holidays for the year of *d*, with a warning for far-future dates."""
    today = date.today()
    if d.year > today.year + 1:
        logger.warning(
            "Querying NYSE holidays for %d, which is more than 1 year in the "
            "future. Computed holidays may be less reliable if the NYSE "
            "calendar changes.",
            d.year,
        )
    return get_nyse_holidays(d.year)


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
    if now_et.date() in _holidays_for_date(now_et.date()):
        return False

    # Regular session window.
    current_time = now_et.time()
    return NYSE_OPEN <= current_time < NYSE_CLOSE


_CRYPTO_SUFFIXES = ("USDT", "USDC", "BUSD", "BTC", "ETH", "USD")


def is_crypto_symbol(symbol: str) -> bool:
    """Return True if *symbol* is a crypto pair.

    Recognises both slash format (``"BTC/USD"``) and Bybit concatenated
    format (``"BTCUSDT"``, ``"BNBUSDT"``, ``"SOLUSDT"``).
    """
    if "/" in symbol:
        return True
    upper = symbol.upper()
    return any(upper.endswith(s) for s in _CRYPTO_SUFFIXES) and len(upper) > len(max(_CRYPTO_SUFFIXES, key=len))


def is_etf_symbol(symbol: str) -> bool:
    """Return True if *symbol* is a plain equity/ETF ticker (not crypto).

    Examples: ``"UGL"``, ``"AGQ"``, ``"SPY"``.
    """
    return not is_crypto_symbol(symbol)


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
        if candidate.weekday() < 5 and candidate not in _holidays_for_date(candidate):
            open_et = datetime.combine(candidate, NYSE_OPEN, tzinfo=ET)
            return open_et.astimezone(UTC)
        candidate += timedelta(days=1)

    # Fallback -- should never be reached with a 10-day lookahead.
    raise RuntimeError("Could not determine next market open within 10 days")
