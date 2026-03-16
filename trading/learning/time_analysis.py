"""Time-of-day P&L attribution for optimal trading windows."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def get_hourly_pnl(days: int = 30) -> dict[int, float]:
    """Get P&L by hour of day (0-23 UTC)."""
    from trading.db.store import get_db

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT timestamp, pnl FROM trades "
            "WHERE timestamp >= ? AND pnl IS NOT NULL",
            (cutoff,),
        ).fetchall()

    hourly: dict[int, float] = defaultdict(float)
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            hourly[ts.hour] += float(r["pnl"] or 0)
        except Exception:
            continue
    return dict(hourly)


def get_daily_pnl_by_dow(days: int = 90) -> dict[int, float]:
    """Get P&L by day of week (0=Monday, 6=Sunday)."""
    from trading.db.store import get_db

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT timestamp, pnl FROM trades "
            "WHERE timestamp >= ? AND pnl IS NOT NULL",
            (cutoff,),
        ).fetchall()

    daily: dict[int, float] = defaultdict(float)
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            daily[ts.weekday()] += float(r["pnl"] or 0)
        except Exception:
            continue
    return dict(daily)


def get_time_sizing_multiplier() -> float:
    """Get sizing multiplier based on current time performance.

    Losing time windows get 0.6x sizing to reduce exposure
    during historically unprofitable hours.
    """
    hourly = get_hourly_pnl(days=30)
    current_hour = datetime.now(timezone.utc).hour
    if not hourly:
        return 1.0
    hour_pnl = hourly.get(current_hour, 0)
    if hour_pnl < 0:
        return 0.6
    return 1.0
