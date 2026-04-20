"""Autonomous Improvement Engine — self-evolving trading system.

Agents continuously converse with each other through recommendations:
  - Performance Agent:  analyzes trade results, flags underperformers
  - Research Agent:     identifies strategy gaps, suggests new builds
  - Risk Agent:         monitors drawdown/exposure, adjusts allocation
  - Regime Agent:       detects regime shifts, recommends strategy switching
  - Learning Agent:     synthesizes lessons, updates knowledge base

All actions are auto-applied with full autonomy. The executor ensures
work is actually done — not just logged:

Immediate execution:
  - Disable a strategy losing money (win rate < 25% over 20+ trades)
  - Shift allocation away from underperformers
  - Tighten risk parameters during drawdown
  - Reduce sizing during event risk
  - Trim budgets on concentrated positions
  - Re-enable backtest-approved strategies during underinvestment
  - Build strategy code for implementation requests
  - Fill coverage gaps with auto-generated strategies
  - Queue disabled strategies for re-evaluation via backtest

Verification (each cycle):
  - Check previous cycle's actions actually took effect
  - Re-apply failed actions with escalation tracking
  - Log verification failures to activity log

Usage:
    from trading.intelligence.autonomous import run_autonomous_cycle
    results = run_autonomous_cycle()
"""

import json
import logging
from datetime import datetime, timezone, timedelta

from trading.config import RISK, STRATEGY_ENABLED, KNOWLEDGE_DIR
from trading.db.store import (
    get_trades, get_daily_pnl, get_positions, log_action,
    insert_recommendation, get_pending_recommendations,
    resolve_recommendation, get_recommendation_history,
    insert_knowledge, insert_backtest_result, get_last_backtest,
    get_strategies_needing_backtest,
    get_setting, set_setting,
)
from trading.intelligence.filter import is_recommendation_safe, guardrail, refresh_guardrail

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Adaptive Threshold System — thresholds the Learning Agent can tune
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLDS = {
    "auto_disable_win_rate": 0.25,       # Disable strategy if win rate below this
    "auto_disable_min_trades": 20,       # Minimum trades before auto-disabling
    "auto_disable_max_loss_pct": -0.15,  # Disable if total loss > this % of capital
    "drawdown_tighten_threshold": 0.10,  # Tighten risk at this drawdown
    "drawdown_halt_threshold": 0.18,     # Emergency halt at this drawdown
    "backtest_adopt_win_rate": 0.60,     # Auto-enable if win rate >= this
    "backtest_adopt_sharpe": 0.3,        # Minimum Sharpe to adopt
    "backtest_adopt_max_dd": 0.20,       # Max drawdown to still adopt
    "backtest_discard_win_rate": 0.30,   # Discard if win rate < this
    "backtest_discard_max_dd": 0.30,     # Discard if drawdown > this
    "concentration_max_pct": 0.25,       # Trim budget when position > this
    "event_risk_sizing_mult": 0.6,       # Reduce sizing during event risk
    "recovery_buffer_pct": 0.05,         # Drawdown must drop this far below halt to start recovery
    "recovery_max_strategies_per_cycle": 2,  # Max strategies to re-enable per recovery cycle
}

_THRESHOLD_BOUNDS = {
    "auto_disable_win_rate": (0.15, 0.50),
    "auto_disable_min_trades": (10, 50),
    "auto_disable_max_loss_pct": (-0.30, -0.05),
    "drawdown_tighten_threshold": (0.05, 0.20),
    "drawdown_halt_threshold": (0.12, 0.30),
    "backtest_adopt_win_rate": (0.40, 0.80),
    "backtest_adopt_sharpe": (0.1, 1.0),
    "backtest_adopt_max_dd": (0.10, 0.35),
    "backtest_discard_win_rate": (0.15, 0.50),
    "backtest_discard_max_dd": (0.15, 0.50),
    "concentration_max_pct": (0.10, 0.40),
    "event_risk_sizing_mult": (0.3, 0.9),
    "recovery_buffer_pct": (0.02, 0.10),
    "recovery_max_strategies_per_cycle": (1, 5),
}

MAX_THRESHOLD_ADJUSTMENT = 0.05  # ±5% max change per cycle

# Runtime cache for threshold overrides (populated by set_threshold)
_threshold_overrides: dict[str, float] = {}


def get_threshold(name: str) -> float:
    """Get a tunable threshold value. Checks runtime overrides, then defaults."""
    if name in _threshold_overrides:
        return _threshold_overrides[name]
    return _DEFAULT_THRESHOLDS.get(name, 0)


def set_threshold(name: str, value: float) -> float:
    """Set a threshold with bounds clamping and max-delta enforcement.

    Returns the actual value after clamping.
    """
    if name not in _DEFAULT_THRESHOLDS:
        log.warning("Unknown threshold: %s", name)
        return value

    # Clamp to bounds
    lo, hi = _THRESHOLD_BOUNDS.get(name, (value, value))
    clamped = max(lo, min(hi, value))

    # Enforce max delta per cycle (±5% of current value)
    current = get_threshold(name)
    if current != 0:
        max_delta = abs(current) * MAX_THRESHOLD_ADJUSTMENT
        delta = clamped - current
        if abs(delta) > max_delta:
            clamped = current + (max_delta if delta > 0 else -max_delta)
        clamped = max(lo, min(hi, clamped))  # Re-clamp after delta cap

    # Warn when requested value was clamped to the same as current (at bound)
    if round(value, 4) != round(current, 4) and clamped == round(current, 4):
        log.warning(
            "Threshold '%s' at bound limit (%.4f). Requested %.4f -> clamped to %.4f (no change).",
            name, current, value, clamped,
        )

    clamped = round(clamped, 4)
    _threshold_overrides[name] = clamped
    log_action(
        "autonomous", "threshold_adjusted",
        details=f"{name}: {current} → {clamped}",
        data={"threshold": name, "old": current, "new": clamped},
    )
    # Persist threshold to DB so it survives restarts
    _persist_thresholds()
    return clamped


# ---------------------------------------------------------------------------
# State persistence — survive process restarts / deploys
# ---------------------------------------------------------------------------

_DB_KEY_THRESHOLDS = "autonomous_thresholds"
_DB_KEY_STRATEGY_ENABLED = "autonomous_strategy_enabled"
_DB_KEY_STRATEGY_BUDGETS = "autonomous_strategy_budgets"
_DB_KEY_RISK_OVERRIDES = "autonomous_risk_overrides"

# Track which RISK keys were changed by autonomous (vs. config defaults)
_risk_overrides: dict[str, float] = {}

# Snapshot of original RISK values from config.py at import time.
# Used by revert_risk to restore parameters after drawdown recovery.
_RISK_DEFAULTS = dict(RISK)


def _persist_thresholds():
    """Save current threshold overrides to DB."""
    try:
        if _threshold_overrides:
            set_setting(_DB_KEY_THRESHOLDS, json.dumps(_threshold_overrides))
    except Exception:
        log.debug("Failed to persist thresholds", exc_info=True)


def _persist_strategy_enabled():
    """Save autonomous strategy enable/disable overrides to DB."""
    try:
        set_setting(_DB_KEY_STRATEGY_ENABLED, json.dumps(
            {k: v for k, v in STRATEGY_ENABLED.items()}
        ))
    except Exception:
        log.debug("Failed to persist strategy enabled state", exc_info=True)


def _persist_strategy_budgets():
    """Save strategy budget overrides to DB."""
    try:
        from trading.risk.portfolio import STRATEGY_BUDGETS
        set_setting(_DB_KEY_STRATEGY_BUDGETS, json.dumps(
            {k: round(v, 6) for k, v in STRATEGY_BUDGETS.items()}
        ))
    except Exception:
        log.debug("Failed to persist strategy budgets", exc_info=True)


def _persist_risk_overrides():
    """Save risk parameter overrides to DB."""
    try:
        if _risk_overrides:
            set_setting(_DB_KEY_RISK_OVERRIDES, json.dumps(
                {k: round(v, 6) for k, v in _risk_overrides.items()}
            ))
    except Exception:
        log.debug("Failed to persist risk overrides", exc_info=True)


def load_persisted_state():
    """Load autonomous state from DB, applying overrides to runtime dicts.

    Call this on startup (e.g. from scheduler or daemon) so that autonomous
    changes survive process restarts and Render deploys.
    """
    loaded = []

    # 1. Thresholds
    try:
        raw = get_setting(_DB_KEY_THRESHOLDS)
        if raw:
            saved = json.loads(raw)
            for k, v in saved.items():
                if k in _DEFAULT_THRESHOLDS:
                    _threshold_overrides[k] = float(v)
            if saved:
                loaded.append(f"thresholds({len(saved)})")
    except Exception:
        log.debug("Failed to load persisted thresholds", exc_info=True)

    # 2. Strategy enabled/disabled
    try:
        raw = get_setting(_DB_KEY_STRATEGY_ENABLED)
        if raw:
            saved = json.loads(raw)
            for k, v in saved.items():
                if k in STRATEGY_ENABLED:
                    STRATEGY_ENABLED[k] = bool(v)
            if saved:
                loaded.append(f"strategy_enabled({len(saved)})")
    except Exception:
        log.debug("Failed to load persisted strategy enabled", exc_info=True)

    # 3. Strategy budgets
    try:
        raw = get_setting(_DB_KEY_STRATEGY_BUDGETS)
        if raw:
            saved = json.loads(raw)
            from trading.risk.portfolio import STRATEGY_BUDGETS
            for k, v in saved.items():
                STRATEGY_BUDGETS[k] = float(v)
            if saved:
                loaded.append(f"budgets({len(saved)})")
    except Exception:
        log.debug("Failed to load persisted strategy budgets", exc_info=True)

    # 4. Risk parameter overrides
    try:
        raw = get_setting(_DB_KEY_RISK_OVERRIDES)
        if raw:
            saved = json.loads(raw)
            for k, v in saved.items():
                if k in RISK:
                    RISK[k] = float(v)
                    _risk_overrides[k] = float(v)
            if saved:
                loaded.append(f"risk({len(saved)})")
    except Exception:
        log.debug("Failed to load persisted risk overrides", exc_info=True)

    # One-time cleanup: reset bad outcome values (text strings like "Disabled strategy ...")
    # so _evaluate_outcomes() can properly evaluate them as positive/negative
    try:
        from trading.db.store import get_db
        with get_db() as conn:
            fixed = conn.execute(
                "UPDATE agent_recommendations SET outcome = NULL "
                "WHERE outcome IS NOT NULL AND outcome NOT IN ('positive', 'negative')"
            ).rowcount
            if fixed:
                loaded.append(f"outcome_cleanup({fixed})")
    except Exception:
        log.debug("Failed to clean up bad outcome values", exc_info=True)

    if loaded:
        log.info("AUTONOMOUS STATE RESTORED: %s", ", ".join(loaded))


# --- Fixed constants (not tunable) ---
MINIMUM_BUDGET = 0.005              # Floor: 0.5% of portfolio (prevents cascading to zero)
REALLOCATION_INTERVAL_HOURS = 24    # Rebalance allocations daily
PERFORMANCE_REVIEW_TRADES = 50      # Look at last 50 trades per strategy
BACKTEST_MIN_TRADES = 5              # Need at least 5 trades to judge
BACKTEST_LOOKBACK_DAYS = 365         # Test against past year of data
BACKTEST_COOLDOWN_DAYS = 7           # Don't re-test within 7 days
BACKTEST_MAX_PER_CYCLE = 3           # Max backtests per autonomous cycle
VERIFY_LOOKBACK_HOURS = 6            # Check actions from last N hours
MAX_FOLLOWUP_ESCALATIONS = 3         # Re-raise failed action up to N times

# Agent identifiers
PERF_AGENT = "performance_agent"
RESEARCH_AGENT = "research_agent"
RISK_AGENT = "risk_agent"
REGIME_AGENT = "regime_agent"
LEARNING_AGENT = "learning_agent"
BACKTEST_AGENT = "backtest_agent"
EXECUTOR_AGENT = "executor_agent"
POSITION_REVIEW_AGENT = "position_review_agent"
RECOVERY_AGENT = "recovery_agent"

# Position review thresholds
POSITION_REVIEW_INTERVAL_HOURS = 12   # How often we review open positions
POSITION_STALE_HOURS = 72             # Flag positions open longer than this
POSITION_LOSS_ALERT_PCT = -5.0        # Flag positions losing more than this %
POSITION_PROFIT_TARGET_PCT = 8.0      # Consider taking profits above this %
POSITION_SL_DISTANCE_WARN = 0.03      # Warn when price within 3% of stop loss


# ---------------------------------------------------------------------------
# Performance Agent — analyzes trade results, flags problems
# ---------------------------------------------------------------------------

