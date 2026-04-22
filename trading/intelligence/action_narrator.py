"""Generate human-readable narratives for system actions.

Each action type gets an interpretation that:
1. Explains what happened in plain language
2. Puts it in portfolio context (% of portfolio, relative to history)
3. Assesses the quality of the decision
4. Identifies what can be learned
5. Links to related actions

Narratives are generated via LLM when first requested, then cached in the
action_narratives table for instant retrieval on subsequent views.
"""

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Template fallbacks per category — these produce human-readable prose
TEMPLATES = {
    "trade": (
        "Executed a {side} order for {symbol} worth ${total:.2f}. "
        "This was driven by {strategy} with a signal strength of {strength}."
    ),
    "error": (
        "An error occurred during {action} for {symbol}. {details}. "
        "This may indicate a configuration issue or an external service disruption."
    ),
    "signal": "Signal aggregation completed across all active strategies. {details}.",
    "risk_block": (
        "Risk management blocked a trade on {symbol}: {details}. "
        "This protective measure prevents overexposure and maintains portfolio discipline."
    ),
    "strategy_run": (
        "Completed a full trading strategy cycle, evaluating market conditions "
        "and generating signals. {details}."
    ),
    "autonomous": (
        "The autonomous improvement agents reviewed current performance, positions, "
        "and market conditions. {details}."
    ),
    "scale_in": (
        "Added to a winning position in {symbol}, increasing exposure to a "
        "profitable trade. {details}."
    ),
    "circuit_breaker": (
        "Circuit breaker activated — a strategy has been temporarily suspended "
        "after consecutive losses to protect capital. {details}."
    ),
    "stop_loss": (
        "Stop loss triggered for {symbol}, automatically closing the position "
        "to limit downside risk. {details}."
    ),
    "operator": "Manual operator action: {action}. {details}.",
    "cleanup": "Routine system maintenance completed. {details}.",
    "scheduler": "Scheduled task executed: {action}. {details}.",
    "system": "System event: {action}. {details}.",
    "funnel": None,  # handled by _funnel_narrative
}


def get_or_generate_narrative(action: dict) -> dict:
    """Main entry point: get cached narrative or generate new one.

    Returns a dict with keys: narrative, interpretation, lessons, quality_score.
    """
    from trading.db.store import get_action_narrative

    action_id = action.get("id")
    if not action_id:
        return {
            "narrative": _template_narrative(action),
            "interpretation": {},
            "lessons": [],
            "quality_score": None,
        }

    # Check cache
    cached = get_action_narrative(action_id)
    if cached:
        return {
            "narrative": cached["narrative"],
            "interpretation": (
                json.loads(cached["interpretation"])
                if cached.get("interpretation")
                else {}
            ),
            "lessons": (
                json.loads(cached["lessons"])
                if cached.get("lessons")
                else []
            ),
            "quality_score": cached.get("quality_score"),
        }

    # Generate new
    return _generate_and_cache(action)


def _generate_and_cache(action: dict) -> dict:
    """Generate narrative via LLM and cache it."""
    from trading.db.store import insert_action_narrative

    try:
        result = _llm_generate(action)
        if result:
            insert_action_narrative(
                action_id=action["id"],
                narrative=result["narrative"],
                interpretation=json.dumps(result.get("interpretation", {})),
                lessons=json.dumps(result.get("lessons", [])),
                quality_score=result.get("quality_score"),
                model=result.get("model", "unknown"),
            )
            return result
    except Exception as e:
        log.warning(
            "LLM narrative generation failed for action #%s: %s",
            action.get("id"),
            e,
        )

    # Fallback to template
    narrative = _template_narrative(action)
    try:
        insert_action_narrative(
            action_id=action["id"],
            narrative=narrative,
            model="template",
        )
    except Exception:
        pass
    return {
        "narrative": narrative,
        "interpretation": {},
        "lessons": [],
        "quality_score": None,
    }


