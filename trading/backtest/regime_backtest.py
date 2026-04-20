"""Regime routing backtest — validate regime multipliers against historical DB data.

Compares hypothetical Sharpe WITH vs WITHOUT regime routing applied to the
last N days of closed trades. Uses only existing SQLite data — no API calls.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Mirror of aggregator._REGIME_ROUTING type_mult tables (read-only snapshot)
_ROUTING_SNAPSHOT = {
    "strongly bullish": {"momentum": 1.30, "mean_reversion": 0.65, "microstructure": 1.10,
                          "fundamental": 0.80, "cross_asset": 1.00, "statistical_arb": 0.90},
    "bullish":          {"momentum": 1.15, "mean_reversion": 0.80, "microstructure": 1.05,
                          "fundamental": 0.90, "cross_asset": 1.00, "statistical_arb": 0.95},
    "neutral":          {"momentum": 1.00, "mean_reversion": 1.00, "microstructure": 1.00,
                          "fundamental": 1.00, "cross_asset": 1.00, "statistical_arb": 1.00},
    "bearish":          {"momentum": 0.70, "mean_reversion": 1.25, "microstructure": 0.95,
                          "fundamental": 1.10, "cross_asset": 0.90, "statistical_arb": 1.10},
    "strongly bearish": {"momentum": 0.50, "mean_reversion": 1.40, "microstructure": 0.85,
                          "fundamental": 1.20, "cross_asset": 0.80, "statistical_arb": 1.20},
}

_STRATEGY_TYPE_MAP = {
    "kalman_trend": "momentum", "meme_momentum": "momentum", "taker_divergence": "momentum",
    "hmm_regime": "momentum", "cross_asset_momentum": "momentum",
    "rsi_divergence": "mean_reversion", "regime_mean_reversion": "mean_reversion",
    "basis_zscore": "mean_reversion", "gold_crypto_hedge": "mean_reversion",
    "microstructure_composite": "microstructure", "whale_flow": "microstructure",
    "oi_price_divergence": "microstructure", "funding_arb": "microstructure",
    "funding_term_structure": "microstructure",
    "multi_factor_rank": "fundamental", "factor_crypto": "fundamental",
    "news_sentiment": "fundamental", "onchain_flow": "fundamental",
    "pairs_trading": "statistical_arb", "cross_basis_rv": "statistical_arb",
}


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return 0.0
    return round(mean / std * math.sqrt(252), 3)


def _max_drawdown(returns: list[float]) -> float:
    peak = 0.0
    max_dd = 0.0
    equity = 0.0
    for r in returns:
        equity += r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 4)


def _infer_regime_at(timestamp: str, score_history: list[dict]) -> str:
    """Approximate regime at `timestamp` using closest daily_pnl overall_score entry."""
    if not score_history:
        return "neutral"
    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    best = min(score_history, key=lambda r: abs(
        (datetime.fromisoformat(r["date"].replace("Z", "+00:00")) - ts).total_seconds()
    ))
    score = best.get("score", 0.0) or 0.0
    if score >= 0.5:
        return "strongly bullish"
    if score >= 0.2:
        return "bullish"
    if score <= -0.5:
        return "strongly bearish"
    if score <= -0.2:
        return "bearish"
    return "neutral"


def run_regime_backtest(days: int = 90) -> dict:
    """Simulate regime routing impact on the last `days` of closed trades.

    For each closed trade:
    - Looks up the inferred regime at trade-open time (using daily_pnl scores)
    - Computes what position size multiplier regime routing would have applied
    - Scales PnL proportionally to get hypothetical routed PnL

    Returns per-strategy-type Sharpe comparison and aggregate stats.
    """
    from trading.db.store import get_db

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with get_db() as conn:
        trades = conn.execute(
            "SELECT strategy, pnl, timestamp FROM trades "
            "WHERE status='closed' AND pnl IS NOT NULL AND timestamp >= ? "
            "ORDER BY timestamp ASC",
            (cutoff,),
        ).fetchall()

        # Load daily score proxy for regime inference
        try:
            score_rows = conn.execute(
                "SELECT date, data FROM daily_pnl WHERE date >= ? ORDER BY date ASC",
                (cutoff[:10],),
            ).fetchall()
        except Exception:
            score_rows = []

    score_history = []
    for r in score_rows:
        try:
            d = json.loads(r["data"]) if r["data"] else {}
            score_history.append({"date": r["date"], "score": d.get("overall_score", 0.0)})
        except Exception:
            pass

    if not trades:
        return {"error": "No closed trades in the last {} days".format(days), "days": days}

    # Group PnL by strategy type
    by_type: dict[str, dict] = {}
    for t in trades:
        strat = t["strategy"] or "unknown"
        stype = _STRATEGY_TYPE_MAP.get(strat, "fundamental")
        regime = _infer_regime_at(t["timestamp"], score_history)
        routing = _ROUTING_SNAPSHOT.get(regime, _ROUTING_SNAPSHOT["neutral"])
        mult = routing.get(stype, 1.0)
        pnl = t["pnl"]

        if stype not in by_type:
            by_type[stype] = {"actual": [], "routed": [], "trades": 0}
        by_type[stype]["actual"].append(pnl)
        by_type[stype]["routed"].append(pnl * mult)
        by_type[stype]["trades"] += 1

    comparison = []
    total_actual = []
    total_routed = []
    for stype, data in sorted(by_type.items()):
        actual_sharpe = _sharpe(data["actual"])
        routed_sharpe = _sharpe(data["routed"])
        delta_pct = round((routed_sharpe - actual_sharpe) / max(abs(actual_sharpe), 0.01) * 100, 1)
        comparison.append({
            "strategy_type": stype,
            "trades": data["trades"],
            "sharpe_without_routing": actual_sharpe,
            "sharpe_with_routing": routed_sharpe,
            "delta_sharpe_pct": delta_pct,
            "max_dd_without": _max_drawdown(data["actual"]),
            "max_dd_with": _max_drawdown(data["routed"]),
        })
        total_actual.extend(data["actual"])
        total_routed.extend(data["routed"])

    aggregate = {
        "total_trades": len(trades),
        "days": days,
        "sharpe_without_routing": _sharpe(total_actual),
        "sharpe_with_routing": _sharpe(total_routed),
        "max_dd_without": _max_drawdown(total_actual),
        "max_dd_with": _max_drawdown(total_routed),
        "win_rate_without": round(sum(1 for p in total_actual if p > 0) / len(total_actual), 3),
        "win_rate_with": round(sum(1 for p in total_routed if p > 0) / len(total_routed), 3),
    }

    return {
        "aggregate": aggregate,
        "by_type": comparison,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
