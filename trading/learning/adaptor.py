"""Strategy adaptor — suggests parameter changes based on trade performance.

v2: Adds apply_approved() to actually modify runtime config for approved adaptations.
"""

import logging

from trading.config import (
    LEARNING, MOMENTUM, MEAN_REVERSION, GOLD_BTC,
    RSI_DIVERGENCE, EMA_CROSSOVER, BOLLINGER_SQUEEZE,
    BTC_ETH_RATIO, TIPS_YIELD, FG_MULTI_TIMEFRAME,
)
from trading.db.store import (
    get_trades, insert_param_change, get_pending_adaptations, approve_adaptation,
)
from trading.learning.reviewer import metrics_by_strategy

log = logging.getLogger(__name__)

# Map strategy name → (config dict, param name → key in dict)
_STRATEGY_CONFIG_MAP = {
    "momentum": MOMENTUM,
    "mean_reversion": MEAN_REVERSION,
    "gold_btc": GOLD_BTC,
    "rsi_divergence": RSI_DIVERGENCE,
    "ema_crossover": EMA_CROSSOVER,
    "bollinger_squeeze": BOLLINGER_SQUEEZE,
    "btc_eth_ratio": BTC_ETH_RATIO,
    "tips_yield": TIPS_YIELD,
    "fg_multi_timeframe": FG_MULTI_TIMEFRAME,
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

    # --- Momentum strategy adaptations ---
    if "momentum" in by_strat and len(by_strat["momentum"]) >= min_trades:
        trades = by_strat["momentum"]
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            wins = [t for t in closed if t["pnl"] > 0]
            win_rate = len(wins) / len(closed)

            if win_rate < 0.40:
                new_threshold = MOMENTUM["entry_threshold"] + 0.02
                suggestions.append({
                    "strategy": "momentum",
                    "param": "entry_threshold",
                    "current": MOMENTUM["entry_threshold"],
                    "suggested": new_threshold,
                    "reason": f"Win rate {win_rate*100:.0f}% is below 40% over {len(closed)} trades. "
                              f"Widening entry threshold from {MOMENTUM['entry_threshold']*100:.0f}% to {new_threshold*100:.0f}% "
                              f"to be more selective.",
                })
                insert_param_change("momentum", "entry_threshold",
                                    MOMENTUM["entry_threshold"], new_threshold,
                                    f"Win rate dropped to {win_rate*100:.0f}% over {len(closed)} trades")

            if win_rate > 0.65:
                new_threshold = max(MOMENTUM["entry_threshold"] - 0.01, 0.02)
                suggestions.append({
                    "strategy": "momentum",
                    "param": "entry_threshold",
                    "current": MOMENTUM["entry_threshold"],
                    "suggested": new_threshold,
                    "reason": f"Win rate {win_rate*100:.0f}% is strong. Consider lowering entry threshold "
                              f"from {MOMENTUM['entry_threshold']*100:.0f}% to {new_threshold*100:.0f}% to catch more trades.",
                })

    # --- Mean reversion adaptations ---
    if "mean_reversion" in by_strat and len(by_strat["mean_reversion"]) >= min_trades:
        trades = by_strat["mean_reversion"]
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            fear_trades = [t for t in closed if t.get("side") == "buy"]
            if fear_trades:
                wins = [t for t in fear_trades if t["pnl"] > 0]
                win_rate = len(wins) / len(fear_trades)

                if win_rate < 0.45:
                    new_threshold = max(MEAN_REVERSION["fear_buy_threshold"] - 5, 10)
                    suggestions.append({
                        "strategy": "mean_reversion",
                        "param": "fear_buy_threshold",
                        "current": MEAN_REVERSION["fear_buy_threshold"],
                        "suggested": new_threshold,
                        "reason": f"Buy-side win rate {win_rate*100:.0f}% below 45%. "
                                  f"Tightening fear threshold from {MEAN_REVERSION['fear_buy_threshold']} to {new_threshold} "
                                  f"to only buy on more extreme fear.",
                    })
                    insert_param_change("mean_reversion", "fear_buy_threshold",
                                        MEAN_REVERSION["fear_buy_threshold"], new_threshold,
                                        f"Buy win rate at {win_rate*100:.0f}%")

    # --- Gold/BTC adaptations ---
    if "gold_btc" in by_strat and len(by_strat["gold_btc"]) >= min_trades:
        trades = by_strat["gold_btc"]
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            wins = [t for t in closed if t["pnl"] > 0]
            win_rate = len(wins) / len(closed)
            if win_rate < 0.40:
                new_threshold = GOLD_BTC["std_dev_threshold"] + 0.5
                suggestions.append({
                    "strategy": "gold_btc",
                    "param": "std_dev_threshold",
                    "current": GOLD_BTC["std_dev_threshold"],
                    "suggested": new_threshold,
                    "reason": f"Win rate {win_rate*100:.0f}% below 40%. "
                              f"Widening std dev threshold from {GOLD_BTC['std_dev_threshold']} to {new_threshold}.",
                })
                insert_param_change("gold_btc", "std_dev_threshold",
                                    GOLD_BTC["std_dev_threshold"], new_threshold,
                                    f"Win rate at {win_rate*100:.0f}%")

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

    # --- EMA Crossover adaptations ---
    if "ema_crossover" in by_strat and len(by_strat["ema_crossover"]) >= min_trades:
        trades = by_strat["ema_crossover"]
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            wins = [t for t in closed if t["pnl"] > 0]
            win_rate = len(wins) / len(closed)
            if win_rate < 0.40:
                new_slow = EMA_CROSSOVER["slow_period"] + 3
                suggestions.append({
                    "strategy": "ema_crossover",
                    "param": "slow_period",
                    "current": EMA_CROSSOVER["slow_period"],
                    "suggested": new_slow,
                    "reason": f"Win rate {win_rate*100:.0f}% below 40%. Widening slow EMA to {new_slow}.",
                })
                insert_param_change("ema_crossover", "slow_period",
                                    EMA_CROSSOVER["slow_period"], new_slow,
                                    f"Win rate at {win_rate*100:.0f}%")

    # --- Bollinger Squeeze adaptations ---
    if "bollinger_squeeze" in by_strat and len(by_strat["bollinger_squeeze"]) >= min_trades:
        trades = by_strat["bollinger_squeeze"]
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            wins = [t for t in closed if t["pnl"] > 0]
            win_rate = len(wins) / len(closed)
            if win_rate < 0.40:
                new_pctl = max(BOLLINGER_SQUEEZE["squeeze_percentile"] - 3, 3)
                suggestions.append({
                    "strategy": "bollinger_squeeze",
                    "param": "squeeze_percentile",
                    "current": BOLLINGER_SQUEEZE["squeeze_percentile"],
                    "suggested": new_pctl,
                    "reason": f"Win rate {win_rate*100:.0f}% below 40%. Tightening squeeze to {new_pctl}th pctl.",
                })
                insert_param_change("bollinger_squeeze", "squeeze_percentile",
                                    BOLLINGER_SQUEEZE["squeeze_percentile"], new_pctl,
                                    f"Win rate at {win_rate*100:.0f}%")

    # --- BTC/ETH Ratio adaptations ---
    if "btc_eth_ratio" in by_strat and len(by_strat["btc_eth_ratio"]) >= min_trades:
        trades = by_strat["btc_eth_ratio"]
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            wins = [t for t in closed if t["pnl"] > 0]
            win_rate = len(wins) / len(closed)
            if win_rate < 0.40:
                new_z = BTC_ETH_RATIO["entry_z"] + 0.5
                suggestions.append({
                    "strategy": "btc_eth_ratio",
                    "param": "entry_z",
                    "current": BTC_ETH_RATIO["entry_z"],
                    "suggested": new_z,
                    "reason": f"Win rate {win_rate*100:.0f}% below 40%. Widening entry z-score to {new_z}.",
                })
                insert_param_change("btc_eth_ratio", "entry_z",
                                    BTC_ETH_RATIO["entry_z"], new_z,
                                    f"Win rate at {win_rate*100:.0f}%")

    # --- TIPS Yield adaptations ---
    if "tips_yield" in by_strat and len(by_strat["tips_yield"]) >= min_trades:
        trades = by_strat["tips_yield"]
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            wins = [t for t in closed if t["pnl"] > 0]
            win_rate = len(wins) / len(closed)
            if win_rate < 0.40:
                new_z = TIPS_YIELD["z_threshold"] + 0.5
                suggestions.append({
                    "strategy": "tips_yield",
                    "param": "z_threshold",
                    "current": TIPS_YIELD["z_threshold"],
                    "suggested": new_z,
                    "reason": f"Win rate {win_rate*100:.0f}% below 40%. Widening z-threshold to {new_z}.",
                })
                insert_param_change("tips_yield", "z_threshold",
                                    TIPS_YIELD["z_threshold"], new_z,
                                    f"Win rate at {win_rate*100:.0f}%")

    # --- F&G Multi-Timeframe adaptations ---
    if "fg_multi_timeframe" in by_strat and len(by_strat["fg_multi_timeframe"]) >= min_trades:
        trades = by_strat["fg_multi_timeframe"]
        closed = [t for t in trades if t.get("pnl") is not None]
        if closed:
            buy_trades = [t for t in closed if t.get("side") == "buy"]
            if buy_trades:
                wins = [t for t in buy_trades if t["pnl"] > 0]
                win_rate = len(wins) / len(buy_trades)
                if win_rate < 0.45:
                    new_fear = max(FG_MULTI_TIMEFRAME["extreme_fear"] - 3, 5)
                    suggestions.append({
                        "strategy": "fg_multi_timeframe",
                        "param": "extreme_fear",
                        "current": FG_MULTI_TIMEFRAME["extreme_fear"],
                        "suggested": new_fear,
                        "reason": f"Buy-side win rate {win_rate*100:.0f}% below 45%. Tightening extreme fear to {new_fear}.",
                    })
                    insert_param_change("fg_multi_timeframe", "extreme_fear",
                                        FG_MULTI_TIMEFRAME["extreme_fear"], new_fear,
                                        f"Buy win rate at {win_rate*100:.0f}%")

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
        # Get approved but not yet applied (approved=1)
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
            # Apply: cast to match original type
            if isinstance(old_val, int):
                config_dict[param] = int(new_val)
            elif isinstance(old_val, float):
                config_dict[param] = float(new_val)
            else:
                config_dict[param] = new_val

            # Mark as applied (approved=2)
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