def _llm_generate(action: dict) -> dict | None:
    """Generate narrative using LLM."""
    from trading.llm.engine import ask_llm, _ascii_safe

    category = action.get("category", "unknown")
    data = action.get("data", {})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}

    # Strip non-ASCII from action fields — prevents UnicodeEncodeError on Claude path
    _s = lambda v: _ascii_safe(str(v)) if v is not None else "N/A"

    prompt = f"""Analyze this trading system action and provide a human-readable narrative.

Action Details:
- Category: {_s(category)}
- Action: {_s(action.get('action'))}
- Symbol: {_s(action.get('symbol'))}
- Details: {_s(action.get('details'))}
- Result: {_s(action.get('result'))}
- Timestamp: {_s(action.get('timestamp'))}
- Raw Data: {_ascii_safe(json.dumps(data, default=str)[:1000])}

Respond in this exact JSON format:
{{
    "narrative": "2-4 sentence human-readable explanation of what happened, why it matters, and what it means for the portfolio",
    "interpretation": {{
        "summary": "One sentence plain English summary",
        "context": "How this fits into the bigger picture",
        "assessment": "Was this a good decision? Why or why not?",
        "impact": "Effect on portfolio, risk, or system state"
    }},
    "lessons": ["lesson 1", "lesson 2"],
    "quality_score": 0.0 to 1.0
}}

Be analytical and specific. Reference actual numbers from the data. Be honest about mistakes."""

    system = (
        "You are a senior trading analyst reviewing automated trading system "
        "actions. Provide concise, analytical interpretations. Always respond "
        "with valid JSON only."
    )

    try:
        response = ask_llm(system, prompt, call_type="narrative")
        if not response or "LLM unavailable" in response:
            return None

        # Parse JSON from response — strip markdown code fences
        text = response.strip()
        # Remove ```json ... ``` wrapping
        import re
        fence_match = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()
        # Also handle case where just ``` prefix/suffix without json tag
        elif text.startswith("```"):
            text = text.lstrip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
            if text.endswith("```"):
                text = text[:-3].strip()

        result = json.loads(text)
        result["model"] = "llm"
        return result
    except json.JSONDecodeError as e:
        log.warning("Failed to parse LLM narrative JSON: %s", e)
        # Try to salvage — use the raw response as narrative text
        if response:
            return {
                "narrative": response[:500],
                "interpretation": {},
                "lessons": [],
                "quality_score": None,
                "model": "llm_raw",
            }
        return None
    except Exception as e:
        log.warning("LLM narrative generation error: %s", e)
        return None


def _template_narrative(action: dict) -> str:
    """Generate a template-based narrative fallback with smart data parsing."""
    category = action.get("category", "unknown")
    action_name = action.get("action", "unknown")
    details = action.get("details", "")

    data = action.get("data", {})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}

    # --- Smart narratives for known action types ---

    # Funnel actions: parse Gen/Con/Act/Exec pattern
    if category == "funnel" or action_name == "cycle_funnel":
        return _funnel_narrative(data, details)

    # System actions with known patterns
    if category == "system" or action_name in (
        "sync_positions", "pair_trades", "cycle_complete",
        "pnl_snapshot", "daily_pnl",
    ):
        return _system_narrative(action_name, data, details)

    # Strategy runs
    if category == "strategy_run":
        return _strategy_run_narrative(data, details)

    # Fall back to template
    template = TEMPLATES.get(category, "System event: {action}. {details}.")
    if template is None:
        template = "System event: {action}. {details}."

    ctx = {
        "side": action.get("action", data.get("side", "unknown")),
        "symbol": action.get("symbol", "unknown"),
        "total": float(data.get("total", data.get("notional", 0))),
        "strategy": data.get("strategy", action_name),
        "strength": data.get("strength", data.get("signal_strength", "N/A")),
        "action": action_name,
        "details": details or "No details available.",
        "result": action.get("result", ""),
    }

    try:
        return template.format(**ctx)
    except (KeyError, ValueError):
        return (
            f"{category.title()} action: {action_name} "
            f"— {details or 'No details'}."
        )


