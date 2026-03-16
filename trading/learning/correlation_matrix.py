"""Strategy correlation tracking and alerting."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def get_signal_history(days: int = 30) -> dict[str, list[int]]:
    """Get signal direction history per strategy.

    Maps each strategy to a list of directional values:
    +1 for BUY/buy, -1 for SELL/sell, 0 for HOLD/other.
    """
    from trading.db.store import get_db

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT strategy, signal, timestamp FROM signals "
            "WHERE timestamp >= ? ORDER BY timestamp",
            (cutoff,),
        ).fetchall()

    history: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        action = r["signal"].lower() if r["signal"] else ""
        direction = 1 if action == "buy" else (-1 if action == "sell" else 0)
        history[r["strategy"]].append(direction)
    return dict(history)


def compute_correlation_matrix(days: int = 30) -> dict[str, dict[str, float]]:
    """Compute pairwise Pearson correlation between strategy signals.

    Returns nested dict: matrix[strategy_a][strategy_b] = correlation.
    Requires at least 5 overlapping signals per pair.
    """
    history = get_signal_history(days)
    strategies = list(history.keys())
    matrix: dict[str, dict[str, float]] = {}

    for i, s1 in enumerate(strategies):
        matrix[s1] = {}
        for j, s2 in enumerate(strategies):
            if i == j:
                matrix[s1][s2] = 1.0
                continue

            v1 = history[s1]
            v2 = history[s2]
            min_len = min(len(v1), len(v2))
            if min_len < 5:
                matrix[s1][s2] = 0.0
                continue

            # Use the most recent min_len entries from each
            a = v1[-min_len:]
            b = v2[-min_len:]
            mean_a = sum(a) / min_len
            mean_b = sum(b) / min_len

            num = sum((a[k] - mean_a) * (b[k] - mean_b) for k in range(min_len))
            den_a = sum((a[k] - mean_a) ** 2 for k in range(min_len)) ** 0.5
            den_b = sum((b[k] - mean_b) ** 2 for k in range(min_len)) ** 0.5

            if den_a * den_b == 0:
                matrix[s1][s2] = 0.0
            else:
                matrix[s1][s2] = round(num / (den_a * den_b), 3)

    return matrix


def check_correlation_alerts(matrix: dict[str, dict[str, float]]) -> list[str]:
    """Alert when strategies from different correlation groups correlate > 0.8.

    This indicates hidden concentration risk -- strategies that should
    diversify are actually moving together.
    """
    alerts: list[str] = []
    try:
        from trading.risk.manager import CORRELATION_GROUPS
    except ImportError:
        return alerts

    # Build strategy -> group mapping
    strat_group: dict[str, str] = {}
    for group, strats in CORRELATION_GROUPS.items():
        for s in strats:
            strat_group[s] = group

    seen: set[tuple[str, str]] = set()
    for s1, row in matrix.items():
        for s2, corr in row.items():
            if s1 == s2:
                continue
            pair = tuple(sorted([s1, s2]))
            if pair in seen:
                continue
            seen.add(pair)

            g1 = strat_group.get(s1, s1)
            g2 = strat_group.get(s2, s2)
            if g1 != g2 and abs(corr) > 0.8:
                alerts.append(
                    f"High correlation ({corr:.2f}) between "
                    f"{s1} ({g1}) and {s2} ({g2})"
                )

    return alerts