def _performance_agent_think() -> list[dict]:
    """Performance agent analyzes all strategies and produces recommendations."""
    recommendations = []
    all_trades = get_trades(limit=1000)

    if not all_trades:
        return recommendations

    # Group by strategy
    by_strategy = {}
    for t in all_trades:
        strat = t.get("strategy", "unknown")
        # Skip profit management and stop loss pseudo-strategies
        if strat.startswith(("profit_mgmt", "stop_loss")):
            continue
        if strat not in by_strategy:
            by_strategy[strat] = []
        by_strategy[strat].append(t)

    for strat_name, trades in by_strategy.items():
        closed = [t for t in trades if t.get("pnl") is not None]
        if len(closed) < get_threshold("auto_disable_min_trades"):
            continue

        # Recent performance (last N closed trades)
        recent = closed[:PERFORMANCE_REVIEW_TRADES]
        wins = [t for t in recent if t["pnl"] > 0]
        win_rate = len(wins) / len(recent)
        total_pnl = sum(t["pnl"] for t in recent)
        avg_pnl = total_pnl / len(recent)

        # Calculate deployed capital estimate
        total_deployed = sum(t.get("total", 0) or 0 for t in recent if t.get("side") == "buy")
        loss_pct = total_pnl / total_deployed if total_deployed > 0 else 0

        # --- Auto-disable losing strategies ---
        if win_rate < get_threshold("auto_disable_win_rate") and strat_name in STRATEGY_ENABLED:
            recommendations.append({
                "from_agent": PERF_AGENT,
                "to_agent": EXECUTOR_AGENT,
                "category": "disable_strategy",
                "action": "auto_disable",
                "target": strat_name,
                "reasoning": (
                    f"Strategy '{strat_name}' has {win_rate*100:.0f}% win rate over "
                    f"{len(recent)} recent trades (threshold: {get_threshold('auto_disable_win_rate')*100:.0f}%). "
                    f"Total P&L: ${total_pnl:+.2f}. Auto-disabling to protect capital."
                ),
                "data": {
                    "win_rate": round(win_rate, 3),
                    "total_pnl": round(total_pnl, 2),
                    "avg_pnl": round(avg_pnl, 2),
                    "trade_count": len(recent),
                    "auto_approve": True,
                },
            })

        # --- Flag heavy losses ---
        elif loss_pct < get_threshold("auto_disable_max_loss_pct") and strat_name in STRATEGY_ENABLED:
            recommendations.append({
                "from_agent": PERF_AGENT,
                "to_agent": EXECUTOR_AGENT,
                "category": "disable_strategy",
                "action": "auto_disable",
                "target": strat_name,
                "reasoning": (
                    f"Strategy '{strat_name}' has lost {loss_pct*100:.1f}% of deployed capital "
                    f"(${total_pnl:+.2f} on ${total_deployed:.0f} deployed). "
                    f"Exceeds {get_threshold('auto_disable_max_loss_pct')*100:.0f}% loss threshold. Auto-disabling."
                ),
                "data": {
                    "loss_pct": round(loss_pct, 4),
                    "total_pnl": round(total_pnl, 2),
                    "total_deployed": round(total_deployed, 2),
                    "auto_approve": True,
                },
            })

    # --- Thompson Sampling budget allocation (replaces heuristic thresholds) ---
    thompson_recs = _thompson_budget_recommendations(by_strategy)
    recommendations.extend(thompson_recs)

    return recommendations


def _thompson_sample(wins: int, losses: int) -> float:
    """Sample from Beta(wins+1, losses+1) using gamma-ratio method (stdlib only)."""
    import random
    a, b = wins + 1, losses + 1
    x = random.gammavariate(a, 1)
    y = random.gammavariate(b, 1)
    return x / (x + y) if (x + y) > 0 else 0.5