def _funnel_narrative(data: dict, details: str) -> str:
    """Human-readable narrative for cycle_funnel actions."""
    gen = data.get("generated", 0)
    con = data.get("consolidated", 0)
    act = data.get("actionable", 0)
    exc = data.get("executed", 0)
    blocked = data.get("blocked", 0)
    cycle_time = data.get("cycle_time_s", 0)

    # Parse from details string if data dict is empty
    if gen == 0 and details:
        import re
        m = re.search(r"Gen:(\d+)", details)
        if m:
            gen = int(m.group(1))
        m = re.search(r"Con:(\d+)", details)
        if m:
            con = int(m.group(1))
        m = re.search(r"Act:(\d+)", details)
        if m:
            act = int(m.group(1))
        m = re.search(r"Exec:(\d+)", details)
        if m:
            exc = int(m.group(1))

    parts = []
    parts.append(
        f"The trading engine scanned all active strategies and generated "
        f"{gen} raw signals."
    )
    if con > 0:
        parts.append(
            f"After cross-strategy consolidation, {con} unique opportunities "
            f"emerged from the noise."
        )
    if act > 0 and act != con:
        parts.append(f"{act} signals passed risk filters and were actionable.")
    if exc > 0:
        parts.append(f"{exc} trade{'s' if exc != 1 else ''} successfully executed.")
    elif act > 0:
        parts.append("No trades were executed this cycle.")
    if blocked > 0:
        parts.append(
            f"{blocked} signal{'s' if blocked != 1 else ''} blocked by risk management."
        )
    if cycle_time > 0:
        parts.append(f"Cycle completed in {cycle_time:.1f}s.")

    return " ".join(parts)


def _system_narrative(action_name: str, data: dict, details: str) -> str:
    """Human-readable narrative for common system actions."""
    if action_name == "sync_positions":
        import re
        m = re.search(r"(\d+)", details) if details else None
        count = m.group(1) if m else "all"
        return (
            f"Synchronized {count} open positions with the exchange, "
            f"updating mark prices, unrealized P&L, and margin status."
        )
    if action_name == "pair_trades":
        import re
        m = re.search(r"(\d+)", details) if details else None
        count = m.group(1) if m else "0"
        return (
            f"Matched {count} completed trade pairs (entry ↔ exit) to calculate "
            f"realized P&L and update performance metrics."
        )
    if action_name == "cycle_complete":
        return (
            f"Full trading cycle completed. {details}. "
            f"All strategies evaluated, risk checks applied, and positions updated."
        )
    if action_name == "pnl_snapshot":
        return (
            f"Recorded a portfolio snapshot for performance tracking. {details}."
        )
    if action_name == "daily_pnl":
        return (
            f"Daily P&L checkpoint recorded. {details}."
        )
    return f"System maintenance: {action_name}. {details}."


def _strategy_run_narrative(data: dict, details: str) -> str:
    """Human-readable narrative for strategy_run actions."""
    strategies_run = data.get("strategies_run", [])
    total = data.get("total_signals", 0)
    if strategies_run:
        return (
            f"Executed {len(strategies_run)} trading strategies "
            f"({', '.join(strategies_run[:5])}{'...' if len(strategies_run) > 5 else ''}) "
            f"generating {total} signals. {details}."
        )
    return (
        f"Completed a full trading strategy cycle, evaluating market conditions "
        f"and generating signals. {details}."
    )


def generate_missing_narratives(limit: int = 20) -> int:
    """Generate narratives for recent actions that don't have one yet,
    AND replace template-fallback narratives with LLM-generated ones."""
    from trading.db.store import get_db

    with get_db() as conn:
        rows = conn.execute(
            "SELECT al.* FROM action_log al "
            "LEFT JOIN action_narratives an ON al.id = an.action_id "
            "WHERE an.action_id IS NULL OR an.model = 'template' "
            "ORDER BY al.timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()

    count = 0
    for row in rows:
        action = dict(row)
        try:
            # Call _generate_and_cache directly — bypasses the cache check in
            # get_or_generate_narrative() so template entries get overwritten.
            _generate_and_cache(action)
            count += 1
        except Exception as e:
            log.warning(
                "Failed to generate narrative for action #%s: %s",
                action.get("id"),
                e,
            )

    if count:
        log.info("Generated/upgraded %d narratives (including template replacements)", count)
    return count


def get_recent_lessons(limit: int = 20) -> list[str]:
    """Get recent lessons from action narratives for agent context."""
    from trading.db.store import get_recent_action_lessons

    return get_recent_action_lessons(limit)
