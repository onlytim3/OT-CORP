"""Strategy adaptor — suggests parameter changes based on trade performance.

v4: Adaptation logic for rsi_divergence and dxy_dollar. New strategies
    (hmm_regime, pairs_trading, kalman_trend, etc.) use local configs and
    will be added to adaptation as they accumulate trade history.
"""

import logging

from trading.config import LEARNING, RSI_DIVERGENCE, DXY_DOLLAR
from trading.db.store import (
    get_trades, insert_param_change, get_pending_adaptations, approve_adaptation,
)
from trading.learning.reviewer import metrics_by_strategy

log = logging.getLogger(__name__)

# Map strategy name → config dict
_STRATEGY_CONFIG_MAP = {
    "rsi_divergence": RSI_DIVERGENCE,
    "dxy_dollar": DXY_DOLLAR,
}


def analyze_and_suggest() -> list[dict]:
    """Analyze trade history and suggest parameter adjustments.

    Only suggests changes when there are enough trades (min_trades_for_adaptation).
    Returns list of suggestions with reasoning.
    """
    min_trades = LEARNING["min_trades_for_adaptation"]
    all_trades = get_trades(limit=500)
    by_strat = {}
    for trade in all_trades:
        strat = trade.get("strategy", "unknown")
        if strat not in by_strat:
            by_strat[strat] = []
        by_strat[strat].append(trade)

    suggestions = []

    # --- RSI Divergence adaptations ---
    if "rsi_divergence" in by_strat and len(by_strat["rsi_divergence"]) >= min_trades:
        trades = by_strat["rsi_divergence"]
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            wins = [t for t in closed if t["pnl"] > 0]
            win_rate = len(wins) / len(closed)
            if win_rate < 0.40:
                new_period = RSI_DIVERGENCE["rsi_period"] + 2
                suggestions.append({
                    "strategy": "rsi_divergence",
                    "param": "rsi_period",
                    "current": RSI_DIVERGENCE["rsi_period"],
                    "suggested": new_period,
                    "reason": f"Win rate {win_rate*100:.0f}% below 40%. Lengthening RSI period to {new_period}.",
                })
                insert_param_change("rsi_divergence", "rsi_period",
                                    RSI_DIVERGENCE["rsi_period"], new_period,
                                    f"Win rate at {win_rate*100:.0f}%")

    # --- DXY Dollar adaptations ---
    if "dxy_dollar" in by_strat and len(by_strat["dxy_dollar"]) >= min_trades:
        trades = by_strat["dxy_dollar"]
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            wins = [t for t in closed if t["pnl"] > 0]
            win_rate = len(wins) / len(closed)
            if win_rate < 0.40:
                new_slow = DXY_DOLLAR["sma_slow"] + 10
                suggestions.append({
                    "strategy": "dxy_dollar",
                    "param": "sma_slow",
                    "current": DXY_DOLLAR["sma_slow"],
                    "suggested": new_slow,
                    "reason": f"Win rate {win_rate*100:.0f}% below 40%. Widening slow SMA to {new_slow}.",
                })
                insert_param_change("dxy_dollar", "sma_slow",
                                    DXY_DOLLAR["sma_slow"], new_slow,
                                    f"Win rate at {win_rate*100:.0f}%")

    return suggestions


def get_pending() -> list[dict]:
    """Get all pending (unapproved) parameter change suggestions."""
    return get_pending_adaptations()


def approve(param_id: int):
    """Approve a parameter change."""
    approve_adaptation(param_id)


def apply_approved() -> list[dict]:
    """Apply all approved-but-not-yet-applied adaptations to runtime config.

    Reads param_history where approved=1, mutates the in-memory config dicts,
    and marks them as applied (approved=2) so they aren't re-applied.

    Returns list of applied changes for logging.
    """
    from trading.db.store import get_db

    applied = []

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM param_history WHERE approved=1 ORDER BY timestamp ASC"
        ).fetchall()

        for row in rows:
            row_dict = dict(row)
            strategy = row_dict["strategy"]
            param = row_dict["param_name"]
            new_val = row_dict["new_value"]

            config_dict = _STRATEGY_CONFIG_MAP.get(strategy)
            if config_dict is None:
                log.warning("Unknown strategy '%s' in adaptation — skipping", strategy)
                continue

            if param not in config_dict:
                log.warning("Unknown param '%s' for strategy '%s' — skipping", param, strategy)
                continue

            old_val = config_dict[param]
            if isinstance(old_val, int):
                config_dict[param] = int(new_val)
            elif isinstance(old_val, float):
                config_dict[param] = float(new_val)
            else:
                config_dict[param] = new_val

            conn.execute(
                "UPDATE param_history SET approved=2 WHERE id=?",
                (row_dict["id"],),
            )

            applied.append({
                "strategy": strategy,
                "param": param,
                "old": old_val,
                "new": config_dict[param],
                "reason": row_dict.get("reason", ""),
            })
            log.info(
                "Applied adaptation: %s.%s = %s -> %s (%s)",
                strategy, param, old_val, config_dict[param], row_dict.get("reason", ""),
            )

    return applied