def _thompson_budget_recommendations(by_strategy: dict) -> list[dict]:
    """Rank strategies via Thompson Sampling and emit budget multiplier recommendations.

    For each strategy with ≥10 closed trades: sample Beta(wins+1, losses+1).
    Rank all sampled scores. Top 25% → 1.3x, Bottom 25% → 0.7x, Middle → 1.0x.
    Scores persisted to settings["thompson_scores"] for dashboard visibility.
    """
    import json as _json
    from trading.db.store import set_setting

    scored: list[tuple[str, float, int, int, float]] = []  # (strat, score, wins, losses, win_rate)

    for strat_name, trades in by_strategy.items():
        closed = [t for t in trades if t.get("pnl") is not None]
        recent = closed[:PERFORMANCE_REVIEW_TRADES]
        if len(recent) < 10:
            continue
        wins = sum(1 for t in recent if t["pnl"] > 0)
        losses = len(recent) - wins
        win_rate = wins / len(recent)
        score = _thompson_sample(wins, losses)
        scored.append((strat_name, score, wins, losses, win_rate))

    if not scored:
        return []

    # Persist scores for dashboard
    try:
        set_setting("thompson_scores", _json.dumps(
            {s[0]: round(s[1], 4) for s in scored}
        ))
    except Exception:
        pass

    # Rank and determine tiers
    scored.sort(key=lambda x: x[1], reverse=True)
    n = len(scored)
    top_cutoff = max(1, n // 4)
    bottom_cutoff = n - max(1, n // 4)

    recs = []
    for i, (strat_name, score, wins, losses, win_rate) in enumerate(scored):
        if i < top_cutoff:
            mult = 1.3
            action = "increase_budget"
            tier = "top"
        elif i >= bottom_cutoff:
            mult = 0.7
            action = "reduce_budget"
            tier = "bottom"
        else:
            continue  # Middle tier: no change

        recs.append({
            "from_agent": PERF_AGENT,
            "to_agent": RISK_AGENT,
            "category": "shift_allocation",
            "action": action,
            "target": strat_name,
            "reasoning": (
                f"Thompson Sampling [{tier} tier, rank {i+1}/{n}]: "
                f"'{strat_name}' score={score:.3f} "
                f"(Beta({wins+1},{losses+1}), win_rate={win_rate*100:.0f}%). "
                f"{'Boosting' if mult > 1 else 'Reducing'} allocation to {mult:.1f}x."
            ),
            "data": {
                "thompson_score": round(score, 4),
                "win_rate": round(win_rate, 3),
                "wins": wins,
                "losses": losses,
                "suggested_budget_mult": mult,
                "tier": tier,
                "rank": i + 1,
                "total_ranked": n,
                "auto_approve": True,
            },
        })

    return recs


# ---------------------------------------------------------------------------
# Risk Agent — monitors portfolio-level risk, adjusts parameters
# ---------------------------------------------------------------------------

def _detect_halt_state() -> dict:
    """Detect whether the system is in a halted or tightened state."""
    total = len(STRATEGY_ENABLED)
    disabled = sum(1 for v in STRATEGY_ENABLED.values() if not v)
    return {
        "is_halted": total > 0 and disabled >= total * 0.8,  # 80%+ disabled = halted
        "is_tightened": len(_risk_overrides) > 0,
        "disabled_count": disabled,
        "total_count": total,
        "overridden_params": list(_risk_overrides.keys()),
    }


def _risk_agent_think() -> list[dict]:
    """Risk agent monitors drawdown, exposure, and recommends adjustments."""
    recommendations = []

    # Check portfolio drawdown from daily P&L
    daily_records = get_daily_pnl(limit=30)
    if len(daily_records) < 2:
        return recommendations

    current_value = daily_records[0]["portfolio_value"]
    peak_value = max(r["portfolio_value"] for r in daily_records)
    drawdown = (peak_value - current_value) / peak_value if peak_value > 0 else 0

    # --- Smart halt factors: computed once, used to classify the situation ---
    halt_enabled = get_setting("emergency_halt_enabled", "true").lower() not in ("false", "0", "off")

    # 1. Consecutive losing days (daily_records sorted DESC — newest first)
    consecutive_losses = 0
    for _r in daily_records:
        if _r.get("daily_return", 0) < 0:
            consecutive_losses += 1
        else:
            break

    # 2. Drawdown velocity: positive = worsening, negative = recovering
    dd_velocity = 0.0
    if len(daily_records) >= 4:
        _val_3d = daily_records[3]["portfolio_value"]
        _dd_3d = (peak_value - _val_3d) / peak_value if peak_value > 0 else 0
        dd_velocity = drawdown - _dd_3d

    # 3. Worst single day in last 7 — flags a market-crash event vs. gradual bleed
    worst_day = min((r.get("daily_return", 0) for r in daily_records[:7]), default=0)

    # 4. Market regime score (-1 bearish … +1 bullish) from the existing briefing system
    regime_score = 0.0
    regime_label = "unknown"
    try:
        from trading.intelligence.engine import generate_briefing
        _briefing = generate_briefing()
        regime_score = getattr(_briefing, "overall_score", 0.0)
        regime_label = getattr(_briefing, "overall_regime", "unknown")
    except Exception:
        pass

    halt_threshold = get_threshold("drawdown_halt_threshold")
    tighten_threshold = get_threshold("drawdown_tighten_threshold")

    # Early-warning: 5+ consecutive red days → effectively lower the halt trigger by 15%
    effective_halt_threshold = halt_threshold
    if consecutive_losses >= 5:
        effective_halt_threshold = max(halt_threshold * 0.85, 0.12)

    # Situation classifiers
    _market_crash = regime_score < -0.5    # broad macro selloff — not strategy failure
    _dd_recovering = dd_velocity < -0.01   # drawdown improved >1% over last 3 days
    _single_event = worst_day < -0.10      # one day lost >10% — likely external shock

    # --- Emergency halt at severe drawdown (smart) ---
    if halt_enabled and drawdown >= effective_halt_threshold:

        if _market_crash:
            # Market is crashing everywhere — halting our strategies doesn't help.
            # Tighten risk aggressively instead and let the regime resolve.
            recommendations.append({
                "from_agent": RISK_AGENT,
                "to_agent": EXECUTOR_AGENT,
                "category": "adjust_param",
                "action": "tighten_risk",
                "target": "risk_params",
                "reasoning": (
                    f"Portfolio drawdown {drawdown*100:.1f}% but market is in broad crash "
                    f"(regime: {regime_label}, score {regime_score:.2f}). "
                    f"Drawdown is macro-driven — halting strategies won't help. "
                    f"Tightening risk parameters aggressively instead."
                ),
                "data": {
                    "drawdown_pct": round(drawdown, 4),
                    "regime_score": round(regime_score, 3),
                    "adjustments": {
                        "max_position_pct": max(RISK["max_position_pct"] * 0.5, 0.03),
                        "min_cash_reserve_pct": min(RISK["min_cash_reserve_pct"] * 2.0, 0.50),
                        "stop_loss_pct": max(RISK["stop_loss_pct"] * 0.6, 0.02),
                    },
                    "auto_approve": True,
                },
            })

        elif _dd_recovering and drawdown < halt_threshold * 1.05:
            # Drawdown is already improving and we're only just over threshold — defer halt.
            recommendations.append({
                "from_agent": RISK_AGENT,
                "to_agent": EXECUTOR_AGENT,
                "category": "adjust_param",
                "action": "tighten_risk",
                "target": "risk_params",
                "reasoning": (
                    f"Drawdown {drawdown*100:.1f}% (threshold {halt_threshold*100:.0f}%) "
                    f"but recovering — velocity {dd_velocity*100:+.1f}% over 3 days. "
                    f"Deferring halt; tightening risk until drawdown confirms recovery."
                ),
                "data": {
                    "drawdown_pct": round(drawdown, 4),
                    "dd_velocity": round(dd_velocity, 4),
                    "adjustments": {
                        "max_position_pct": max(RISK["max_position_pct"] * 0.7, 0.05),
                        "min_cash_reserve_pct": min(RISK["min_cash_reserve_pct"] * 1.5, 0.40),
                        "stop_loss_pct": max(RISK["stop_loss_pct"] * 0.7, 0.03),
                    },
                    "auto_approve": True,
                },
            })

        else:
            # True halt: strategy failure, persistent drawdown, or accelerating losses.
            _halt_ctx = []
            if consecutive_losses >= 5:
                _halt_ctx.append(f"{consecutive_losses} consecutive losing days")
            if dd_velocity > 0.02:
                _halt_ctx.append(f"drawdown accelerating {dd_velocity*100:+.1f}% over 3 days")
            if _single_event:
                _halt_ctx.append(f"single-day loss of {worst_day*100:.1f}%")
            _ctx_str = "; ".join(_halt_ctx) if _halt_ctx else "persistent drawdown above threshold"

            recommendations.append({
                "from_agent": RISK_AGENT,
                "to_agent": EXECUTOR_AGENT,
                "category": "change_regime",
                "action": "emergency_halt",
                "target": "all_strategies",
                "reasoning": (
                    f"CRITICAL: Portfolio drawdown {drawdown*100:.1f}% "
                    f"(current: ${current_value:.0f}, peak: ${peak_value:.0f}). "
                    f"Context: {_ctx_str}. "
                    f"Regime: {regime_label} (score {regime_score:.2f}). "
                    f"Halting all strategies until operator review."
                ),
                "data": {
                    "drawdown_pct": round(drawdown, 4),
                    "current_value": round(current_value, 2),
                    "peak_value": round(peak_value, 2),
                    "consecutive_losses": consecutive_losses,
                    "dd_velocity": round(dd_velocity, 4),
                    "regime_score": round(regime_score, 3),
                    "auto_approve": True,
                },
            })

    # --- Tighten risk during moderate drawdown ---
    elif halt_enabled and drawdown >= tighten_threshold:
        recommendations.append({
            "from_agent": RISK_AGENT,
            "to_agent": EXECUTOR_AGENT,
            "category": "adjust_param",
            "action": "tighten_risk",
            "target": "risk_params",
            "reasoning": (
                f"Portfolio drawdown at {drawdown*100:.1f}% "
                f"(threshold: {tighten_threshold*100:.0f}%). "
                f"Tightening: reduce max_position_pct, increase cash reserve, "
                f"tighten stop losses. Will revert when drawdown recovers."
            ),
            "data": {
                "drawdown_pct": round(drawdown, 4),
                "adjustments": {
                    "max_position_pct": max(RISK["max_position_pct"] * 0.7, 0.05),
                    "min_cash_reserve_pct": min(RISK["min_cash_reserve_pct"] * 1.5, 0.40),
                    "stop_loss_pct": max(RISK["stop_loss_pct"] * 0.7, 0.03),
                },
                "auto_approve": True,
            },
        })

    else:
        # --- Recovery path: check if system is halted and drawdown has improved ---
        halt_state = _detect_halt_state()
        halt_threshold = get_threshold("drawdown_halt_threshold")
        tighten_threshold = get_threshold("drawdown_tighten_threshold")
        recovery_buffer = get_threshold("recovery_buffer_pct")

        # Phase 1: Revert tightened risk params when drawdown < tighten threshold
        if halt_state["is_tightened"] and drawdown < tighten_threshold:
            recommendations.append({
                "from_agent": RISK_AGENT,
                "to_agent": EXECUTOR_AGENT,
                "category": "adjust_param",
                "action": "revert_risk",
                "target": "risk_params",
                "reasoning": (
                    f"Drawdown recovered to {drawdown*100:.1f}% "
                    f"(below tighten threshold {tighten_threshold*100:.0f}%). "
                    f"Reverting {len(halt_state['overridden_params'])} tightened risk parameters "
                    f"to config defaults: {', '.join(halt_state['overridden_params'])}."
                ),
                "data": {
                    "drawdown_pct": round(drawdown, 4),
                    "params_to_revert": halt_state["overridden_params"],
                    "auto_approve": True,
                },
            })

        # Phase 2: Gradual strategy re-enablement when halted and drawdown
        # is meaningfully below halt threshold (with hysteresis buffer)
        recovery_threshold = halt_threshold - recovery_buffer
        if halt_state["is_halted"] and drawdown < recovery_threshold:
            max_per_cycle = int(get_threshold("recovery_max_strategies_per_cycle"))
            recommendations.append({
                "from_agent": RISK_AGENT,
                "to_agent": EXECUTOR_AGENT,
                "category": "change_regime",
                "action": "gradual_recovery",
                "target": "halted_strategies",
                "reasoning": (
                    f"RECOVERY: Drawdown at {drawdown*100:.1f}% is below recovery threshold "
                    f"{recovery_threshold*100:.1f}% (halt={halt_threshold*100:.0f}% - "
                    f"buffer={recovery_buffer*100:.0f}%). "
                    f"{halt_state['disabled_count']}/{halt_state['total_count']} strategies disabled. "
                    f"Attempting to re-enable up to {max_per_cycle} backtest-approved strategies."
                ),
                "data": {
                    "drawdown_pct": round(drawdown, 4),
                    "current_value": round(current_value, 2),
                    "peak_value": round(peak_value, 2),
                    "recovery_threshold": round(recovery_threshold, 4),
                    "max_per_cycle": max_per_cycle,
                    "auto_approve": True,
                },
            })

    # --- Check position concentration ---
    positions = get_positions()
    if positions and current_value > 0:
        for pos in positions:
            pos_value = pos["qty"] * (pos["current_price"] or pos["avg_cost"])
            concentration = pos_value / current_value
            if concentration > 0.25:
                recommendations.append({
                    "from_agent": RISK_AGENT,
                    "to_agent": EXECUTOR_AGENT,
                    "category": "performance_alert",
                    "action": "concentration_warning",
                    "target": pos["symbol"],
                    "reasoning": (
                        f"Position {pos['symbol']} represents {concentration*100:.1f}% "
                        f"of portfolio (${pos_value:.0f} / ${current_value:.0f}). "
                        f"Exceeds 25% concentration limit. Consider reducing."
                    ),
                    "data": {
                        "concentration_pct": round(concentration, 4),
                        "position_value": round(pos_value, 2),
                        "auto_approve": False,  # Requires human review
                    },
                })

    return recommendations


# ---------------------------------------------------------------------------
# Regime Agent — detects market regime changes, recommends strategy switching
# ---------------------------------------------------------------------------

def _regime_agent_think() -> list[dict]:
    """Regime agent analyzes market conditions and recommends strategy adjustments."""
    recommendations = []

    try:
        from trading.intelligence.engine import generate_briefing
        briefing = generate_briefing()
    except Exception:
        return recommendations

    overall = briefing.overall_score

    # --- Strong bearish regime: recommend defensive posture ---
    if overall < -0.4:
        # Check if we have too many long-biased strategies active
        long_biased = [
            s for s in STRATEGY_ENABLED
            if STRATEGY_ENABLED.get(s) and s in (
                "cross_asset_momentum", "meme_momentum", "factor_crypto",
                "multi_factor_rank",
            )
        ]
        if long_biased:
            recommendations.append({
                "from_agent": REGIME_AGENT,
                "to_agent": EXECUTOR_AGENT,
                "category": "change_regime",
                "action": "defensive_posture",
                "target": ",".join(long_biased),
                "reasoning": (
                    f"Market regime strongly bearish (score: {overall:+.2f}). "
                    f"Long-biased strategies ({', '.join(long_biased)}) likely to underperform. "
                    f"Recommend temporarily disabling or reducing allocation."
                ),
                "data": {
                    "regime_score": overall,
                    "regime_label": briefing.overall_regime,
                    "affected_strategies": long_biased,
                    "auto_approve": True,
                },
            })

    # --- Event risk: dampen all activity ---
    if briefing.has_event_risk():
        recommendations.append({
            "from_agent": REGIME_AGENT,
            "to_agent": RISK_AGENT,
            "category": "performance_alert",
            "action": "event_risk_warning",
            "target": "portfolio",
            "reasoning": (
                f"Event risk detected: {', '.join(briefing.event_risk)}. "
                f"Dynamic allocation will auto-dampen sizing (0.6x multiplier). "
                f"Consider reducing open positions before event."
            ),
            "data": {
                "events": briefing.event_risk,
                "auto_approve": False,
            },
        })

    # --- Strong bullish + low exposure: recommend increasing ---
    if overall > 0.4:
        positions = get_positions()
        daily = get_daily_pnl(limit=1)
        if daily:
            pv = daily[0]["portfolio_value"]
            pos_value = sum(
                p["qty"] * (p["current_price"] or p["avg_cost"]) for p in positions
            ) if positions else 0
            exposure = pos_value / pv if pv > 0 else 0

            if exposure < 0.30:
                recommendations.append({
                    "from_agent": REGIME_AGENT,
                    "to_agent": LEARNING_AGENT,
                    "category": "research_finding",
                    "action": "low_exposure_bullish",
                    "target": "portfolio",
                    "reasoning": (
                        f"Market regime strongly bullish (score: {overall:+.2f}) "
                        f"but portfolio exposure is only {exposure*100:.0f}%. "
                        f"System may be underinvested. Review if strategies are "
                        f"generating enough signals or if thresholds are too conservative."
                    ),
                    "data": {
                        "regime_score": overall,
                        "exposure_pct": round(exposure, 4),
                        "auto_approve": False,
                    },
                })

    return recommendations


# ---------------------------------------------------------------------------
# Research Agent — identifies gaps, suggests new implementations
# ---------------------------------------------------------------------------

def _research_agent_think() -> list[dict]:
    """Research agent analyzes strategy universe gaps and recommends builds."""
    recommendations = []

    try:
        from trading.intelligence.strategy_researcher import analyze_gaps
        analysis = analyze_gaps()
    except Exception:
        return recommendations

    # --- Flag zero-coverage categories ---
    for gap in analysis.category_gaps:
        recommendations.append({
            "from_agent": RESEARCH_AGENT,
            "to_agent": LEARNING_AGENT,
            "category": "add_strategy",
            "action": "coverage_gap",
            "target": gap["category"],
            "reasoning": (
                f"Category '{gap['category']}' has {gap['total_strategies']} known strategies "
                f"but ZERO implementations. This is a diversification blind spot. "
                f"Highest priority strategies from this category should be built next."
            ),
            "data": {
                "category": gap["category"],
                "total_strategies": gap["total_strategies"],
                "auto_approve": False,
            },
        })

    # --- Recommend top-priority strategies for implementation ---
    top_priorities = analysis.implementation_queue[:3]
    for strat in top_priorities:
        if strat.priority >= 8:
            recommendations.append({
                "from_agent": RESEARCH_AGENT,
                "to_agent": LEARNING_AGENT,
                "category": "add_strategy",
                "action": "implement_strategy",
                "target": strat.name,
                "reasoning": (
                    f"Strategy '{strat.name}' ({strat.category}) has priority {strat.priority}/10, "
                    f"expected Sharpe {strat.sharpe_range}, complexity: {strat.complexity}. "
                    f"Data needed: {strat.data_needed}. "
                    f"This is a high-value implementation target."
                ),
                "data": {
                    "name": strat.name,
                    "category": strat.category,
                    "priority": strat.priority,
                    "sharpe_range": strat.sharpe_range,
                    "complexity": strat.complexity,
                    "data_needed": strat.data_needed,
                    "auto_approve": False,
                },
            })

    return recommendations


# ---------------------------------------------------------------------------
# Learning Agent — synthesizes lessons, updates knowledge base
# ---------------------------------------------------------------------------

def _learning_agent_think() -> list[dict]:
    """Learning agent reviews outcomes and synthesizes lessons."""
    recommendations = []

    # Review recent recommendation outcomes to learn what works
    history = get_recommendation_history(limit=100)
    resolved = [r for r in history if r.get("status") == "resolved"]

    if len(resolved) >= 10:
        # Count outcomes
        applied = [r for r in resolved if r.get("resolution") == "applied"]
        rejected = [r for r in resolved if r.get("resolution") == "rejected"]
        successful = [r for r in applied if r.get("outcome") == "positive"]

        if applied:
            success_rate = len(successful) / len(applied) if applied else 0
            recommendations.append({
                "from_agent": LEARNING_AGENT,
                "to_agent": LEARNING_AGENT,
                "category": "research_finding",
                "action": "meta_analysis",
                "target": "recommendation_system",
                "reasoning": (
                    f"Meta-analysis of {len(resolved)} resolved recommendations: "
                    f"{len(applied)} applied ({len(successful)} successful = "
                    f"{success_rate*100:.0f}% success rate), "
                    f"{len(rejected)} rejected. "
                    f"The autonomous system's recommendations are "
                    f"{'effective' if success_rate > 0.5 else 'underperforming — review thresholds'}."
                ),
                "data": {
                    "total_resolved": len(resolved),
                    "applied": len(applied),
                    "successful": len(successful),
                    "rejected": len(rejected),
                    "success_rate": round(success_rate, 3),
                    "auto_approve": True,
                },
            })

            # --- Close the feedback loop: actually adjust thresholds ---
            if success_rate < 0.3 and len(applied) >= 5:
                # Many actions applied but few worked → system is too aggressive
                # Loosen auto-disable (lower threshold = harder to trigger disable)
                cur_wr = get_threshold("auto_disable_win_rate")
                recommendations.append({
                    "from_agent": LEARNING_AGENT,
                    "to_agent": EXECUTOR_AGENT,
                    "category": "adjust_param",
                    "action": "adjust_threshold",
                    "target": "auto_disable_win_rate",
                    "reasoning": (
                        f"Success rate is {success_rate*100:.0f}% ({len(successful)}/{len(applied)} applied). "
                        f"System is too aggressive — loosening auto-disable win rate from "
                        f"{cur_wr*100:.0f}% to reduce false disables."
                    ),
                    "data": {
                        "threshold_name": "auto_disable_win_rate",
                        "current_value": cur_wr,
                        "new_value": round(cur_wr * 0.95, 4),  # -5%
                        "direction": "decrease",
                        "auto_approve": True,
                    },
                })
                # Tighten backtest adoption (require stronger evidence)
                cur_bt = get_threshold("backtest_adopt_win_rate")
                recommendations.append({
                    "from_agent": LEARNING_AGENT,
                    "to_agent": EXECUTOR_AGENT,
                    "category": "adjust_param",
                    "action": "adjust_threshold",
                    "target": "backtest_adopt_win_rate",
                    "reasoning": (
                        f"Low success rate ({success_rate*100:.0f}%) suggests adopted strategies "
                        f"aren't performing. Tightening backtest adoption threshold from "
                        f"{cur_bt*100:.0f}% to require stronger evidence."
                    ),
                    "data": {
                        "threshold_name": "backtest_adopt_win_rate",
                        "current_value": cur_bt,
                        "new_value": round(cur_bt * 1.05, 4),  # +5%
                        "direction": "increase",
                        "auto_approve": True,
                    },
                })
            elif success_rate > 0.7 and len(applied) >= 5:
                # System is effective — can be slightly more aggressive
                cur_wr = get_threshold("auto_disable_win_rate")
                recommendations.append({
                    "from_agent": LEARNING_AGENT,
                    "to_agent": EXECUTOR_AGENT,
                    "category": "adjust_param",
                    "action": "adjust_threshold",
                    "target": "auto_disable_win_rate",
                    "reasoning": (
                        f"High success rate ({success_rate*100:.0f}%) — system is effective. "
                        f"Can be slightly more aggressive with auto-disable threshold."
                    ),
                    "data": {
                        "threshold_name": "auto_disable_win_rate",
                        "current_value": cur_wr,
                        "new_value": round(cur_wr * 0.95, 4),  # -5%
                        "direction": "decrease",
                        "auto_approve": True,
                    },
                })
                # System is effective — loosen backtest adoption to try more strategies
                cur_bt = get_threshold("backtest_adopt_win_rate")
                recommendations.append({
                    "from_agent": LEARNING_AGENT,
                    "to_agent": EXECUTOR_AGENT,
                    "category": "adjust_param",
                    "action": "adjust_threshold",
                    "target": "backtest_adopt_win_rate",
                    "reasoning": (
                        f"High success rate ({success_rate*100:.0f}%) — system is effective. "
                        f"Loosening backtest adoption threshold from {cur_bt*100:.0f}% "
                        f"to allow more strategies to be adopted."
                    ),
                    "data": {
                        "threshold_name": "backtest_adopt_win_rate",
                        "current_value": cur_bt,
                        "new_value": round(cur_bt * 0.95, 4),  # -5%
                        "direction": "decrease",
                        "auto_approve": True,
                    },
                })

    # Check for strategies that were disabled by auto-disable but might have recovered
    disabled_recs = [
        r for r in history
        if r.get("category") == "disable_strategy"
        and r.get("resolution") == "applied"
    ]
    for rec in disabled_recs[:5]:
        target = rec.get("target", "")
        # Check if strategy has been re-enabled manually
        if target in STRATEGY_ENABLED and not STRATEGY_ENABLED.get(target):
            # Could recommend re-evaluation
            recommendations.append({
                "from_agent": LEARNING_AGENT,
                "to_agent": PERF_AGENT,
                "category": "research_finding",
                "action": "re_evaluate_disabled",
                "target": target,
                "reasoning": (
                    f"Strategy '{target}' was auto-disabled. "
                    f"Market conditions may have changed. "
                    f"Recommend re-evaluation with recent data to see if it should be re-enabled."
                ),
                "data": {
                    "disabled_at": rec.get("timestamp"),
                    "original_reason": rec.get("reasoning", ""),
                    "auto_approve": False,
                },
            })

    # Analyze volume profiles to learn optimal trading times
    try:
        from trading.db.store import get_volume_profile, get_volume_profile_by_day
        from trading.config import ASTER_SYMBOLS

        for coin in ("bitcoin", "ethereum", "solana"):
            aster_sym = ASTER_SYMBOLS.get(coin)
            if not aster_sym:
                continue

            hourly = get_volume_profile(aster_sym, days=30)
            if len(hourly) < 12:
                continue  # Not enough data yet

            # Find peak and dead volume hours
            peak_hours = sorted(hourly, key=lambda h: h["avg_ratio"], reverse=True)[:3]
            dead_hours = sorted(hourly, key=lambda h: h["avg_ratio"])[:3]

            if dead_hours and dead_hours[0]["avg_ratio"] < 0.4:
                dead_range = ", ".join(f"{h['hour']:02d}:00" for h in dead_hours)
                peak_range = ", ".join(f"{h['hour']:02d}:00" for h in peak_hours)
                recommendations.append({
                    "from_agent": LEARNING_AGENT,
                    "to_agent": LEARNING_AGENT,
                    "category": "research_finding",
                    "action": "volume_pattern",
                    "target": coin,
                    "reasoning": (
                        f"Volume analysis for {coin} (30d): "
                        f"Peak hours UTC: {peak_range} "
                        f"(avg {peak_hours[0]['avg_ratio']:.0%} of baseline). "
                        f"Dead hours UTC: {dead_range} "
                        f"(avg {dead_hours[0]['avg_ratio']:.0%} of baseline). "
                        f"Avoid entries during dead hours."
                    ),
                    "data": {
                        "peak_hours": [h["hour"] for h in peak_hours],
                        "dead_hours": [h["hour"] for h in dead_hours],
                        "peak_avg_ratio": round(peak_hours[0]["avg_ratio"], 3),
                        "dead_avg_ratio": round(dead_hours[0]["avg_ratio"], 3),
                        "auto_approve": True,
                    },
                })
    except Exception:
        log.debug("Volume profile analysis failed (non-fatal)", exc_info=True)

    return recommendations


# ---------------------------------------------------------------------------
# Backtest Agent — validates strategies against historical data before adopt
# ---------------------------------------------------------------------------

def _backtest_agent_think() -> list[dict]:
    """Backtest agent runs strategies against a year of historical data.

    Scans for strategies needing validation:
    1. Disabled strategies that haven't been tested recently
    2. Newly enabled strategies with < 5 live trades
    3. All strategies past their cooldown period

    Runs up to BACKTEST_MAX_PER_CYCLE backtests per cycle.
    """
    recommendations = []

    # Get strategies due for backtesting
    candidates = get_strategies_needing_backtest(cooldown_days=BACKTEST_COOLDOWN_DAYS)
    if not candidates:
        return recommendations

    # Prioritize: disabled strategies first (re-evaluation), then enabled with few trades
    all_trades = get_trades(limit=1000)
    trades_by_strat = {}
    for t in all_trades:
        strat = t.get("strategy", "unknown")
        trades_by_strat.setdefault(strat, []).append(t)

    disabled = [s for s in candidates if not STRATEGY_ENABLED.get(s, False)]
    undertested = [
        s for s in candidates
        if STRATEGY_ENABLED.get(s, False)
        and len(trades_by_strat.get(s, [])) < 5
    ]
    rest = [s for s in candidates if s not in disabled and s not in undertested]

    # Order: disabled first, then undertested, then the rest
    ordered = disabled + undertested + rest

    # Get current portfolio value for starting capital
    daily = get_daily_pnl(limit=1)
    starting_capital = daily[0]["portfolio_value"] if daily else 1000.0

    tested = 0
    for strategy_name in ordered:
        if tested >= BACKTEST_MAX_PER_CYCLE:
            break

        try:
            from trading.backtest.engine import Backtester, _fetch_historical_data
            from trading.strategy.registry import get_strategy

            # Verify strategy exists
            if get_strategy(strategy_name) is None:
                continue

            log.info("BACKTEST: Running %d-day backtest for '%s'", BACKTEST_LOOKBACK_DAYS, strategy_name)

            historical_data = _fetch_historical_data(strategy_name, BACKTEST_LOOKBACK_DAYS)

            end_date = datetime.now(timezone.utc).date() - timedelta(days=1)
            start_date = end_date - timedelta(days=BACKTEST_LOOKBACK_DAYS)

            backtester = Backtester(
                starting_capital=starting_capital,
                commission_pct=0.001,
            )
            result = backtester.run(
                strategy_name=strategy_name,
                historical_data=historical_data,
                start_date=str(start_date),
                end_date=str(end_date),
            )

            m = result.metrics
            win_rate = m.get("win_rate", 0)
            sharpe = m.get("sharpe_ratio", 0)
            max_dd = m.get("max_drawdown", 0)
            total_trades = m.get("total_trades", 0)

            # Evaluate against thresholds
            if total_trades < BACKTEST_MIN_TRADES:
                verdict = "inconclusive"
                reasoning = (
                    f"Backtest of '{strategy_name}' over {BACKTEST_LOOKBACK_DAYS} days "
                    f"produced only {total_trades} trades (min {BACKTEST_MIN_TRADES}). "
                    f"Insufficient data to judge — deferring."
                )
            elif (win_rate >= get_threshold("backtest_adopt_win_rate")
                  and sharpe >= get_threshold("backtest_adopt_sharpe")
                  and max_dd <= get_threshold("backtest_adopt_max_dd")):
                verdict = "adopt"
                reasoning = (
                    f"Backtest PASSED for '{strategy_name}': "
                    f"win_rate={win_rate*100:.0f}% (≥{get_threshold('backtest_adopt_win_rate')*100:.0f}%), "
                    f"Sharpe={sharpe:.2f} (≥{get_threshold('backtest_adopt_sharpe')}), "
                    f"max_dd={max_dd*100:.1f}% (≤{get_threshold('backtest_adopt_max_dd')*100:.0f}%), "
                    f"{total_trades} trades over {BACKTEST_LOOKBACK_DAYS} days. "
                    f"Recommending auto-enable."
                )
            elif (win_rate < get_threshold("backtest_discard_win_rate")
                  or max_dd > get_threshold("backtest_discard_max_dd")):
                verdict = "discard"
                reasoning = (
                    f"Backtest FAILED for '{strategy_name}': "
                    f"win_rate={win_rate*100:.0f}%, Sharpe={sharpe:.2f}, "
                    f"max_dd={max_dd*100:.1f}%, {total_trades} trades. "
                    f"Below adoption thresholds — keeping disabled."
                )
            else:
                verdict = "inconclusive"
                reasoning = (
                    f"Backtest MIXED for '{strategy_name}': "
                    f"win_rate={win_rate*100:.0f}%, Sharpe={sharpe:.2f}, "
                    f"max_dd={max_dd*100:.1f}%, {total_trades} trades. "
                    f"Does not clearly pass or fail — deferring for review."
                )

            # Record result in DB
            insert_backtest_result(strategy_name, BACKTEST_LOOKBACK_DAYS, m, verdict)

            # Emit recommendation based on verdict
            if verdict == "adopt":
                recommendations.append({
                    "from_agent": BACKTEST_AGENT,
                    "to_agent": EXECUTOR_AGENT,
                    "category": "enable_strategy",
                    "action": "backtest_adopt",
                    "target": strategy_name,
                    "reasoning": reasoning,
                    "data": {
                        "win_rate": round(win_rate, 3),
                        "sharpe": round(sharpe, 4),
                        "max_drawdown": round(max_dd, 4),
                        "total_trades": total_trades,
                        "total_pnl": round(m.get("total_pnl", 0), 2),
                        "lookback_days": BACKTEST_LOOKBACK_DAYS,
                        "auto_approve": True,
                    },
                })
            elif verdict == "discard":
                recommendations.append({
                    "from_agent": BACKTEST_AGENT,
                    "to_agent": EXECUTOR_AGENT,
                    "category": "disable_strategy",
                    "action": "backtest_discard",
                    "target": strategy_name,
                    "reasoning": reasoning,
                    "data": {
                        "win_rate": round(win_rate, 3),
                        "sharpe": round(sharpe, 4),
                        "max_drawdown": round(max_dd, 4),
                        "total_trades": total_trades,
                        "lookback_days": BACKTEST_LOOKBACK_DAYS,
                        "auto_approve": True,
                    },
                })
            else:
                recommendations.append({
                    "from_agent": BACKTEST_AGENT,
                    "to_agent": LEARNING_AGENT,
                    "category": "research_finding",
                    "action": "backtest_inconclusive",
                    "target": strategy_name,
                    "reasoning": reasoning,
                    "data": {
                        "win_rate": round(win_rate, 3),
                        "sharpe": round(sharpe, 4),
                        "max_drawdown": round(max_dd, 4),
                        "total_trades": total_trades,
                        "lookback_days": BACKTEST_LOOKBACK_DAYS,
                        "auto_approve": True,
                    },
                })

            tested += 1
            log.info("BACKTEST: '%s' verdict=%s (win=%.0f%%, sharpe=%.2f, dd=%.1f%%)",
                     strategy_name, verdict, win_rate * 100, sharpe, max_dd * 100)

        except Exception as e:
            log.warning("BACKTEST: Failed for '%s': %s", strategy_name, e)
            continue

    return recommendations


# ---------------------------------------------------------------------------
# Recovery & Self-Healing Agent (Wave 7)
# ---------------------------------------------------------------------------

def _recovery_agent_think() -> list[dict]:
    """Analyzes the circuit breaker state and recommends self-healing actions.

    If the fund is in 'Conservative Mode' but market conditions are excellent,
    it recommends 'Force Graduation' or 'Relax Confluence'.
    """
    from trading.strategy.circuit_breaker import get_recovery_mode
    from trading.db.store import get_intelligence_briefing

    recommendations = []
    mode = get_recovery_mode()
    if not mode.get("active"):
        return []

    # Get intelligence briefing for regime context
    briefing = get_intelligence_briefing()
    regime_score = briefing.get("regime_score", 0.0)
    is_bullish = regime_score > 0.4
    is_extreme_bullish = regime_score > 0.7

    activated_at = mode.get("activated_at", "")
    age_days = 0
    if activated_at:
        try:
            dt = datetime.fromisoformat(activated_at)
            age_days = (datetime.now(timezone.utc) - dt).days
        except Exception:
            pass

    # Recommendation 1: Force Graduation (Extreme High Conviction)
    if is_extreme_bullish and age_days >= 7:
        recommendations.append({
            "from_agent": RECOVERY_AGENT,
            "to_agent": EXECUTOR_AGENT,
            "category": "system_config",
            "action": "force_graduate",
            "target": "circuit_breaker",
            "reasoning": (
                f"Fund has been in conservative mode for {age_days} days. "
                f"Market regime is extremely bullish (score {regime_score:.2f}). "
                f"Wave 5 guardrails are active. Recommending immediate return to normal operations."
            ),
            "data": {"auto_approve": True},
        })

    # Recommendation 2: Relax Confluence (Moderate High Conviction)
    elif is_bullish and mode.get("min_strategies", 2) > 1:
        recommendations.append({
            "from_agent": RECOVERY_AGENT,
            "to_agent": EXECUTOR_AGENT,
            "category": "system_config",
            "action": "relax_confluence",
            "target": "circuit_breaker",
            "reasoning": (
                f"Market regime is bullish (score {regime_score:.2f}). "
                "Current 2-strategy confluence requirement is creating a mathematical deadlock. "
                "Recommending relaxation to 1-strategy confluence under Wave 5 Signal Guardrails."
            ),
            "data": {"auto_approve": True},
        })

    # Recommendation 3: Rehabilitate Strategies
    try:
        from trading.db.store import get_setting
        from trading.config import STRATEGY_ENABLED
        for name in STRATEGY_ENABLED:
            cb_raw = get_setting(f"cb_{name}")
            if cb_raw:
                cb_state = json.loads(cb_raw)
                if cb_state.get("consecutive_losses", 0) >= 5:
                    recommendations.append({
                        "from_agent": RECOVERY_AGENT,
                        "to_agent": EXECUTOR_AGENT,
                        "category": "strategy_config",
                        "action": "rehabilitate_strategy",
                        "target": name,
                        "reasoning": (
                            f"Strategy '{name}' was circuit-broken due to consecutive losses. "
                            "With Wave 5 Signal Filter now active, this strategy can be safely re-enabled "
                            "under the new guardrail protection."
                        ),
                        "data": {"auto_approve": True},
                    })
    except Exception:
        pass

    return recommendations


# ---------------------------------------------------------------------------
# Verification — ensure previous cycle's actions actually took effect
# ---------------------------------------------------------------------------

def _evaluate_outcomes():
    """Evaluate outcomes of recently applied recommendations.

    Checks if applied actions had the desired effect and updates outcome
    to 'positive' or 'negative'. This closes the feedback loop so the
    Learning Agent has real success/failure data.
    """
    try:
        from trading.db.store import (
            get_recently_applied_recommendations,
            update_recommendation_outcome,
        )
    except ImportError:
        return

    recent = get_recently_applied_recommendations(hours=48)
    if not recent:
        return

    trades = get_trades(limit=500)
    pnl_data = get_daily_pnl(limit=7)
    current_drawdown = 0.0
    if pnl_data and len(pnl_data) >= 2:
        peak = max(d.get("portfolio_value", 0) for d in pnl_data)
        current_val = pnl_data[0].get("portfolio_value", 0) if pnl_data else 0
        current_drawdown = (peak - current_val) / peak if peak > 0 else 0

    for rec in recent:
        action = rec.get("action", "")
        target = rec.get("target", "")
        data = json.loads(rec["data"]) if isinstance(rec.get("data"), str) else (rec.get("data") or {})
        outcome = None

        try:
            if action == "auto_disable":
                # Positive if strategy is still disabled (action stuck)
                # Check recent market — would strategy have lost more?
                strat_trades = [t for t in trades if t.get("strategy") == target
                                and t.get("timestamp", "") > rec.get("resolved_at", "")]
                if not strat_trades:
                    # No trades since disable = action had effect, mark positive
                    outcome = "positive"
                else:
                    strat_pnl = sum(t.get("pnl", 0) or 0 for t in strat_trades)
                    outcome = "positive" if strat_pnl <= 0 else "negative"

            elif action == "tighten_risk":
                # Positive if drawdown was contained (didn't hit halt threshold)
                outcome = "positive" if current_drawdown < get_threshold("drawdown_halt_threshold") else "negative"

            elif action == "emergency_halt":
                # Positive if drawdown decreased after halt
                outcome = "positive" if current_drawdown < get_threshold("drawdown_halt_threshold") else "negative"

            elif action == "backtest_adopt":
                # Positive if re-enabled strategy has positive recent P&L
                strat_trades = [t for t in trades if t.get("strategy") == target
                                and t.get("timestamp", "") > rec.get("resolved_at", "")]
                if strat_trades:
                    strat_pnl = sum(t.get("pnl", 0) or 0 for t in strat_trades)
                    outcome = "positive" if strat_pnl > 0 else "negative"

            elif action == "adjust_threshold":
                # Check if the threshold is still set to the expected value
                param = data.get("threshold_name", "") or data.get("param", "")
                expected = data.get("new_value")
                if param and expected is not None:
                    actual = get_threshold(param) if param in _DEFAULT_THRESHOLDS else None
                    if actual is not None and abs(actual - expected) < 0.001:
                        outcome = "positive"
                    else:
                        outcome = "negative"
                else:
                    outcome = "positive"  # No specific param to verify

            elif action in ("implement_strategy", "coverage_gap"):
                # Verify the strategy file actually exists
                from pathlib import Path
                strat_dir = Path(__file__).resolve().parent.parent / "strategy"
                snake = target.lower().replace(" ", "_").replace("-", "_")
                if (strat_dir / f"{snake}.py").exists():
                    outcome = "positive"
                else:
                    outcome = "negative"

            elif action in ("reduce_budget", "increase_budget"):
                # Verify budget was actually changed
                try:
                    from trading.risk.portfolio import STRATEGY_BUDGETS
                    expected_budget = data.get("new_budget")
                    actual_budget = STRATEGY_BUDGETS.get(target)
                    if expected_budget and actual_budget and abs(actual_budget - expected_budget) < 0.001:
                        outcome = "positive"
                    elif not expected_budget:
                        outcome = "positive"  # No specific value to verify
                    else:
                        outcome = "negative"
                except ImportError:
                    outcome = "positive"

            elif action in ("event_risk", "concentration_warning", "defensive_posture"):
                # Risk management — positive if drawdown hasn't worsened
                outcome = "positive" if current_drawdown < get_threshold("drawdown_halt_threshold") else "negative"

            elif action == "meta_analysis":
                # Informational — always positive if it was recorded
                outcome = "positive"

            if outcome:
                update_recommendation_outcome(rec["id"], outcome)
                log.debug("OUTCOME: rec #%d (%s on %s) → %s", rec["id"], action, target, outcome)
        except Exception as e:
            log.debug("Outcome evaluation failed for rec #%d: %s", rec.get("id", 0), e)


def _verify_previous_actions() -> list[dict]:
    """Check that actions from the last cycle actually took effect.

    Returns new recommendations for any actions that failed verification.
    """
    followups = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=VERIFY_LOOKBACK_HOURS)).isoformat()

    history = get_recommendation_history(limit=50)
    recent_applied = [
        r for r in history
        if r.get("resolution") == "applied"
        and r.get("timestamp", "") >= cutoff
    ]

    for rec in recent_applied:
        action = rec.get("action", "")
        target = rec.get("target", "")
        data = json.loads(rec["data"]) if isinstance(rec.get("data"), str) else (rec.get("data") or {})
        escalation = data.get("_escalation_count", 0)

        if escalation >= MAX_FOLLOWUP_ESCALATIONS:
            continue  # Already escalated enough

        verified = True
        failure_reason = ""

        try:
            if action == "auto_disable" and target in STRATEGY_ENABLED:
                if STRATEGY_ENABLED.get(target, False):
                    verified = False
                    failure_reason = f"Strategy '{target}' was disabled but is now enabled again"

            elif action == "backtest_discard" and target in STRATEGY_ENABLED:
                if STRATEGY_ENABLED.get(target, False):
                    verified = False
                    failure_reason = f"Strategy '{target}' was discarded but is still enabled"

            elif action == "emergency_halt":
                still_enabled = [s for s, v in STRATEGY_ENABLED.items() if v]
                if still_enabled:
                    verified = False
                    failure_reason = f"Emergency halt issued but {len(still_enabled)} strategies still enabled: {still_enabled[:3]}"

            elif action == "tighten_risk":
                adjustments = data.get("adjustments", {})
                for param, expected in adjustments.items():
                    actual = RISK.get(param)
                    if actual is not None and actual != expected:
                        verified = False
                        failure_reason = f"Risk param '{param}' expected {expected} but is {actual}"
                        break

            elif action == "reduce_budget":
                try:
                    from trading.risk.portfolio import STRATEGY_BUDGETS
                    expected = data.get("new_budget")
                    actual = STRATEGY_BUDGETS.get(target)
                    if expected and actual and abs(actual - expected) > 0.001:
                        verified = False
                        failure_reason = f"Budget for '{target}' expected {expected} but is {actual}"
                except ImportError:
                    pass

            elif action == "implement_strategy":
                # Check if the strategy file was actually generated
                # Use _snake_case to match how the builder names files
                from trading.intelligence.strategy_builder import _snake_case, STRATEGY_DIR
                snake = _snake_case(target)
                expected_file = STRATEGY_DIR / f"{snake}.py"
                if not expected_file.exists():
                    verified = False
                    failure_reason = f"Strategy '{target}' was requested but file {expected_file} not found"

        except Exception as e:
            log.debug("Verification check failed for %s: %s", action, e)
            continue  # Don't escalate on verification errors

        if not verified:
            log.warning("VERIFY FAILED: %s on '%s' — %s", action, target, failure_reason)
            # Update the original recommendation's outcome to reflect verification failure
            try:
                from trading.db.store import update_recommendation_outcome
                update_recommendation_outcome(rec["id"], "negative")
            except Exception:
                pass
            followups.append({
                "from_agent": EXECUTOR_AGENT,
                "to_agent": EXECUTOR_AGENT,
                "category": rec.get("category", "followup"),
                "action": action,  # Re-issue the same action
                "target": target,
                "reasoning": (
                    f"[FOLLOW-UP #{escalation + 1}] Previous action did not take effect: "
                    f"{failure_reason}. Re-applying."
                ),
                "data": {
                    **data,
                    "_escalation_count": escalation + 1,
                    "_original_timestamp": rec.get("timestamp"),
                    "auto_approve": True,
                },
            })

            log_action(
                "autonomous", "verification_failed",
                details=f"{action} on '{target}': {failure_reason}",
                data={"escalation": escalation + 1, "original_rec_id": rec.get("id")},
            )

    if followups:
        log.info("VERIFY: %d actions need re-application", len(followups))

    return followups


