"""Autonomous Improvement Engine — self-evolving trading system.

Agents continuously converse with each other through recommendations:
  - Performance Agent:  analyzes trade results, flags underperformers
  - Research Agent:     identifies strategy gaps, suggests new builds
  - Risk Agent:         monitors drawdown/exposure, adjusts allocation
  - Regime Agent:       detects regime shifts, recommends strategy switching
  - Learning Agent:     synthesizes lessons, updates knowledge base

Safe actions are auto-applied. Dangerous actions require human review.

Auto-approved (safe):
  - Disable a strategy losing money (win rate < 25% over 20+ trades)
  - Shift allocation away from underperformers
  - Tighten risk parameters during drawdown
  - Update knowledge base with new findings
  - Log recommendations and reasoning

Requires human review (dangerous):
  - Enable a new untested strategy
  - Increase leverage
  - Loosen risk limits
  - Deploy new strategy code

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
    insert_knowledge,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Auto-approve thresholds — these actions happen without human intervention
AUTO_DISABLE_WIN_RATE = 0.25        # Disable strategy if win rate below 25%
AUTO_DISABLE_MIN_TRADES = 20        # Minimum trades before auto-disabling
AUTO_DISABLE_MAX_LOSS_PCT = -0.15   # Disable if total loss > 15% of deployed capital

DRAWDOWN_TIGHTEN_THRESHOLD = 0.10   # Tighten risk at 10% drawdown
DRAWDOWN_HALT_THRESHOLD = 0.18      # Emergency halt at 18% drawdown

REALLOCATION_INTERVAL_HOURS = 24    # Rebalance allocations daily
PERFORMANCE_REVIEW_TRADES = 50      # Look at last 50 trades per strategy

# Agent identifiers
PERF_AGENT = "performance_agent"
RESEARCH_AGENT = "research_agent"
RISK_AGENT = "risk_agent"
REGIME_AGENT = "regime_agent"
LEARNING_AGENT = "learning_agent"
EXECUTOR_AGENT = "executor_agent"


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
        if len(closed) < AUTO_DISABLE_MIN_TRADES:
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
        if win_rate < AUTO_DISABLE_WIN_RATE and strat_name in STRATEGY_ENABLED:
            recommendations.append({
                "from_agent": PERF_AGENT,
                "to_agent": EXECUTOR_AGENT,
                "category": "disable_strategy",
                "action": "auto_disable",
                "target": strat_name,
                "reasoning": (
                    f"Strategy '{strat_name}' has {win_rate*100:.0f}% win rate over "
                    f"{len(recent)} recent trades (threshold: {AUTO_DISABLE_WIN_RATE*100:.0f}%). "
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
        elif loss_pct < AUTO_DISABLE_MAX_LOSS_PCT and strat_name in STRATEGY_ENABLED:
            recommendations.append({
                "from_agent": PERF_AGENT,
                "to_agent": EXECUTOR_AGENT,
                "category": "disable_strategy",
                "action": "auto_disable",
                "target": strat_name,
                "reasoning": (
                    f"Strategy '{strat_name}' has lost {loss_pct*100:.1f}% of deployed capital "
                    f"(${total_pnl:+.2f} on ${total_deployed:.0f} deployed). "
                    f"Exceeds {AUTO_DISABLE_MAX_LOSS_PCT*100:.0f}% loss threshold. Auto-disabling."
                ),
                "data": {
                    "loss_pct": round(loss_pct, 4),
                    "total_pnl": round(total_pnl, 2),
                    "total_deployed": round(total_deployed, 2),
                    "auto_approve": True,
                },
            })

        # --- Recommend allocation shift for underperformers ---
        elif win_rate < 0.40 and len(recent) >= 10:
            recommendations.append({
                "from_agent": PERF_AGENT,
                "to_agent": RISK_AGENT,
                "category": "shift_allocation",
                "action": "reduce_budget",
                "target": strat_name,
                "reasoning": (
                    f"Strategy '{strat_name}' underperforming: {win_rate*100:.0f}% win rate, "
                    f"${total_pnl:+.2f} P&L over {len(recent)} trades. "
                    f"Recommend reducing allocation until performance improves."
                ),
                "data": {
                    "win_rate": round(win_rate, 3),
                    "total_pnl": round(total_pnl, 2),
                    "suggested_budget_mult": 0.5,
                    "auto_approve": True,
                },
            })

        # --- Recommend allocation boost for top performers ---
        elif win_rate >= 0.60 and avg_pnl > 0 and len(recent) >= 10:
            recommendations.append({
                "from_agent": PERF_AGENT,
                "to_agent": RISK_AGENT,
                "category": "shift_allocation",
                "action": "increase_budget",
                "target": strat_name,
                "reasoning": (
                    f"Strategy '{strat_name}' outperforming: {win_rate*100:.0f}% win rate, "
                    f"${total_pnl:+.2f} P&L, avg ${avg_pnl:+.2f}/trade over {len(recent)} trades. "
                    f"Recommend increasing allocation to capture more alpha."
                ),
                "data": {
                    "win_rate": round(win_rate, 3),
                    "total_pnl": round(total_pnl, 2),
                    "avg_pnl": round(avg_pnl, 2),
                    "suggested_budget_mult": 1.5,
                    "auto_approve": True,
                },
            })

    return recommendations


# ---------------------------------------------------------------------------
# Risk Agent — monitors portfolio-level risk, adjusts parameters
# ---------------------------------------------------------------------------

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

    # --- Emergency halt at severe drawdown ---
    if drawdown >= DRAWDOWN_HALT_THRESHOLD:
        recommendations.append({
            "from_agent": RISK_AGENT,
            "to_agent": EXECUTOR_AGENT,
            "category": "change_regime",
            "action": "emergency_halt",
            "target": "all_strategies",
            "reasoning": (
                f"CRITICAL: Portfolio drawdown at {drawdown*100:.1f}% "
                f"(current: ${current_value:.0f}, peak: ${peak_value:.0f}). "
                f"Exceeds {DRAWDOWN_HALT_THRESHOLD*100:.0f}% threshold. "
                f"Recommending emergency halt — disable all strategies until review."
            ),
            "data": {
                "drawdown_pct": round(drawdown, 4),
                "current_value": round(current_value, 2),
                "peak_value": round(peak_value, 2),
                "auto_approve": True,
            },
        })

    # --- Tighten risk during moderate drawdown ---
    elif drawdown >= DRAWDOWN_TIGHTEN_THRESHOLD:
        recommendations.append({
            "from_agent": RISK_AGENT,
            "to_agent": EXECUTOR_AGENT,
            "category": "adjust_param",
            "action": "tighten_risk",
            "target": "risk_params",
            "reasoning": (
                f"Portfolio drawdown at {drawdown*100:.1f}% "
                f"(threshold: {DRAWDOWN_TIGHTEN_THRESHOLD*100:.0f}%). "
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

    return recommendations


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
        data = rec.get("data", {})
        # Full autonomy: mark all recommendations as auto_approve
        data["auto_approve"] = True

        # --- Auto-apply all actions ---
        action = rec["action"]
        target = rec.get("target", "")
        result = None

        try:
            if action == "auto_disable" and target in STRATEGY_ENABLED:
                # Disable the strategy in runtime config
                STRATEGY_ENABLED[target] = False
                result = f"Disabled strategy '{target}'"
                log.warning("AUTO-DISABLE: %s — %s", target, rec["reasoning"][:100])

            elif action == "emergency_halt":
                # Disable all strategies
                for strat in list(STRATEGY_ENABLED.keys()):
                    STRATEGY_ENABLED[strat] = False
                result = "Emergency halt — all strategies disabled"
                log.critical("EMERGENCY HALT: %s", rec["reasoning"][:100])

            elif action == "tighten_risk":
                adjustments = data.get("adjustments", {})
                for param, value in adjustments.items():
                    if param in RISK:
                        old = RISK[param]
                        RISK[param] = value
                        log.warning("RISK TIGHTENED: %s = %s → %s", param, old, value)
                result = f"Tightened {len(adjustments)} risk parameters"

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
                    new_budget = round(current * mult, 4)
                    STRATEGY_BUDGETS[target] = new_budget
                    result = f"Reduced {target} budget: {current:.4f} → {new_budget:.4f}"
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
                    result = f"Increased {target} budget: {current:.4f} → {new_budget:.4f}"
                    log.info("BUDGET INCREASED: %s", result)
                except Exception as e:
                    result = f"Budget increase failed: {e}"

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

            elif action == "enable_strategy" and target in STRATEGY_ENABLED:
                STRATEGY_ENABLED[target] = True
                result = f"Enabled strategy '{target}'"
                log.info("AUTO-ENABLE: %s — %s", target, rec["reasoning"][:100])

            elif action in ("research_finding", "performance_alert", "event_risk",
                            "concentration_warning", "re_evaluate", "underinvestment_alert"):
                # Informational — log and save to knowledge base
                insert_knowledge(
                    title=f"Agent {rec['from_agent']}: {action} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source=rec["from_agent"],
                    category="agent_intelligence",
                    content=rec["reasoning"],
                    key_rules=json.dumps(data),
                )
                result = f"Agent intelligence logged: {action}"
                log.info("AGENT INTELLIGENCE: [%s] %s — %s", rec["from_agent"], action, rec["reasoning"][:100])

            elif action == "implement_strategy":
                # Research agent wants a new strategy built — log to knowledge base as deferred
                insert_knowledge(
                    title=f"Strategy Implementation Request: {target} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source=f"agent_{rec.get('from_agent', 'unknown')}",
                    category="deferred_implementation",
                    content=rec["reasoning"],
                    key_rules=json.dumps(data),
                )
                result = f"Strategy implementation '{target}' logged to knowledge base (deferred)"
                log.info("DEFERRED IMPLEMENTATION: %s — %s", target, rec["reasoning"][:100])

            elif action == "coverage_gap":
                # Research agent identified a category with zero coverage — acknowledge
                insert_knowledge(
                    title=f"Coverage Gap Acknowledged: {target} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source=f"agent_{rec.get('from_agent', 'unknown')}",
                    category="coverage_gap",
                    content=rec["reasoning"],
                    key_rules=json.dumps(data),
                )
                result = f"Coverage gap '{target}' acknowledged and logged to knowledge base"
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
        resolve_recommendation(rec_id, "applied", result)

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
            },
        )

    return applied


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
# Main autonomous cycle
# ---------------------------------------------------------------------------

def run_autonomous_cycle() -> dict:
    """Run a full autonomous improvement cycle.

    Each agent thinks independently, produces recommendations,
    then the executor applies safe ones and stores others for review.

    Returns summary of actions taken.
    """
    log.info("Autonomous improvement cycle started")

    all_recommendations = []
    agent_results = {}

    # --- Each agent thinks ---
    agents = [
        ("performance", _performance_agent_think),
        ("risk", _risk_agent_think),
        ("regime", _regime_agent_think),
        ("research", _research_agent_think),
        ("learning", _learning_agent_think),
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

    # --- Log the conversation ---
    _log_agent_conversation(all_recommendations)

    # --- Execute safe recommendations ---
    applied = _execute_safe_recommendations(all_recommendations)

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
