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

# Template fallbacks per category
TEMPLATES = {
    "trade": (
        "The system executed a {side} order for {symbol} worth ${total:.2f}. "
        "Strategy: {strategy}. Signal strength: {strength}."
    ),
    "error": (
        "An error occurred: {action} for {symbol}. Details: {details}. "
        "This may indicate a configuration issue or external service problem."
    ),
    "signal": "Signal aggregation completed. {details}.",
    "risk_block": (
        "Risk manager blocked a trade: {details}. "
        "The system's safety guardrails are functioning correctly."
    ),
    "strategy_run": "Trading cycle completed. {details}.",
    "autonomous": "Autonomous agents completed a review cycle. {details}.",
    "scale_in": "Added to winning position in {symbol}. {details}.",
    "circuit_breaker": (
        "Circuit breaker activated: {details}. "
        "The strategy has been temporarily disabled."
    ),
    "stop_loss": "Stop loss triggered for {symbol}. {details}.",
    "operator": "Manual action via operator console: {action}. {details}.",
    "cleanup": "System cleanup performed. {details}.",
    "scheduler": "Scheduler event: {action}. {details}.",
    "system": "System event: {action}. {details}.",
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
    from trading.llm.engine import ask_llm

    category = action.get("category", "unknown")
    data = action.get("data", {})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    prompt = f"""Analyze this trading system action and provide a human-readable narrative.

Action Details:
- Category: {category}
- Action: {action.get('action', 'N/A')}
- Symbol: {action.get('symbol', 'N/A')}
- Details: {action.get('details', 'N/A')}
- Result: {action.get('result', 'N/A')}
- Timestamp: {action.get('timestamp', 'N/A')}
- Raw Data: {json.dumps(data, default=str)[:1000]}

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

        # Parse JSON from response — handle potential markdown code blocks
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            if text.startswith("json"):
                text = text[4:].strip()

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
    """Generate a template-based narrative fallback."""
    category = action.get("category", "unknown")
    template = TEMPLATES.get(category, "System action: {action}. {details}.")

    data = action.get("data", {})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    # Build context dict from action + data
    ctx = {
        "side": action.get("action", data.get("side", "unknown")),
        "symbol": action.get("symbol", "unknown"),
        "total": float(data.get("total", data.get("notional", 0))),
        "strategy": data.get("strategy", action.get("action", "unknown")),
        "strength": data.get("strength", data.get("signal_strength", "N/A")),
        "action": action.get("action", "unknown"),
        "details": action.get("details", "No details available."),
        "result": action.get("result", ""),
    }

    try:
        return template.format(**ctx)
    except (KeyError, ValueError):
        return (
            f"{category.title()} action: {action.get('action', 'unknown')} "
            f"-- {action.get('details', 'No details')}."
        )


def generate_missing_narratives(limit: int = 20) -> int:
    """Generate narratives for recent actions that don't have one yet."""
    from trading.db.store import get_db

    with get_db() as conn:
        rows = conn.execute(
            "SELECT al.* FROM action_log al "
            "LEFT JOIN action_narratives an ON al.id = an.action_id "
            "WHERE an.action_id IS NULL "
            "ORDER BY al.timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()

    count = 0
    for row in rows:
        action = dict(row)
        try:
            get_or_generate_narrative(action)
            count += 1
        except Exception as e:
            log.warning(
                "Failed to generate narrative for action #%s: %s",
                action.get("id"),
                e,
            )

    if count:
        log.info("Generated %d narratives for recent actions", count)
    return count


def get_recent_lessons(limit: int = 20) -> list[str]:
    """Get recent lessons from action narratives for agent context."""
    from trading.db.store import get_recent_action_lessons

    return get_recent_action_lessons(limit)