# ---------------------------------------------------------------------------
# Executor — applies safe recommendations automatically
# ---------------------------------------------------------------------------

def _execute_safe_recommendations(recommendations: list[dict]) -> list[dict]:
    """Apply ALL recommendations with full autonomy.

    All recommendations are auto-approved and executed immediately.
    The system operates with full agent autonomy — no human review gate.
    Returns list of applied actions.
    """
    applied = []

    for rec in recommendations:
        # --- GUARDRAIL CHECK ---
        # If the pattern is historically failing, veto auto-execution
        if not is_recommendation_safe(rec["from_agent"], rec.get("category", ""), rec["action"]):
            log.warning(
                "VETO: Guardrail blocked auto-execution of %s recommendation (%s)",
                rec["from_agent"], rec["action"]
            )
            # Downgrade to 'resolved' with 'vetoed' outcome
            resolve_recommendation(
                rec.get("id", 0), 
                resolution="vetoed", 
                outcome="neutral"
            )
            continue

        data = rec.get("data", {})
        # Full autonomy: mark all recommendations as auto_approve
        data["auto_approve"] = True

        # --- Auto-apply all actions ---
        action = rec["action"]
        target = rec.get("target", "")
        result = None

        try:
            # --- Wave 7: Recovery & Self-Healing Actions ---
            if action == "force_graduate":
                from trading.strategy.circuit_breaker import force_graduate as _fg
                _fg(reason=rec["reasoning"])
                result = "Forced graduation complete — conservative mode disabled"
                log.warning("RECOVERY: %s", result)

            elif action == "relax_confluence":
                from trading.db.store import get_setting, set_setting
                raw = get_setting("recovery_mode")
                if raw:
                    state = json.loads(raw)
                    state["min_strategies"] = 1
                    set_setting("recovery_mode", json.dumps(state))
                    result = "Confluence relaxed to 1-strategy requirement"
                else:
                    result = "Confluence relaxation failed: recovery_mode not active"
                log.warning("RECOVERY: %s", result)

            elif action == "rehabilitate_strategy":
                from trading.strategy.circuit_breaker import rehabilitate_strategy as _rs
                _rs(target, reason=rec["reasoning"])
                result = f"Rehabilitated strategy '{target}'"
                log.warning("RECOVERY: %s", result)

            elif action == "auto_disable" and target in STRATEGY_ENABLED:
                # Disable the strategy in runtime config
                STRATEGY_ENABLED[target] = False
                result = f"Disabled strategy '{target}'"
                log.warning("AUTO-DISABLE: %s — %s", target, rec["reasoning"][:100])
                # Also write strategy_override entry so operator_hooks respects disable
                try:
                    set_setting(f"strategy_override_{target}", "disabled")
                except Exception:
                    pass

            elif action == "emergency_halt":
                # Disable all strategies
                for strat in list(STRATEGY_ENABLED.keys()):
                    STRATEGY_ENABLED[strat] = False
                result = "Emergency halt — all strategies disabled"
                log.critical("EMERGENCY HALT: %s", rec["reasoning"][:100])
                # Also write strategy_override entries so operator_hooks respects halt
                try:
                    for strat in STRATEGY_ENABLED:
                        set_setting(f"strategy_override_{strat}", "disabled")
                except Exception:
                    pass
                insert_knowledge(
                    title=f"Emergency Halt — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
                    source=RISK_AGENT,
                    category="recovery",
                    content=rec["reasoning"],
                    key_rules=json.dumps(data),
                )

            elif action == "gradual_recovery":
                # Re-enable strategies gradually, prioritizing those with passing backtests
                max_per_cycle = data.get("max_per_cycle", 2)
                enabled_count = 0
                recovery_details = []

                for strat, is_enabled in sorted(STRATEGY_ENABLED.items()):
                    if is_enabled:
                        continue
                    if enabled_count >= max_per_cycle:
                        break
                    # Only re-enable if strategy has a passing backtest
                    last_bt = get_last_backtest(strat)
                    if last_bt and last_bt.get("verdict") == "adopt":
                        STRATEGY_ENABLED[strat] = True
                        # Clear the strategy_override so operator_hooks doesn't re-disable
                        try:
                            set_setting(f"strategy_override_{strat}", "enabled")
                        except Exception:
                            pass
                        enabled_count += 1
                        recovery_details.append(strat)
                        bt_metrics = last_bt.get("metrics") or {}
                        if isinstance(bt_metrics, str):
                            bt_metrics = json.loads(bt_metrics) if bt_metrics else {}
                        log.warning(
                            "RECOVERY: Re-enabled '%s' (backtest verdict: adopt, "
                            "win_rate=%.0f%%, sharpe=%.2f)",
                            strat,
                            bt_metrics.get("win_rate", 0) * 100,
                            bt_metrics.get("sharpe_ratio", 0),
                        )

                if enabled_count:
                    result = f"Recovery: re-enabled {enabled_count} strategies: {', '.join(recovery_details)}"
                else:
                    # No passing backtests — queue all disabled strategies for re-evaluation
                    # so the backtest agent can clear them for re-enablement next cycle
                    disabled_strats = [s for s, v in STRATEGY_ENABLED.items() if not v]
                    queued = 0
                    for strat in disabled_strats:
                        try:
                            insert_backtest_result(
                                strategy=strat, days=0,
                                metrics={"triggered_by": "halt_recovery", "stale": True},
                                verdict="needs_retest",
                            )
                            queued += 1
                        except Exception:
                            pass
                    result = (
                        f"Recovery stalled: no backtest-approved strategies available. "
                        f"Queued {queued} strategies for re-evaluation."
                    )
                log.warning("GRADUAL RECOVERY: %s", result)
                insert_knowledge(
                    title=f"Drawdown Recovery — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
                    source=RISK_AGENT,
                    category="recovery",
                    content=f"{rec['reasoning']}\n\nResult: {result}",
                    key_rules=json.dumps(data),
                )

            elif action == "tighten_risk":
                adjustments = data.get("adjustments", {})
                for param, value in adjustments.items():
                    if param in RISK:
                        old = RISK[param]
                        RISK[param] = value
                        _risk_overrides[param] = value
                        log.warning("RISK TIGHTENED: %s = %s → %s", param, old, value)
                result = f"Tightened {len(adjustments)} risk parameters"

            elif action == "revert_risk":
                # Revert tightened risk parameters to config defaults
                params_reverted = []
                for param in list(_risk_overrides.keys()):
                    if param in _RISK_DEFAULTS:
                        old_val = RISK[param]
                        RISK[param] = _RISK_DEFAULTS[param]
                        params_reverted.append(f"{param}: {old_val} -> {_RISK_DEFAULTS[param]}")
                        log.warning("RISK REVERTED: %s = %s → %s (config default)", param, old_val, _RISK_DEFAULTS[param])
                _risk_overrides.clear()
                result = f"Reverted {len(params_reverted)} risk parameters: {'; '.join(params_reverted)}"
                insert_knowledge(
                    title=f"Risk Parameters Reverted — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
                    source=RISK_AGENT,
                    category="recovery",
                    content=f"{rec['reasoning']}\n\nResult: {result}",
                    key_rules=json.dumps(data),
                )

            elif action == "defensive_posture":
                # Temporarily disable long-biased strategies
                affected = data.get("affected_strategies", [])
                for strat in affected:
                    if strat in STRATEGY_ENABLED:
                        STRATEGY_ENABLED[strat] = False
                result = f"Defensive posture — disabled {len(affected)} long-biased strategies"
                log.warning("DEFENSIVE POSTURE: disabled %s", affected)

            elif action == "reduce_budget":
                # Reduce the strategy's budget in portfolio allocator
                try:
                    from trading.risk.portfolio import STRATEGY_BUDGETS, DEFAULT_BUDGET
                    mult = data.get("suggested_budget_mult", 0.5)
                    current = STRATEGY_BUDGETS.get(target, DEFAULT_BUDGET)
                    new_budget = round(max(current * mult, MINIMUM_BUDGET), 4)
                    STRATEGY_BUDGETS[target] = new_budget
                    data["new_budget"] = new_budget  # Store for outcome evaluation
                    floor_note = " (hit minimum floor)" if new_budget <= MINIMUM_BUDGET else ""
                    result = f"Reduced {target} budget: {current:.4f} → {new_budget:.4f}{floor_note}"
                    log.info("BUDGET REDUCED: %s", result)
                except Exception as e:
                    result = f"Budget reduction failed: {e}"

            elif action == "increase_budget":
                try:
                    from trading.risk.portfolio import STRATEGY_BUDGETS, DEFAULT_BUDGET
                    mult = data.get("suggested_budget_mult", 1.5)
                    current = STRATEGY_BUDGETS.get(target, DEFAULT_BUDGET)
                    new_budget = round(min(current * mult, 0.12), 4)  # Cap at 12%
                    STRATEGY_BUDGETS[target] = new_budget
                    data["new_budget"] = new_budget  # Store for outcome evaluation
                    result = f"Increased {target} budget: {current:.4f} → {new_budget:.4f}"
                    log.info("BUDGET INCREASED: %s", result)
                except Exception as e:
                    result = f"Budget increase failed: {e}"

            elif action == "adjust_threshold":
                # Learning Agent feedback loop — actually tune thresholds
                t_name = data.get("threshold_name", "")
                t_new = data.get("new_value")
                if t_name and t_new is not None:
                    old_val = get_threshold(t_name)
                    actual = set_threshold(t_name, float(t_new))
                    result = f"Threshold '{t_name}' adjusted: {old_val} → {actual}"
                    log.info("THRESHOLD ADJUSTED: %s — %s", result, rec["reasoning"][:100])
                else:
                    result = f"Threshold adjustment skipped: missing name or value"

            elif action == "meta_analysis":
                # Save meta-analysis to knowledge base
                insert_knowledge(
                    title=f"Autonomous System Meta-Analysis — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source="learning_agent",
                    category="system_learning",
                    content=rec["reasoning"],
                    key_rules=json.dumps(data),
                )
                result = "Meta-analysis saved to knowledge base"

            elif action == "backtest_adopt" and target in STRATEGY_ENABLED:
                STRATEGY_ENABLED[target] = True
                result = (
                    f"Backtest-adopted strategy '{target}' — "
                    f"win_rate={data.get('win_rate', 0)*100:.0f}%, "
                    f"sharpe={data.get('sharpe', 0):.2f}"
                )
                log.info("BACKTEST-ADOPT: %s — %s", target, rec["reasoning"][:100])

            elif action == "backtest_discard":
                if target in STRATEGY_ENABLED:
                    STRATEGY_ENABLED[target] = False
                insert_knowledge(
                    title=f"Backtest Discard: {target} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source="backtest_agent",
                    category="backtest_discard",
                    content=rec["reasoning"],
                    key_rules=json.dumps(data),
                )
                result = (
                    f"Backtest-discarded strategy '{target}' — "
                    f"win_rate={data.get('win_rate', 0)*100:.0f}%, "
                    f"sharpe={data.get('sharpe', 0):.2f}"
                )
                log.info("BACKTEST-DISCARD: %s — %s", target, rec["reasoning"][:100])

            elif action == "enable_strategy" and target in STRATEGY_ENABLED:
                STRATEGY_ENABLED[target] = True
                result = f"Enabled strategy '{target}'"
                log.info("AUTO-ENABLE: %s — %s", target, rec["reasoning"][:100])

            elif action == "event_risk":
                # Actually reduce sizing during event risk — don't just log it
                sizing_mult = data.get("suggested_sizing_mult", get_threshold("event_risk_sizing_mult"))
                try:
                    from trading.risk.portfolio import STRATEGY_BUDGETS, DEFAULT_BUDGET
                    adjusted = 0
                    for strat in list(STRATEGY_BUDGETS.keys()):
                        if STRATEGY_ENABLED.get(strat, False):
                            old = STRATEGY_BUDGETS[strat]
                            STRATEGY_BUDGETS[strat] = round(old * sizing_mult, 4)
                            adjusted += 1
                    # Also tighten stop losses during events
                    if "stop_loss_pct" in RISK:
                        old_sl = RISK["stop_loss_pct"]
                        RISK["stop_loss_pct"] = round(old_sl * 0.8, 4)  # 20% tighter
                        _risk_overrides["stop_loss_pct"] = RISK["stop_loss_pct"]
                        log.warning("EVENT RISK: Tightened stop_loss_pct %s → %s", old_sl, RISK["stop_loss_pct"])
                    result = f"Event risk: reduced sizing by {sizing_mult:.0%} on {adjusted} strategies, tightened stops"
                    log.warning("EVENT RISK APPLIED: %s — %s", result, rec["reasoning"][:100])
                except Exception as e:
                    result = f"Event risk sizing reduction failed: {e}"
                insert_knowledge(
                    title=f"Event Risk Applied — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source=rec["from_agent"], category="agent_intelligence",
                    content=rec["reasoning"], key_rules=json.dumps(data),
                )

            elif action == "concentration_warning":
                # Actually reduce budget for the concentrated position
                try:
                    from trading.risk.portfolio import STRATEGY_BUDGETS, DEFAULT_BUDGET
                    concentration = data.get("concentration_pct", 0)
                    if concentration > get_threshold("concentration_max_pct") and target:
                        # Find strategies trading this symbol and reduce their budgets
                        positions = get_positions()
                        concentrated_strats = set()
                        for p in positions:
                            if p.get("symbol", "").replace("/", "") == target.replace("/", ""):
                                strat = p.get("strategy", "")
                                if strat:
                                    concentrated_strats.add(strat)
                        for strat in concentrated_strats:
                            old = STRATEGY_BUDGETS.get(strat, DEFAULT_BUDGET)
                            new_budget = round(old * 0.5, 4)  # Halve the budget
                            STRATEGY_BUDGETS[strat] = new_budget
                            log.warning("CONCENTRATION: Reduced %s budget %s → %s", strat, old, new_budget)
                        result = f"Concentration warning: reduced budgets for {len(concentrated_strats)} strategies on {target}"
                    else:
                        result = f"Concentration warning noted for {target} ({concentration:.0%})"
                except Exception as e:
                    result = f"Concentration budget reduction failed: {e}"
                insert_knowledge(
                    title=f"Concentration Action: {target} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source=rec["from_agent"], category="agent_intelligence",
                    content=rec["reasoning"], key_rules=json.dumps(data),
                )

            elif action in ("re_evaluate", "re_evaluate_disabled"):
                # Actually queue the strategy for backtest instead of just logging
                if target and target in STRATEGY_ENABLED:
                    try:
                        from trading.db.store import insert_backtest_result
                        # Clear cooldown by marking last backtest as stale
                        # The backtest agent will pick it up next cycle
                        log.info("RE-EVALUATE: Clearing backtest cooldown for '%s'", target)
                        # Force it into the backtest queue by inserting a stale placeholder
                        insert_backtest_result(
                            strategy=target,
                            days=0,
                            metrics={"triggered_by": "re_evaluate", "stale": True},
                            verdict="needs_retest",
                        )
                        result = f"Re-evaluation queued: '{target}' added to backtest queue"
                    except Exception as e:
                        result = f"Re-evaluation queue failed: {e}"
                else:
                    result = f"Re-evaluation: target '{target}' not found in strategy registry"
                insert_knowledge(
                    title=f"Re-evaluation Queued: {target} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source=rec["from_agent"], category="agent_intelligence",
                    content=rec["reasoning"], key_rules=json.dumps(data),
                )

            elif action == "underinvestment_alert":
                # Legacy: manual underinvestment re-enablement.
                # Automated recovery is handled by gradual_recovery (generated by Risk Agent).
                # This handler is retained for operator-triggered or future agent use.
                try:
                    enabled_count = 0
                    for strat, is_enabled in STRATEGY_ENABLED.items():
                        if is_enabled:
                            continue
                        # Check if it has a passing backtest
                        last_bt = get_last_backtest(strat)
                        if last_bt and last_bt.get("verdict") == "adopt":
                            STRATEGY_ENABLED[strat] = True
                            enabled_count += 1
                            log.info("UNDERINVESTMENT: Re-enabled '%s' (passed backtest)", strat)
                            if enabled_count >= 2:
                                break  # Don't re-enable too many at once
                    if enabled_count:
                        result = f"Underinvestment: re-enabled {enabled_count} backtest-approved strategies"
                    else:
                        result = "Underinvestment: no backtest-approved disabled strategies to re-enable"
                except Exception as e:
                    result = f"Underinvestment re-enable failed: {e}"
                insert_knowledge(
                    title=f"Underinvestment Action — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source=rec["from_agent"], category="agent_intelligence",
                    content=rec["reasoning"], key_rules=json.dumps(data),
                )

            elif action in ("research_finding", "performance_alert"):
                # Genuinely informational — log to knowledge base
                insert_knowledge(
                    title=f"Agent {rec['from_agent']}: {action} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source=rec["from_agent"], category="agent_intelligence",
                    content=rec["reasoning"], key_rules=json.dumps(data),
                )
                result = f"Agent intelligence logged: {action}"
                log.info("AGENT INTELLIGENCE: [%s] %s — %s", rec["from_agent"], action, rec["reasoning"][:100])

            elif action == "implement_strategy":
                # Build the strategy immediately instead of deferring
                built = False
                try:
                    from trading.intelligence.strategy_builder import generate_strategy_code, STRATEGY_DIR, _snake_case
                    code = generate_strategy_code(
                        strategy_name=target,
                        description=rec["reasoning"],
                        category=data.get("category", "trend_following"),
                        priority=data.get("priority", 5),
                        data_needed=", ".join(data.get("data_needed", [])) if isinstance(data.get("data_needed"), list) else data.get("data_needed", "OHLCV"),
                    )
                    if code:
                        # Use the same _snake_case as generate_strategy_code so
                        # the filename matches the class `name` attribute inside.
                        snake = _snake_case(target)
                        out_path = STRATEGY_DIR / f"{snake}.py"
                        out_path.write_text(code)
                        # Register in STRATEGY_ENABLED (disabled, awaiting backtest)
                        STRATEGY_ENABLED[snake] = False
                        _persist_strategy_enabled()
                        built = True
                        result = f"Strategy '{target}' built → {out_path} (disabled, awaiting backtest)"
                        log.info("STRATEGY BUILT: %s → %s", target, out_path)
                except Exception as e:
                    log.warning("Strategy build failed for '%s': %s", target, e)

                if not built:
                    # Fall back to deferred if build fails
                    insert_knowledge(
                        title=f"Strategy Implementation Request: {target} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                        source=f"agent_{rec.get('from_agent', 'unknown')}",
                        category="deferred_implementation",
                        content=rec["reasoning"], key_rules=json.dumps(data),
                    )
                    result = f"Strategy build failed for '{target}', deferred to knowledge base"

            elif action == "coverage_gap":
                # Log the gap AND trigger the research agent to find implementations
                insert_knowledge(
                    title=f"Coverage Gap: {target} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source=f"agent_{rec.get('from_agent', 'unknown')}",
                    category="coverage_gap",
                    content=rec["reasoning"], key_rules=json.dumps(data),
                )
                # Try to find and build a strategy for this gap category
                try:
                    from trading.intelligence.strategy_researcher import analyze_gaps
                    from trading.intelligence.strategy_builder import generate_strategy_code
                    analysis = analyze_gaps()
                    # Find top candidate matching this gap category
                    candidates = [
                        s for s in analysis.implementation_queue
                        if s.category == target
                    ]
                    if candidates:
                        top = candidates[0]
                        from trading.intelligence.strategy_builder import _snake_case, STRATEGY_DIR as _STRAT_DIR
                        code = generate_strategy_code(
                            strategy_name=top.name,
                            description=getattr(top, "description", rec["reasoning"]),
                            category=top.category,
                            priority=top.priority,
                            data_needed=top.data_needed,
                        )
                        if code:
                            snake = _snake_case(top.name)
                            out_path = _STRAT_DIR / f"{snake}.py"
                            out_path.write_text(code)
                            STRATEGY_ENABLED[snake] = False
                            _persist_strategy_enabled()
                            result = f"Coverage gap '{target}': built '{top.name}' → {out_path}"
                            log.info("COVERAGE GAP FILLED: %s → %s", target, out_path)
                        else:
                            result = f"Coverage gap '{target}': found candidate '{top.name}' but build failed"
                    else:
                        result = f"Coverage gap '{target}' logged — no implementation candidates found"
                except Exception as e:
                    result = f"Coverage gap '{target}' logged — auto-fill failed: {e}"
                log.info("COVERAGE GAP: %s — %s", target, rec["reasoning"][:100])

            else:
                # Unknown action — log to knowledge base for transparency
                insert_knowledge(
                    title=f"Agent Action: {action} on {target} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source=f"agent_{rec.get('from_agent', 'unknown')}",
                    category="agent_action",
                    content=rec["reasoning"],
                    key_rules=json.dumps(data),
                )
                result = f"Action '{action}' acknowledged and logged to knowledge base"
                log.info("AGENT ACTION LOGGED: %s — %s", action, rec["reasoning"][:100])

        except Exception as e:
            result = f"Execution failed: {e}"
            log.error("Auto-execution failed for %s: %s", action, e)

        # Record the recommendation and its outcome
        rec_id = insert_recommendation(
            from_agent=rec["from_agent"],
            to_agent=rec["to_agent"],
            category=rec["category"],
            action=rec["action"],
            target=target,
            reasoning=rec["reasoning"],
            data=data,
        )
        resolve_recommendation(rec_id, "applied")  # outcome stays NULL for _evaluate_outcomes()

        applied.append({
            "action": action,
            "target": target,
            "result": result,
            "reasoning": rec["reasoning"][:200],
        })

        log_action(
            "autonomous", action,
            symbol=target if len(target) < 20 else None,
            details=result,
            data={
                "from_agent": rec["from_agent"],
                "reasoning": rec["reasoning"][:500],
                **data,
            },
        )

    # Persist all state changes to DB so they survive restarts
    if applied:
        _persist_strategy_enabled()
        _persist_strategy_budgets()
        _persist_risk_overrides()
        # Thresholds are persisted inline in set_threshold()

    return applied


