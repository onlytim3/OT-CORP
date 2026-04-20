"""Daily Journal Agent — reads the journal and executes system improvements.

This agent is an autonomous executor, not a recommender.  It reads the daily
journal (LLM-generated narrative of recent system activity), extracts actionable
needs, and executes them directly — adjusting risk, enabling/disabling strategies,
tightening parameters, and logging every action taken.

Workflow:
  1. Read today's journal from action_log (LLM-generated narrative)
  2. If no journal, synthesise from raw trades / P&L / signals
  3. Identify specific needs via rule engine + LLM parsing
  4. Execute actions directly — no queuing, no waiting
  5. Write a "plan log" entry describing what was done and why

Called from run_trading_cycle() Phase 7.5 (after autonomous, before alerts).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta

from trading.config import RISK, STRATEGY_ENABLED
from trading.db.store import (
    get_db, get_trades, get_daily_pnl, get_action_log,
    insert_knowledge, log_action, get_setting, set_setting,
)
from trading.llm.engine import ask_llm

log = logging.getLogger(__name__)

_MAX_ACTIONS_PER_CYCLE = 5  # Guard: never do more than 5 things in one cycle


# ---------------------------------------------------------------------------
# Step 1 — Read today's journal
# ---------------------------------------------------------------------------

def _read_todays_journal() -> str | None:
    """Return the LLM-generated daily journal text if it exists for today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entries = get_action_log(limit=10, category="journal")
    for e in entries:
        if (e.get("timestamp", "") or "").startswith(today):
            data = e.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    pass
            if isinstance(data, dict) and data.get("content"):
                return data["content"]
            if e.get("details"):
                return e["details"]
    return None


