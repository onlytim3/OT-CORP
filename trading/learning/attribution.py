"""Strategy P&L attribution - splits trade P&L by contributing strategy."""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def attribute_pnl(trade_id: int, pnl: float, contributors: list[str], strengths: list[float]):
    """Split P&L proportionally by signal strength across contributing strategies."""
    from trading.db.store import get_db
    if not contributors or not strengths:
        return
    total_strength = sum(abs(s) for s in strengths)
    if total_strength == 0:
        return
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        for strategy, strength in zip(contributors, strengths):
            weight = abs(strength) / total_strength
            attributed = pnl * weight
            conn.execute(
                "INSERT INTO strategy_attribution (timestamp, trade_id, strategy, attributed_pnl, strength_weight) VALUES (?,?,?,?,?)",
                (now, trade_id, strategy, round(attributed, 4), round(weight, 4))
            )
    logger.info(f"Attributed ${pnl:.2f} across {len(contributors)} strategies for trade {trade_id}")


def get_strategy_attribution(strategy: str = None, days: int = 30) -> list[dict]:
    """Get attribution data, optionally filtered by strategy."""
    from trading.db.store import get_db
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        if strategy:
            rows = conn.execute(
                "SELECT * FROM strategy_attribution WHERE strategy=? AND timestamp>=? ORDER BY timestamp DESC",
                (strategy, cutoff)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM strategy_attribution WHERE timestamp>=? ORDER BY timestamp DESC",
                (cutoff,)
            ).fetchall()
    return [dict(r) for r in rows]


def get_attribution_summary(days: int = 30) -> list[dict]:
    """Get summary attribution by strategy."""
    from trading.db.store import get_db
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT strategy, SUM(attributed_pnl) as total_pnl, COUNT(*) as trade_count, "
            "AVG(strength_weight) as avg_weight FROM strategy_attribution WHERE timestamp>=? "
            "GROUP BY strategy ORDER BY total_pnl DESC",
            (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]