# ---------------------------------------------------------------------------
# Challenger agent — peer review of high-stakes recommendations (Phase 1.5)
# ---------------------------------------------------------------------------

_CHALLENGER_TARGETS = {"emergency_halt", "defensive_posture", "emergency_disable", "tighten_risk"}


def _challenger_agent_think(all_recommendations: list[dict]) -> list[dict]:
    """Review high-stakes recommendations and produce dissent or confirm responses.

    Runs after all 10 primary agents but before execution. Targets only
    auto-approve recommendations that could halt or severely restrict trading.
    A dissent strips auto_approve so the journal agent must manually approve it
    on the next cycle; a confirm adds supporting evidence.
    """
    from trading.db.store import get_daily_pnl

    high_stakes = [
        r for r in all_recommendations
        if r.get("action") in _CHALLENGER_TARGETS
        and r.get("data", {}).get("auto_approve")
    ]
    if not high_stakes:
        return []

    # Gather context once for all challenges
    regime_score = 0.0
    regime_label = "unknown"
    try:
        from trading.intelligence.engine import generate_briefing
        _b = generate_briefing()
        regime_score = getattr(_b, "overall_score", 0.0)
        regime_label = getattr(_b, "overall_regime", "unknown")
    except Exception:
        pass

    dd_velocity = 0.0
    try:
        daily = get_daily_pnl(days=5)
        if len(daily) >= 4:
            peak = max((r.get("portfolio_value", 0) for r in daily), default=0)
            if peak > 0:
                current = daily[0].get("portfolio_value", peak)
                val_3d = daily[3].get("portfolio_value", peak)
                dd_now = (peak - current) / peak
                dd_3d = (peak - val_3d) / peak
                dd_velocity = dd_now - dd_3d  # positive = worsening
    except Exception:
        pass

    dissents: list[dict] = []

    for rec in high_stakes:
        action = rec["action"]
        from_agent = rec["from_agent"]

        # Is the market regime contradicting this recommendation?
        regime_contradicts = False
        if action in ("emergency_halt", "defensive_posture", "emergency_disable"):
            # Halting during a crash makes sense; halting in a bull market is suspicious
            regime_contradicts = regime_score > 0.3  # Regime is bullish, halt seems wrong
        elif action == "tighten_risk":
            regime_contradicts = regime_score > 0.5 and dd_velocity < -0.01  # Bull + recovering

        # Is drawdown actually recovering?
        dd_recovering = dd_velocity < -0.01

        if regime_contradicts or dd_recovering:
            context_parts = []
            if regime_contradicts:
                context_parts.append(
                    f"regime is {regime_label} (score {regime_score:+.2f}) — market conditions "
                    f"don't support halting"
                )
            if dd_recovering:
                context_parts.append(
                    f"drawdown is recovering (velocity {dd_velocity*100:+.2f}% over 3 days)"
                )
            context = "; ".join(context_parts)

            dissents.append({
                "from_agent": "challenger_agent",
                "to_agent": from_agent,
                "category": "challenge",
                "action": "dissent",
                "target": action,
                "reasoning": (
                    f"CHALLENGER DISSENT on '{action}' from {from_agent}: {context}. "
                    f"Removing auto_approve — defer to journal agent for manual review."
                ),
                "data": {
                    "original_action": action,
                    "regime_score": round(regime_score, 3),
                    "regime_label": regime_label,
                    "dd_velocity": round(dd_velocity, 4),
                    "auto_approve": False,
                },
            })
            # Strip auto_approve from the original recommendation in-place
            rec["data"]["auto_approve"] = False
            rec["reasoning"] = (
                rec["reasoning"]
                + f" [CHALLENGER DISSENT: {context} — manual review required]"
            )
            log.info(
                "Challenger dissented on '%s' from %s: %s",
                action, from_agent, context,
            )
        else:
            # Confirm the recommendation with supporting regime context
            dissents.append({
                "from_agent": "challenger_agent",
                "to_agent": from_agent,
                "category": "challenge",
                "action": "confirm",
                "target": action,
                "reasoning": (
                    f"CHALLENGER CONFIRMS '{action}' from {from_agent}: "
                    f"regime={regime_label} ({regime_score:+.2f}), "
                    f"dd_velocity={dd_velocity*100:+.2f}% — conditions support this action."
                ),
                "data": {
                    "original_action": action,
                    "regime_score": round(regime_score, 3),
                    "dd_velocity": round(dd_velocity, 4),
                    "auto_approve": False,  # Challenger's own rec doesn't auto-execute
                },
            })

    return dissents