def _build_context_without_journal() -> str:
    """Build a plain-text context summary when no journal exists."""
    trades = get_trades(limit=30)
    pnl_data = get_daily_pnl(limit=7)

    strat_stats: dict[str, dict] = {}
    for t in trades:
        s = t.get("strategy", "unknown")
        if s not in strat_stats:
            strat_stats[s] = {"count": 0, "pnl": 0.0}
        strat_stats[s]["count"] += 1
        strat_stats[s]["pnl"] += t.get("pnl") or 0

    pnl_7d = sum(d.get("daily_return", 0) for d in pnl_data)
    worst = min((d.get("daily_return", 0) for d in pnl_data), default=0)

    lines = [f"System context (auto-built, no journal today):"]
    lines.append(f"7-day P&L: {pnl_7d:+.2f}%, worst day: {worst:+.2f}%")
    lines.append(f"Active strategies: {sum(1 for v in STRATEGY_ENABLED.values() if v)}/{len(STRATEGY_ENABLED)}")
    for strat, st in sorted(strat_stats.items(), key=lambda x: x[1]["pnl"]):
        lines.append(f"  {strat}: {st['count']} trades, P&L ${st['pnl']:+.2f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 2 — Rule-based need detection
# ---------------------------------------------------------------------------

def _detect_needs() -> list[dict]:
    """Identify concrete, actionable needs without LLM."""
    needs = []
    trades = get_trades(limit=100)
    pnl_data = get_daily_pnl(limit=14)

    # Aggregate per strategy
    strat_stats: dict[str, dict] = {}
    for t in trades:
        s = t.get("strategy", "unknown")
        if s not in strat_stats:
            strat_stats[s] = {"count": 0, "wins": 0, "pnl": 0.0}
        strat_stats[s]["count"] += 1
        strat_stats[s]["pnl"] += t.get("pnl") or 0
        if (t.get("pnl") or 0) > 0:
            strat_stats[s]["wins"] += 1

    # Underperforming strategies → disable
    for strat, st in strat_stats.items():
        if not STRATEGY_ENABLED.get(strat, False):
            continue  # Already disabled
        if st["count"] < 15:
            continue  # Not enough data
        win_rate = st["wins"] / st["count"]
        if win_rate < 0.25:
            needs.append({
                "action": "disable_strategy",
                "strategy": strat,
                "reason": f"Win rate {win_rate:.0%} over {st['count']} trades (threshold: 25%)",
                "priority": "high",
            })
        elif st["pnl"] < -200 and st["count"] >= 20:
            needs.append({
                "action": "disable_strategy",
                "strategy": strat,
                "reason": f"Down ${st['pnl']:,.2f} over {st['count']} trades",
                "priority": "high",
            })

    # Portfolio drawdown → tighten risk
    if pnl_data:
        pnl_7d = sum(d.get("daily_return", 0) for d in pnl_data[:7])
        if pnl_7d < -8:
            needs.append({
                "action": "tighten_risk",
                "param": "max_position_pct",
                "new_value": max(0.05, RISK.get("max_position_pct", 0.10) * 0.80),
                "reason": f"7-day P&L {pnl_7d:+.2f}% — reducing position cap by 20%",
                "priority": "high",
            })
        elif pnl_7d < -4:
            needs.append({
                "action": "tighten_risk",
                "param": "stop_loss_pct",
                "new_value": max(0.02, RISK.get("stop_loss_pct", 0.05) * 0.85),
                "reason": f"7-day P&L {pnl_7d:+.2f}% — tightening stop losses",
                "priority": "medium",
            })

    # Coverage gap → re-enable safe strategies
    active_count = sum(1 for v in STRATEGY_ENABLED.values() if v)
    if active_count < 8:
        needs.append({
            "action": "coverage_review",
            "active": active_count,
            "reason": f"Only {active_count} strategies active — coverage is thin",
            "priority": "low",
        })

    return needs


# ---------------------------------------------------------------------------
# Step 3 — LLM action extraction from journal
# ---------------------------------------------------------------------------

def _extract_actions_from_journal(journal_text: str) -> list[dict]:
    """Ask LLM to extract specific executable actions from the journal."""
    known_strategies = list(STRATEGY_ENABLED.keys())

    system = (
        "You are a trading system executive agent. You read daily trading journals "
        "and extract SPECIFIC executable actions to improve the system.\n\n"
        "Return a JSON array of actions. Each action must have these exact keys:\n"
        "  action: one of [disable_strategy, enable_strategy, set_risk_param, adjust_leverage, log_insight]\n"
        "  target: strategy name (from list) or risk param name or insight text\n"
        "  value: new value if applicable (null otherwise)\n"
        "  reason: 1-sentence justification from the journal\n"
        "  priority: high | medium | low\n\n"
        f"Known strategy names: {', '.join(known_strategies)}\n"
        f"Known risk params: stop_loss_pct, max_position_pct, max_daily_loss_pct, max_trades_per_day\n"
        f"Current state: {sum(1 for v in STRATEGY_ENABLED.values() if v)}/{len(STRATEGY_ENABLED)} strategies active\n\n"
        "CRITICAL RULES:\n"
        "- Only suggest disable_strategy if the journal explicitly mentions a strategy underperforming\n"
        "- Only suggest enable_strategy for strategies explicitly mentioned as good candidates to re-enable\n"
        "- Only suggest set_risk_param with concrete numeric values\n"
        "- Maximum 3 actions total\n"
        "- Return ONLY valid JSON array, no other text"
    )

    try:
        response = ask_llm(
            prompt=f"Journal:\n{journal_text[:3000]}",
            system=system,
            max_tokens=400,
        )
        if not response:
            return []

        # Strip markdown fences
        response = re.sub(r"^```(?:json)?\s*", "", response.strip())
        response = re.sub(r"\s*```$", "", response.strip())

        actions = json.loads(response)
        if isinstance(actions, list):
            return actions[:3]
    except Exception as e:
        log.debug("LLM action extraction failed: %s", e)

    return []


# ---------------------------------------------------------------------------
# Step 4 — Execute actions directly
# ---------------------------------------------------------------------------

def _execute_action(action: dict) -> str:
    """Execute a single action and return a result string."""
    atype = action.get("action", "")
    # Accept both "target" and "strategy"/"param" keys (LLM vs rule-based needs)
    target = action.get("target") or action.get("strategy") or action.get("param") or ""
    value = action.get("value")
    if value is None:
        value = action.get("new_value")
    reason = action.get("reason", "Journal agent")

    if atype == "disable_strategy":
        strategy = target
        if strategy not in STRATEGY_ENABLED:
            return f"skip: unknown strategy {strategy}"
        if not STRATEGY_ENABLED.get(strategy, False):
            return f"skip: {strategy} already disabled"

        set_setting(f"strategy_override_{strategy}", "disabled")
        STRATEGY_ENABLED[strategy] = False
        log_action("journal_agent", "disable_strategy",
                  details=f"Disabled {strategy}: {reason}")
        return f"disabled {strategy}"

    elif atype == "enable_strategy":
        strategy = target
        if strategy not in STRATEGY_ENABLED:
            return f"skip: unknown strategy {strategy}"
        if STRATEGY_ENABLED.get(strategy, False):
            return f"skip: {strategy} already enabled"

        set_setting(f"strategy_override_{strategy}", "enabled")
        STRATEGY_ENABLED[strategy] = True
        log_action("journal_agent", "enable_strategy",
                  details=f"Enabled {strategy}: {reason}")
        return f"enabled {strategy}"

    elif atype == "set_risk_param" or atype == "tighten_risk":
        param = target
        if not param:
            return "skip: no param specified"
        if value is None:
            return "skip: no value specified"

        try:
            new_val = float(value)
        except (TypeError, ValueError):
            return f"skip: invalid value {value}"

        old_val = RISK.get(param)
        RISK[param] = new_val
        override_data = {
            "value": new_val,
            "old_value": old_val,
            "set_at": datetime.now(timezone.utc).isoformat(),
            "source": "journal_agent",
        }
        set_setting(f"risk_override_{param}", json.dumps(override_data))
        log_action("journal_agent", "set_risk_param",
                  details=f"{param}: {old_val} → {new_val} ({reason})")
        return f"set {param}: {old_val} → {new_val}"

    elif atype == "log_insight":
        insert_knowledge(
            title=f"Journal Insight {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            source="journal_agent",
            category="journal_insight",
            content=target or reason,
        )
        return f"logged insight: {(target or reason)[:80]}"

    elif atype == "coverage_review":
        # When strategies are disabled (e.g. emergency halt), log diagnostic insight
        disabled = [s for s, v in STRATEGY_ENABLED.items() if not v]
        if disabled:
            insight = (
                f"Coverage review: {len(disabled)}/{len(STRATEGY_ENABLED)} strategies disabled. "
                f"Reason for review: {reason}. "
                f"Disabled strategies: {', '.join(disabled[:10])}"
                f"{'...' if len(disabled) > 10 else ''}"
            )
            insert_knowledge(
                title=f"Coverage Review {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                source="journal_agent",
                category="journal_insight",
                content=insight,
            )
            log_action("journal_agent", "coverage_review",
                      details=f"Logged coverage review insight: {len(disabled)} disabled")
            return f"logged coverage insight ({len(disabled)} disabled)"
        return "skip: no disabled strategies found"

    elif atype == "check_recovery":
        # If in recovery mode and drawdown has improved, attempt graduation
        try:
            from trading.strategy.circuit_breaker import get_recovery_mode, force_graduate
            mode = get_recovery_mode()
            if not mode.get("active"):
                return "skip: not in recovery mode"

            # Pull recent P&L; if 7-day return is positive, graduate
            pnl_data = get_daily_pnl(limit=7)
            pnl_7d = sum(d.get("daily_return", 0) for d in pnl_data) if pnl_data else 0
            if pnl_7d > 0:
                force_graduate(reason=f"Journal agent: 7-day P&L recovered to {pnl_7d:+.2f}%")
                log_action("journal_agent", "force_graduate",
                          details=f"Graduated from recovery: 7-day P&L {pnl_7d:+.2f}%")
                return f"graduated from recovery (7-day P&L: {pnl_7d:+.2f}%)"
            return f"skip: still in drawdown (7-day P&L: {pnl_7d:+.2f}%)"
        except Exception as e:
            return f"error: {e}"

    return f"skip: unknown action type {atype}"


# ---------------------------------------------------------------------------
# Step 5 — Log the plan
# ---------------------------------------------------------------------------

def _write_plan_log(journal_text: str | None, needs: list[dict],
                   executed: list[tuple[dict, str]]) -> None:
    """Write a structured plan log to the knowledge base."""
    ts = datetime.now(timezone.utc).isoformat()
    lines = [f"# Journal Agent Plan — {ts[:10]}\n"]

    lines.append("## Source")
    lines.append("Journal" if journal_text else "Auto-built context (no journal today)")

    lines.append("\n## Needs Detected")
    if needs:
        for n in needs:
            lines.append(f"- [{n['priority']}] {n.get('reason', n.get('action', '?'))}")
    else:
        lines.append("- None detected")

    lines.append("\n## Actions Executed")
    if executed:
        for action, result in executed:
            atype = action.get("action", "?")
            reason = action.get("reason", "")
            lines.append(f"- **{atype}** → {result} ({reason})")
    else:
        lines.append("- No actions taken this cycle")

    plan_text = "\n".join(lines)
    insert_knowledge(
        title=f"Journal Agent Plan {ts[:10]}",
        source="journal_agent",
        category="daily_plan",
        content=plan_text,
    )
    deferred = sum(1 for n in needs if n["priority"] == "low")
    log_action("journal_agent", "plan_logged",
              details=(
                  f"{len(needs)} needs detected, {len(executed)} actions executed"
                  + (f", {deferred} low-priority need(s) deferred" if deferred else "")
              ))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_journal_agent() -> dict:
    """Read journal, detect needs, execute actions, log plan.

    Returns a dict with execution summary.
    """
    log.info("Journal Agent: starting")

    # 1. Get journal or build context
    journal_text = _read_todays_journal()
    context = journal_text or _build_context_without_journal()

    # 2. Rule-based need detection (always runs)
    needs = _detect_needs()

    # 3. LLM action extraction from journal (if available)
    llm_actions = _extract_actions_from_journal(context) if context else []

    # 4. Merge: rule actions + LLM actions
    all_actions: list[dict] = []

    # Rule-based needs → include high + medium; always include coverage_review
    for need in needs:
        if need["priority"] in ("high", "medium"):
            all_actions.append(need)
        elif need.get("action") == "coverage_review":
            all_actions.append(need)

    # Auto-inject check_recovery when recovery mode is active
    try:
        from trading.strategy.circuit_breaker import get_recovery_mode
        if get_recovery_mode().get("active"):
            all_actions.append({
                "action": "check_recovery",
                "reason": "Recovery mode active — attempt graduation if conditions met",
                "priority": "medium",
            })
    except Exception:
        pass

    # LLM actions
    for a in llm_actions:
        if a.get("action") in ("disable_strategy", "enable_strategy",
                               "set_risk_param", "log_insight"):
            all_actions.append(a)

    # Cap to MAX_ACTIONS_PER_CYCLE
    all_actions = all_actions[:_MAX_ACTIONS_PER_CYCLE]

    # 5. Execute all actions
    executed: list[tuple[dict, str]] = []
    for action in all_actions:
        try:
            result = _execute_action(action)
            executed.append((action, result))
            log.info("Journal Agent executed: %s → %s", action.get("action"), result)
        except Exception as e:
            log.warning("Journal Agent action failed: %s — %s", action, e)
            executed.append((action, f"error: {e}"))

    # 6. Fill counterfactual exits with current prices (non-blocking)
    try:
        from trading.db.store import fill_counterfactual_exits
        from trading.data.aster import get_aster_market_summary
        summary_data = get_aster_market_summary() or {}
        # Build price map from any available price field in market summary
        prices: dict[str, float] = {}
        try:
            from trading.execution.aster_client import get_aster_mark_prices
            for entry in (get_aster_mark_prices() or []):
                sym = entry.get("symbol", "")
                mp = entry.get("markPrice")
                if sym and mp:
                    prices[sym] = float(mp)
        except Exception:
            pass
        filled = fill_counterfactual_exits(prices)
        if filled:
            log.info("Journal Agent: filled %d counterfactual exits", filled)
    except Exception as e:
        log.debug("Counterfactual fill skipped: %s", e)

    # 7. Write plan log
    _write_plan_log(journal_text, needs, executed)

    summary = {
        "status": "ok",
        "had_journal": journal_text is not None,
        "needs_detected": len(needs),
        "llm_actions": len(llm_actions),
        "actions_executed": len(executed),
        "results": [r for _, r in executed],
    }
    log.info("Journal Agent: done — %d actions executed", len(executed))
    return summary
