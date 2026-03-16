"""Tiered LLM engine — Claude Sonnet for quality, Gemini Flash for bulk.

Usage:
    from trading.llm.engine import ask_llm, explain_trade, generate_journal

Environment variables:
    ANTHROPIC_API_KEY  — Claude API key (primary, quality tasks)
    GEMINI_API_KEY     — Google Gemini API key (fallback, bulk tasks)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Literal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider backends
# ---------------------------------------------------------------------------

def _call_claude(system: str, prompt: str, model: str = "claude-sonnet-4-20250514") -> str | None:
    """Call Claude API. Returns response text or None on failure."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        log.warning("Claude API error: %s", e)
        return None


def _call_gemini(system: str, prompt: str, model: str = "gemini-2.0-flash") -> str | None:
    """Call Gemini API. Returns response text or None on failure."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        gen_model = genai.GenerativeModel(
            model_name=model,
            system_instruction=system,
        )
        response = gen_model.generate_content(prompt)
        return response.text
    except Exception as e:
        log.warning("Gemini API error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Tiered dispatch
# ---------------------------------------------------------------------------

Tier = Literal["quality", "bulk"]

def ask_llm(system: str, prompt: str, tier: Tier = "quality") -> str:
    """Send a prompt to the LLM tier.

    - "quality" tier: Claude Sonnet first, Gemini Flash fallback
    - "bulk" tier: Gemini Flash first, Claude fallback

    Always returns a string (graceful degradation to a static message).
    """
    if tier == "quality":
        result = _call_claude(system, prompt)
        if result:
            return result
        result = _call_gemini(system, prompt)
        if result:
            return result
    else:
        result = _call_gemini(system, prompt)
        if result:
            return result
        result = _call_claude(system, prompt)
        if result:
            return result

    return "(LLM unavailable — set ANTHROPIC_API_KEY or GEMINI_API_KEY in .env)"


# ---------------------------------------------------------------------------
# System prompt — trading system context
# ---------------------------------------------------------------------------

TRADING_SYSTEM_PROMPT = """You are the AI co-pilot for OT-CORP, an autonomous crypto & commodities trading system.

Your role:
- Explain trades, signals, and system decisions in clear, concise language
- Analyze P&L, risk events, and strategy performance
- Generate insightful daily journal entries
- Answer operator questions about the system's behavior

Style guidelines:
- Be direct and specific — cite numbers, strategies, timestamps
- Use markdown formatting (bold, tables, bullet points)
- When explaining a trade: cover the signal source, confluence score, leverage, risk checks passed, and sizing logic
- When analyzing P&L: identify contributing strategies, market conditions, and what worked/didn't
- Keep responses under 300 words unless explicitly asked for detail
- Never fabricate data — if information is missing, say so

System architecture:
- 23 strategies across crypto, equities, and commodities
- Confluence-based signal aggregation (multiple strategies must agree)
- ATR-based dynamic stop losses adjusted by leverage
- 4 leverage profiles (conservative/moderate/aggressive/greedy), currently on aggressive
- Risk checks: position size, sector exposure, correlation groups, cash reserve, drawdown, leverage cap (5x)
- Autonomous learning agents that recommend parameter changes
- Circuit breaker (5 consecutive losses = 48h cooldown)
- TWAP execution for large orders, limit orders with market fallback
"""


# ---------------------------------------------------------------------------
# High-level functions (quality tier)
# ---------------------------------------------------------------------------

def explain_trade(trade: dict, context: dict | None = None) -> str:
    """Explain why a trade was made, in natural language."""
    prompt = f"Explain this trade decision:\n\n{json.dumps(trade, indent=2, default=str)}"
    if context:
        prompt += f"\n\nAdditional context:\n{json.dumps(context, indent=2, default=str)}"
    prompt += "\n\nExplain: What triggered this trade? What was the reasoning? Was it a good decision given the data?"
    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, tier="quality")


def generate_journal(trades: list[dict], positions: list[dict], pnl: list[dict],
                     signals: list[dict], actions: list[dict]) -> str:
    """Generate a daily trading journal entry."""
    now = datetime.now(timezone.utc)
    prompt = f"""Generate a daily trading journal for {now.strftime('%Y-%m-%d')}.

Recent trades: {json.dumps(trades[:15], indent=2, default=str)}

Current positions: {json.dumps(positions[:10], indent=2, default=str)}

Recent P&L: {json.dumps(pnl[:7], indent=2, default=str)}

Key signals: {json.dumps(signals[:10], indent=2, default=str)}

System actions: {json.dumps(actions[:10], indent=2, default=str)}

Write a concise journal entry covering:
1. What happened today (trades, signals, market conditions)
2. What worked and what didn't
3. Key risk events or blocks
4. Lessons and observations
5. Tomorrow's outlook based on current positions and signals"""

    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, tier="quality")


def analyze_performance(pnl_data: list[dict], strategies: list[dict]) -> str:
    """Analyze trading performance over a period."""
    prompt = f"""Analyze this trading performance:

P&L history: {json.dumps(pnl_data[:30], indent=2, default=str)}

Strategy performance: {json.dumps(strategies[:20], indent=2, default=str)}

Provide:
1. Overall performance summary
2. Best and worst performing strategies
3. Risk-adjusted return assessment
4. Specific actionable recommendations"""

    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, tier="quality")


def interpret_risk_event(event: dict, positions: list[dict]) -> str:
    """Explain a risk block or margin event."""
    prompt = f"""Explain this risk event to the operator:

Event: {json.dumps(event, indent=2, default=str)}

Current positions: {json.dumps(positions[:10], indent=2, default=str)}

Explain what triggered the risk block, whether it was appropriate, and what the operator should consider."""

    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, tier="quality")


# ---------------------------------------------------------------------------
# Bulk tier functions (Gemini Flash)
# ---------------------------------------------------------------------------

def summarize_signals(signals: list[dict]) -> str:
    """Quick summary of signal batch (bulk tier)."""
    prompt = f"Summarize these trading signals in 2-3 sentences:\n{json.dumps(signals[:20], default=str)}"
    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, tier="bulk")


def annotate_action(action: dict) -> str:
    """Add a one-sentence annotation to a system action (bulk tier)."""
    prompt = f"In one sentence, explain what this system action means for the portfolio:\n{json.dumps(action, default=str)}"
    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, tier="bulk")


# ---------------------------------------------------------------------------
# Chat with full context
# ---------------------------------------------------------------------------

def chat_with_context(message: str, context: dict) -> str:
    """Answer an operator question with full trading system context."""
    prompt = f"""The operator asks: "{message}"

Current system state:
{json.dumps(context, indent=2, default=str)}

Answer the operator's question using the system data above. Be specific and cite actual numbers from the data."""

    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, tier="quality")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_llm_availability() -> dict:
    """Check which LLM providers are configured and reachable."""
    status = {
        "claude": {"configured": bool(os.getenv("ANTHROPIC_API_KEY")), "reachable": False},
        "gemini": {"configured": bool(os.getenv("GEMINI_API_KEY")), "reachable": False},
    }

    if status["claude"]["configured"]:
        result = _call_claude("Say OK", "ping", model="claude-sonnet-4-20250514")
        status["claude"]["reachable"] = result is not None

    if status["gemini"]["configured"]:
        result = _call_gemini("Say OK", "ping")
        status["gemini"]["reachable"] = result is not None

    status["any_available"] = any(p["reachable"] for p in status.values() if isinstance(p, dict))
    return status