# ---------------------------------------------------------------------------
# Conversation log — agents talking to each other
# ---------------------------------------------------------------------------

def _log_agent_conversation(all_recommendations: list[dict]):
    """Log the full agent conversation for transparency and learning."""
    if not all_recommendations:
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Agent Conversation — {timestamp}",
        "",
    ]

    for rec in all_recommendations:
        auto = "AUTO" if rec.get("data", {}).get("auto_approve") else "REVIEW"
        lines.append(
            f"**[{rec['from_agent']}→{rec['to_agent']}]** "
            f"[{auto}] {rec['action']} `{rec.get('target', '')}`"
        )
        lines.append(f"> {rec['reasoning']}")
        lines.append("")

    content = "\n".join(lines)

    # Append to conversation log
    log_path = KNOWLEDGE_DIR / "agent_conversations.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Keep only last 50 conversations (prevent unbounded growth)
    existing = ""
    if log_path.exists():
        existing = log_path.read_text()
        # Count conversation blocks
        blocks = existing.split("# Agent Conversation —")
        if len(blocks) > 50:
            # Keep last 50
            existing = "# Agent Conversation —".join(blocks[-50:])

    log_path.write_text(content + "\n---\n\n" + existing)


# ---------------------------------------------------------------------------
# Position Review Agent — deep analysis of open positions using system context
# ---------------------------------------------------------------------------

def _position_review_agent_think() -> list[dict]:
    """Position Review Agent: analyzes every open position with full system context.

    Unlike a raw LLM call, this agent:
    1. Gathers strategy performance history for the strategy that opened the trade
    2. Checks volume conditions (is liquidity drying up?)
    3. Evaluates proximity to stop loss / take profit
    4. Considers portfolio-level risk (drawdown, concentration)
    5. Reviews recent lessons from other agents
    6. Produces structured recommendations (hold / tighten_stop / take_profit / close)
    7. Writes a rich analysis to trade_analyses table for the dashboard

    Runs on the autonomous cycle (every cycle) but only writes analysis every 12h.
    """
    recommendations = []

    positions = get_positions()
    if not positions:
        return recommendations

    all_trades = get_trades(limit=200)
    open_trades = [t for t in all_trades if not t.get("closed_at")]
    if not open_trades:
        return recommendations

    # Gather portfolio context once
    daily_records = get_daily_pnl(limit=30)
    portfolio_value = daily_records[0]["portfolio_value"] if daily_records else 0
    peak_value = max(r["portfolio_value"] for r in daily_records) if daily_records else 0
    portfolio_drawdown = (peak_value - portfolio_value) / peak_value if peak_value > 0 else 0

    # Strategy performance lookup
    trades_by_strategy = {}
    for t in all_trades:
        strat = t.get("strategy", "unknown")
        trades_by_strategy.setdefault(strat, []).append(t)

    # Recent lessons from other agents
    recent_lessons = []
    try:
        from trading.intelligence.action_narrator import get_recent_lessons
        recent_lessons = get_recent_lessons(limit=10)
    except Exception:
        pass

    # Volume data
    vol_data = {}
    try:
        from trading.execution.router import get_crypto_quote
    except ImportError:
        get_crypto_quote = None

    # Check last analysis time per trade to avoid spamming
    from trading.db.store import get_trade_analyses, insert_trade_analysis

    for trade in open_trades:
        symbol = trade.get("symbol", "")
        trade_id = trade.get("id", 0)
        strategy = trade.get("strategy", "unknown")

        # Find matching position
        pos = next((p for p in positions if p.get("symbol") == symbol or
                     p.get("symbol", "").replace("/", "") == symbol.replace("/", "")), None)
        if not pos:
            continue

        # --- Skip if analyzed within last 12 hours ---
        recent_analyses = get_trade_analyses(trade_id, limit=1)
        if recent_analyses:
            try:
                last_analysis_time = datetime.fromisoformat(
                    recent_analyses[0]["timestamp"].replace("Z", "+00:00")
                )
                hours_since = (datetime.now(timezone.utc) - last_analysis_time).total_seconds() / 3600
                if hours_since < POSITION_REVIEW_INTERVAL_HOURS:
                    continue  # Already reviewed recently
            except Exception:
                pass

        # --- Gather deep context for this position ---
        entry_price = trade.get("price", 0)
        current_price = pos.get("current_price", 0) or 0
        unrealized_pnl = pos.get("unrealized_pnl", 0) or 0
        unrealized_pct = pos.get("unrealized_pnl_pct", 0) or 0
        stop_loss = trade.get("stop_loss_price")
        take_profit = trade.get("take_profit_price")
        leverage = trade.get("leverage", 1) or 1

        # Time open
        hours_open = 0
        try:
            entry_time = datetime.fromisoformat(trade["timestamp"].replace("Z", "+00:00"))
            hours_open = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
        except Exception:
            pass

        # Strategy track record
        strat_trades = trades_by_strategy.get(strategy, [])
        strat_closed = [t for t in strat_trades if t.get("pnl") is not None]
        strat_win_rate = None
        strat_avg_pnl = None
        if strat_closed:
            wins = len([t for t in strat_closed if t["pnl"] > 0])
            strat_win_rate = wins / len(strat_closed)
            strat_avg_pnl = sum(t["pnl"] for t in strat_closed) / len(strat_closed)

        # Position concentration
        pos_value = pos.get("market_value", 0) or (pos.get("qty", 0) * current_price)
        concentration = pos_value / portfolio_value if portfolio_value > 0 else 0

        # Stop loss proximity
        sl_distance = None
        sl_at_risk = False
        if stop_loss and current_price > 0:
            sl_distance = abs(current_price - stop_loss) / current_price
            sl_at_risk = sl_distance < POSITION_SL_DISTANCE_WARN

        # Take profit proximity
        tp_distance = None
        tp_near = False
        if take_profit and current_price > 0:
            tp_distance = abs(take_profit - current_price) / current_price
            tp_near = tp_distance < 0.02  # Within 2% of target

        # --- Determine action based on structured rules ---
        action = "hold"
        urgency = "normal"
        reasons = []

        # Rule 1: Deep loss — recommend closing
        if unrealized_pct <= POSITION_LOSS_ALERT_PCT:
            action = "close"
            urgency = "high"
            reasons.append(
                f"Position is down {unrealized_pct:.1f}% (${unrealized_pnl:+.2f}), "
                f"exceeding {POSITION_LOSS_ALERT_PCT}% loss threshold"
            )

        # Rule 2: Near stop loss — recommend tightening or preparing exit
        if sl_at_risk and action != "close":
            action = "tighten_stop"
            urgency = "high"
            reasons.append(
                f"Price is within {sl_distance*100:.1f}% of stop loss "
                f"(${stop_loss:.2f}). Close to triggering"
            )

        # Rule 3: Take profit target nearly reached
        if tp_near:
            action = "take_profit"
            urgency = "medium"
            reasons.append(
                f"Price within {tp_distance*100:.1f}% of take profit target "
                f"(${take_profit:.2f}). Consider taking profits"
            )

        # Rule 4: Strong profit — consider trailing stop
        if unrealized_pct >= POSITION_PROFIT_TARGET_PCT and action == "hold":
            action = "take_profit"
            urgency = "medium"
            reasons.append(
                f"Position is up {unrealized_pct:+.1f}% (${unrealized_pnl:+.2f}). "
                f"Consider trailing stop or partial close"
            )

        # Rule 5: Stale position
        if hours_open >= POSITION_STALE_HOURS and action == "hold":
            urgency = "low"
            reasons.append(
                f"Position has been open for {hours_open:.0f}h ({hours_open/24:.1f} days). "
                f"Review if thesis still holds"
            )

        # Rule 6: Strategy underperforming
        if strat_win_rate is not None and strat_win_rate < 0.35 and len(strat_closed) >= 10:
            reasons.append(
                f"Strategy '{strategy}' has {strat_win_rate*100:.0f}% win rate "
                f"over {len(strat_closed)} trades — underperforming"
            )
            if action == "hold":
                action = "tighten_stop"
                urgency = "medium"

        # Rule 7: High concentration risk
        if concentration > get_threshold("concentration_max_pct"):
            reasons.append(
                f"Position represents {concentration*100:.1f}% of portfolio — "
                f"exceeds {get_threshold('concentration_max_pct')*100:.0f}% limit"
            )
            if action == "hold":
                action = "tighten_stop"

        # Rule 8: Portfolio in drawdown — protect capital
        if portfolio_drawdown >= get_threshold("drawdown_tighten_threshold"):
            reasons.append(
                f"Portfolio in {portfolio_drawdown*100:.1f}% drawdown — "
                f"capital preservation mode"
            )
            if action == "hold" and unrealized_pnl > 0:
                action = "take_profit"
                urgency = "medium"

        # Rule 9: Leveraged positions get stricter monitoring
        if leverage > 1:
            reasons.append(f"Leveraged position ({leverage}x) — tighter monitoring required")
            if unrealized_pct < -3.0 and action == "hold":
                action = "tighten_stop"
                urgency = "high"

        # Default hold reason
        if not reasons:
            reasons.append(
                f"Position performing within normal range "
                f"({unrealized_pct:+.1f}%, {hours_open:.0f}h open)"
            )

        # --- Build rich analysis narrative from system knowledge ---
        analysis_parts = []

        # Position status
        status_emoji = "📈" if unrealized_pnl >= 0 else "📉"
        analysis_parts.append(
            f"{status_emoji} {symbol} {trade.get('side', '').upper()} @ ${entry_price:.2f} → "
            f"${current_price:.2f} ({unrealized_pct:+.1f}%, ${unrealized_pnl:+.2f})"
        )

        # Strategy context
        if strat_win_rate is not None:
            analysis_parts.append(
                f"Strategy '{strategy}': {strat_win_rate*100:.0f}% win rate, "
                f"avg P&L ${strat_avg_pnl:+.2f} over {len(strat_closed)} closed trades"
            )

        # Risk levels
        risk_items = []
        if stop_loss:
            risk_items.append(f"SL ${stop_loss:.2f} ({sl_distance*100:.1f}% away)" if sl_distance else f"SL ${stop_loss:.2f}")
        if take_profit:
            risk_items.append(f"TP ${take_profit:.2f} ({tp_distance*100:.1f}% away)" if tp_distance else f"TP ${take_profit:.2f}")
        if leverage > 1:
            risk_items.append(f"{leverage}x leverage")
        if risk_items:
            analysis_parts.append(f"Risk levels: {', '.join(risk_items)}")

        # Portfolio context
        analysis_parts.append(
            f"Portfolio: ${portfolio_value:.0f} (drawdown: {portfolio_drawdown*100:.1f}%, "
            f"position weight: {concentration*100:.1f}%)"
        )

        # Agent assessment
        assessment = action.upper().replace("_", " ")
        analysis_parts.append(f"Assessment: {assessment} — {'; '.join(reasons)}")

        # Include relevant lessons
        symbol_lessons = [l for l in recent_lessons if symbol.lower() in l.lower() or strategy.lower() in l.lower()]
        if symbol_lessons:
            analysis_parts.append(f"Relevant lessons: {symbol_lessons[0][:200]}")

        analysis_text = "\n".join(analysis_parts)

        # --- Enhance with LLM synthesis (best-effort) ---
        try:
            from trading.llm.engine import synthesize_position_review
            structured = {
                "symbol": symbol, "side": trade.get("side"), "action": action,
                "urgency": urgency, "entry_price": entry_price,
                "current_price": current_price, "unrealized_pnl": unrealized_pnl,
                "unrealized_pct": unrealized_pct, "hours_open": round(hours_open, 1),
                "leverage": leverage, "strategy": strategy,
                "strategy_win_rate": round(strat_win_rate, 3) if strat_win_rate is not None else None,
                "concentration": round(concentration, 4),
                "portfolio_drawdown": round(portfolio_drawdown, 4),
                "reasons": reasons,
            }
            llm_synthesis = synthesize_position_review(structured, analysis_parts)
            if llm_synthesis and "LLM unavailable" not in llm_synthesis:
                analysis_text = llm_synthesis
        except Exception as e:
            log.debug("LLM position synthesis failed (using rules): %s", e)

        # --- Write analysis to trade_analyses table ---
        market_snapshot = {
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pct": unrealized_pct,
            "hours_open": round(hours_open, 1),
            "portfolio_drawdown": round(portfolio_drawdown, 4),
            "concentration": round(concentration, 4),
            "strategy_win_rate": round(strat_win_rate, 3) if strat_win_rate else None,
            "sl_distance": round(sl_distance, 4) if sl_distance else None,
            "tp_distance": round(tp_distance, 4) if tp_distance else None,
            "action": action,
            "urgency": urgency,
        }
        try:
            insert_trade_analysis(trade_id, analysis_text, market_snapshot, source="position_review_agent")
            log.info("Position review: trade #%d (%s) → %s [%s]", trade_id, symbol, action, urgency)
        except Exception as e:
            log.warning("Failed to insert position review for trade #%d: %s", trade_id, e)

        # --- Produce recommendations for actionable items ---
        if action in ("close", "take_profit", "tighten_stop") and urgency in ("high", "medium"):
            recommendations.append({
                "from_agent": POSITION_REVIEW_AGENT,
                "to_agent": EXECUTOR_AGENT if action == "close" else RISK_AGENT,
                "category": "position_review",
                "action": action,
                "target": symbol,
                "reasoning": f"{assessment}: {'; '.join(reasons)}",
                "data": {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "side": trade.get("side"),
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "unrealized_pct": round(unrealized_pct, 2),
                    "hours_open": round(hours_open, 1),
                    "leverage": leverage,
                    "strategy": strategy,
                    "strategy_win_rate": round(strat_win_rate, 3) if strat_win_rate is not None else None,
                    "concentration_pct": round(concentration, 4),
                    "urgency": urgency,
                    "auto_approve": action == "tighten_stop",  # Only auto-approve stop tightening
                },
            })

    return recommendations

# ---------------------------------------------------------------------------
# News interpretation agent
# ---------------------------------------------------------------------------

NEWS_AGENT = "news_interpretation_agent"


def _news_agent_think() -> list[dict]:
    """News Interpretation Agent — analyzes market news impact on portfolio.

    Fetches headlines from all sources, uses LLM to interpret,
    and generates recommendations if high-impact news is detected.
    """
    recommendations = []

    try:
        from trading.data.news import fetch_all_headlines
        from trading.llm.engine import analyze_news_impact
        from trading.execution.router import get_positions_from_aster
        from trading.config import CRYPTO_SYMBOLS, ASTER_SYMBOLS
        from trading.db.store import log_action, get_action_log
        import json

        # Don't run if we analyzed news recently (within 2 hours)
        recent = get_action_log(limit=5, category="intelligence")
        for r in recent:
            if r.get("action") == "news_analysis":
                from datetime import datetime, timezone, timedelta
                ts = r.get("timestamp", "")
                try:
                    last_run = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) - last_run < timedelta(hours=2):
                        return recommendations
                except Exception:
                    pass
                break

        # Fetch headlines
        headlines = fetch_all_headlines(max_per_source=6)
        if len(headlines) < 5:
            return recommendations

        # Get positions and regime
        positions = get_positions_from_aster()[:15]
        regime = "unknown"
        for b in get_action_log(limit=3, category="intelligence"):
            data = b.get("data", {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            if isinstance(data, dict) and data.get("overall_regime"):
                regime = data["overall_regime"]
                break

        traded_assets = list(set(list(CRYPTO_SYMBOLS.keys()) + list(ASTER_SYMBOLS.keys())))
        analysis = analyze_news_impact(headlines, positions, regime, traded_assets[:30])

        if not analysis or "LLM unavailable" in analysis:
            return recommendations

        # Log the analysis
        log_action(
            "intelligence", "news_analysis",
            details=analysis[:300],
            data={
                "full_analysis": analysis,
                "headline_count": len(headlines),
                "source_count": len(set(h.get("source", "") for h in headlines)),
                "regime": regime,
            },
        )

        # Check for risk alerts in the analysis
        analysis_lower = analysis.lower()
        if any(word in analysis_lower for word in ["risk alert", "reduce exposure", "emergency", "crash", "liquidat"]):
            recommendations.append({
                "from_agent": NEWS_AGENT,
                "to_agent": "risk",
                "category": "risk_alert",
                "action": "review_exposure",
                "target": "portfolio",
                "reasoning": f"News analysis flagged risk: {analysis[:200]}",
                "data": {"auto_approve": False, "source": "news_analysis"},
            })

        log.info("News agent: analyzed %d headlines, regime=%s", len(headlines), regime)

    except Exception as e:
        log.debug("News agent failed (non-fatal): %s", e)

    return recommendations


# ---------------------------------------------------------------------------
# Main autonomous cycle
# ---------------------------------------------------------------------------

def run_autonomous_cycle() -> dict:
    """Run a full autonomous improvement cycle.

    Each agent thinks independently, produces recommendations,
    then the executor applies safe ones and stores others for review.

    Returns summary of actions taken.
    """
    log.info("Autonomous improvement cycle started")

    # Restore persisted state (strategy enables, thresholds, budgets, risk)
    # so that previous cycle's changes survive process restarts
    load_persisted_state()

    # Write the emergency halt default setting on first run (persists across restarts)
    if get_setting("emergency_halt_enabled") is None:
        set_setting("emergency_halt_enabled", "false")

    # If emergency halt is disabled, clear conservative mode and re-enable any
    # strategies stuck as "disabled" by a previous halt.
    if get_setting("emergency_halt_enabled", "true").lower() in ("false", "0", "off"):
        try:
            from trading.strategy.circuit_breaker import force_graduate, get_recovery_mode
            if get_recovery_mode().get("active"):
                force_graduate("Emergency halt disabled by operator")
        except Exception:
            pass
        for strat in list(STRATEGY_ENABLED.keys()):
            if get_setting(f"strategy_override_{strat}", "") == "disabled":
                set_setting(f"strategy_override_{strat}", "enabled")
                STRATEGY_ENABLED[strat] = True

    # Ensure the recommendation guardrail has the latest failure patterns
    refresh_guardrail()

    all_recommendations = []
    agent_results = {}

    # --- Phase 0a: Evaluate outcomes of previously applied recommendations ---
    try:
        _evaluate_outcomes()
    except Exception as e:
        log.debug("Outcome evaluation failed: %s", e)

    # --- Phase 0b: Verify previous cycle's actions took effect ---
    try:
        followups = _verify_previous_actions()
        if followups:
            all_recommendations.extend(followups)
            agent_results["verification"] = f"{len(followups)} follow-ups"
            log.warning("VERIFY: %d actions from last cycle need re-application", len(followups))
        else:
            agent_results["verification"] = "all clear"
    except Exception as e:
        log.error("Verification phase failed: %s", e)
        agent_results["verification"] = f"error: {e}"

    # --- Gather recent lessons from action narratives for context ---
    recent_lessons = []
    try:
        from trading.intelligence.action_narrator import get_recent_lessons
        recent_lessons = get_recent_lessons(limit=15)
        if recent_lessons:
            log.info("Injecting %d recent lessons into agent context", len(recent_lessons))
    except Exception:
        pass

    # --- Each agent thinks ---
    agents = [
        ("performance", _performance_agent_think),
        ("risk", _risk_agent_think),
        ("regime", _regime_agent_think),
        ("research", _research_agent_think),
        ("learning", _learning_agent_think),
        ("backtest", _backtest_agent_think),
        ("position_review", _position_review_agent_think),
        ("news_interpretation", _news_agent_think),
        ("signal_filter", _signal_filter_agent_think),
        ("recovery", _recovery_agent_think),
    ]

    for agent_name, think_fn in agents:
        try:
            recs = think_fn()
            all_recommendations.extend(recs)
            agent_results[agent_name] = len(recs)
            if recs:
                log.info("%s agent produced %d recommendations", agent_name, len(recs))
        except Exception as e:
            log.error("%s agent failed: %s", agent_name, e)
            agent_results[agent_name] = f"error: {e}"

    # --- Phase 1.5: Challenger agent — peer review high-stakes recommendations ---
    try:
        challenger_recs = _challenger_agent_think(all_recommendations)
        all_recommendations.extend(challenger_recs)
        agent_results["challenger"] = len(challenger_recs)
        if challenger_recs:
            dissents = sum(1 for r in challenger_recs if r.get("action") == "dissent")
            confirms = len(challenger_recs) - dissents
            log.info(
                "Challenger agent: %d dissents, %d confirms", dissents, confirms
            )
    except Exception as e:
        log.error("Challenger agent failed: %s", e)
        agent_results["challenger"] = f"error: {e}"

    # --- Log the conversation ---
    _log_agent_conversation(all_recommendations)

    # --- Execute safe recommendations ---
    applied = _execute_safe_recommendations(all_recommendations)

    # --- Strategy Builder: generate code for deferred implementations ---
    try:
        from trading.intelligence.strategy_builder import build_pending_strategies
        built = build_pending_strategies()
        if built:
            agent_results["builder"] = len(built)
            for b in built:
                log.info("BUILDER: Generated strategy %s → %s", b["name"], b["file"])
    except Exception as e:
        log.error("Strategy builder failed: %s", e)
        agent_results["builder"] = f"error: {e}"

    # --- Summary ---
    auto_count = sum(1 for r in all_recommendations if r.get("data", {}).get("auto_approve"))
    review_count = len(all_recommendations) - auto_count

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_recommendations": len(all_recommendations),
        "auto_applied": len(applied),
        "needs_review": review_count,
        "agent_results": agent_results,
        "applied_actions": applied,
    }

    log_action(
        "autonomous", "cycle_complete",
        details=(
            f"Agents produced {len(all_recommendations)} recommendations: "
            f"{len(applied)} auto-applied, {review_count} need review"
        ),
        data=summary,
    )

    log.info(
        "Autonomous cycle complete: %d recommendations, %d applied, %d need review",
        len(all_recommendations), len(applied), review_count,
    )

    return summary


# ---------------------------------------------------------------------------
# Signal Filter Agent — reports on the guardrail's active veteos
# ---------------------------------------------------------------------------

def _signal_filter_agent_think() -> list[dict]:
    """Signal Filter agent reports on current blacklist and veto statistics."""
    from trading.intelligence.filter import guardrail
    recommendations = []
    
    try:
        blacklist = guardrail.get_blacklist()
        if blacklist:
            recommendations.append({
                "from_agent": "signal_filter_agent",
                "to_agent": "learning_agent",
                "category": "performance_alert",
                "action": "active_blacklist",
                "target": "recommendation_system",
                "reasoning": (
                    f"Activated {len(blacklist)} recommendation veto patterns due to low success rates (<30%). "
                    f"Most recent failures being actively blocked: "
                    f"{', '.join([str(b['pattern']) for b in blacklist[:3]])}."
                ),
                "data": {
                    "blacklist_count": len(blacklist),
                    "blacklist_details": blacklist,
                    "auto_approve": False,
                },
            })
    except Exception as e:
        log.debug("Signal filter agent failed to get blacklist: %s", e)
        
    return recommendations


def get_autonomous_status() -> dict:
    """Get current status of the autonomous system for dashboard display."""
    pending = get_pending_recommendations()
    history = get_recommendation_history(limit=20)

    recent_applied = [
        r for r in history
        if r.get("resolution") == "applied"
    ]

    return {
        "pending_recommendations": len(pending),
        "recent_applied": len(recent_applied),
        "pending_details": [
            {
                "from": r["from_agent"],
                "action": r["action"],
                "target": r.get("target", ""),
                "reasoning": r["reasoning"][:150],
            }
            for r in pending[:10]
        ],
        "recent_actions": [
            {
                "timestamp": r["timestamp"],
                "action": r["action"],
                "target": r.get("target", ""),
                "outcome": r.get("outcome", ""),
            }
            for r in recent_applied[:10]
        ],
    }
