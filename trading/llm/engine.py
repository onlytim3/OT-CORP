"""Gemini-powered LLM engine for OT-CORP trading system.

All LLM calls route through Gemini with call-type-specific model selection:
  - gemini-2.5-flash: fast/cheap for narratives, annotations, pre-trade reasoning
  - gemini-2.5-flash: deeper reasoning for chat, analysis, reviews (same model, higher token budget)

Usage:
    from trading.llm.engine import ask_llm, explain_trade, generate_journal

Environment variables:
    GEMINI_API_KEY — Google Gemini API key (required)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------

MODEL_FLASH = "gemini-2.5-flash"
MODEL_THINKING = "gemini-2.5-flash"

# Per-call-type configuration: model selection + token budget + thinking budget
# For Gemini 2.5 models, max_tokens includes thinking tokens.
# thinking_budget limits reasoning tokens so more go to visible output.
CALL_PROFILES: dict[str, dict] = {
    "chat":             {"model": MODEL_THINKING, "max_tokens": 8000, "thinking_budget": 4096},
    "chat_full":        {"model": MODEL_THINKING, "max_tokens": 12000, "thinking_budget": 8192},
    "narrative":        {"model": MODEL_FLASH,    "max_tokens": 2000, "thinking_budget": 512},
    "explain_trade":    {"model": MODEL_THINKING, "max_tokens": 4000, "thinking_budget": 1024},
    "journal":          {"model": MODEL_THINKING, "max_tokens": 5000, "thinking_budget": 1024},
    "performance":      {"model": MODEL_THINKING, "max_tokens": 4000, "thinking_budget": 1024},
    "risk_event":       {"model": MODEL_THINKING, "max_tokens": 2000, "thinking_budget": 512},
    "signal_summary":   {"model": MODEL_FLASH,    "max_tokens": 1000, "thinking_budget": 256},
    "annotate":         {"model": MODEL_FLASH,    "max_tokens": 500,  "thinking_budget": 128},
    "pre_trade":        {"model": MODEL_FLASH,    "max_tokens": 2000, "thinking_budget": 512},
    "post_trade":       {"model": MODEL_THINKING, "max_tokens": 3000, "thinking_budget": 1024},
    "agent_synthesis":  {"model": MODEL_THINKING, "max_tokens": 4000, "thinking_budget": 1024},
    "weekly_synthesis": {"model": MODEL_THINKING, "max_tokens": 6000, "thinking_budget": 1024},
    "news_analysis":    {"model": MODEL_THINKING, "max_tokens": 5000, "thinking_budget": 1024},
}


# ---------------------------------------------------------------------------
# Rate limiter (simple in-process token bucket)
# ---------------------------------------------------------------------------

_last_call_time = 0.0
_MIN_INTERVAL = 0.5  # seconds between calls


def _rate_limit():
    global _last_call_time
    elapsed = time.monotonic() - _last_call_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_call_time = time.monotonic()


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------

_genai_client = None
_genai_init_attempted = False


def _get_genai_client():
    """Lazy-init the google.genai Client singleton.

    Retries if a previous attempt failed (e.g., env var not yet loaded).
    """
    global _genai_client, _genai_init_attempted
    if _genai_client is not None:
        return _genai_client
    # Ensure dotenv is loaded (config.py does load_dotenv on import)
    try:
        import trading.config  # noqa: F401 — side-effect: loads .env
    except Exception:
        pass
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        if not _genai_init_attempted:
            _genai_init_attempted = True
            log.warning("GEMINI_API_KEY not set — LLM features disabled")
        return None
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
    from google import genai
    _genai_client = genai.Client(api_key=api_key)
    return _genai_client


def _call_gemini(system: str, prompt: str, model: str = MODEL_FLASH,
                 max_tokens: int = 1500,
                 thinking_budget: int | None = None) -> str | None:
    """Call Gemini API via google.genai SDK. Returns response text or None on failure.

    For Gemini 2.5 models, max_output_tokens includes thinking tokens.
    Set thinking_budget to control how many tokens go to reasoning vs output.
    """
    client = _get_genai_client()
    if client is None:
        return None
    _rate_limit()
    try:
        config: dict = {
            "system_instruction": system,
            "max_output_tokens": max_tokens,
        }
        # Constrain thinking budget so more tokens go to visible output
        if thinking_budget is not None:
            config["thinking_config"] = {"thinking_budget": thinking_budget}
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return response.text
    except Exception as e:
        log.warning("Gemini API error (%s): %s", model, e)
        return None


# ---------------------------------------------------------------------------
# Core dispatch
# ---------------------------------------------------------------------------

def ask_llm(system: str, prompt: str, call_type: str = "chat") -> str:
    """Send prompt to Gemini with call-type-specific model and token budget.

    Always returns a string (graceful degradation to a static message).

    Call types: chat, narrative, explain_trade, journal, performance,
    risk_event, signal_summary, annotate, pre_trade, post_trade,
    agent_synthesis, weekly_synthesis.
    """
    profile = CALL_PROFILES.get(call_type, CALL_PROFILES["chat"])
    result = _call_gemini(
        system, prompt,
        model=profile["model"],
        max_tokens=profile["max_tokens"],
        thinking_budget=profile.get("thinking_budget"),
    )
    if result:
        return result
    return "(LLM unavailable — set GEMINI_API_KEY in .env)"


# ---------------------------------------------------------------------------
# System prompt — trading system context
# ---------------------------------------------------------------------------

TRADING_SYSTEM_PROMPT = """You are the AI co-pilot for OT-CORP, an autonomous crypto & commodities trading system.

You have FULL reasoning powers. Think deeply, analyze multi-step cause-and-effect chains, and provide expert-level trading insights. You are not a simple Q&A bot — you are a senior quantitative trading analyst with complete access to the system's live state.

Your capabilities:
- **Execute ANY trading instruction**: buy, sell, open, close, reduce positions via natural language
- **Understand market names**: BTC, bitcoin, ETH, ether, SOL, solana — all resolve correctly to exchange symbols (BTC/USD, etc.)
- **Trade logic mastery**: understand opening/closing, long/short, leverage, margin, stop-losses, take-profits
- Deep multi-step reasoning about market dynamics, portfolio risk, and strategy interactions
- Causal analysis: trace why a trade happened through signal → confluence → risk checks → execution
- Cross-referencing: connect P&L patterns to strategy behavior, market regimes, and agent actions
- Proactive risk identification: spot concerning patterns before they become problems
- Counterfactual analysis: explain what would happen under different scenarios

Your role:
- Explain trades, signals, and system decisions with deep analytical reasoning
- Analyze P&L, risk events, and strategy performance — identify root causes, not just symptoms
- Reason about portfolio-level interactions between positions and strategies
- Proactively flag risks, anomalies, or opportunities you notice in the data
- Answer operator questions with the depth and nuance of an expert trader

Style guidelines:
- Be direct and specific — cite numbers, strategies, timestamps
- Use markdown formatting (bold, tables, bullet points)
- When explaining a trade: cover the signal source, confluence score, leverage, risk checks passed, sizing logic, AND your assessment of the trade quality
- When analyzing P&L: identify contributing strategies, market conditions, what worked/didn't, AND the deeper patterns
- Provide your own analytical opinion when relevant — you're an expert, not just a data formatter
- Never fabricate data — if information is missing, say so
- Structure long responses with headers for readability

System architecture:
- 23 strategies across crypto, equities, and commodities
- Confluence-based signal aggregation (multiple strategies must agree)
- ATR-based dynamic stop losses adjusted by leverage
- 4 leverage profiles (conservative/moderate/aggressive/greedy), currently on aggressive
- Risk checks: position size, sector exposure, correlation groups, cash reserve, drawdown, leverage cap (5x)
- Autonomous learning agents (Performance, Risk, Regime, Research, Learning) that recommend parameter changes
- Circuit breaker (5 consecutive losses = 48h cooldown)
- TWAP execution for large orders, limit orders with market fallback
"""


# ---------------------------------------------------------------------------
# Existing high-level functions
# ---------------------------------------------------------------------------

def explain_trade(trade: dict, context: dict | None = None) -> str:
    """Explain why a trade was made, in natural language."""
    prompt = f"Explain this trade decision:\n\n{json.dumps(trade, indent=2, default=str)}"
    if context:
        prompt += f"\n\nAdditional context:\n{json.dumps(context, indent=2, default=str)}"
    prompt += "\n\nExplain: What triggered this trade? What was the reasoning? Was it a good decision given the data?"
    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="explain_trade")


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

    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="journal")


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

    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="performance")


def interpret_risk_event(event: dict, positions: list[dict]) -> str:
    """Explain a risk block or margin event."""
    prompt = f"""Explain this risk event to the operator:

Event: {json.dumps(event, indent=2, default=str)}

Current positions: {json.dumps(positions[:10], indent=2, default=str)}

Explain what triggered the risk block, whether it was appropriate, and what the operator should consider."""

    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="risk_event")


def summarize_signals(signals: list[dict]) -> str:
    """Quick summary of signal batch."""
    prompt = f"Summarize these trading signals in 2-3 sentences:\n{json.dumps(signals[:20], default=str)}"
    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="signal_summary")


def annotate_action(action: dict) -> str:
    """Add a one-sentence annotation to a system action."""
    prompt = f"In one sentence, explain what this system action means for the portfolio:\n{json.dumps(action, default=str)}"
    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="annotate")


def chat_with_context(message: str, context: dict) -> str:
    """Answer an operator question with full trading system context."""
    prompt = f"""The operator asks: "{message}"

Current system state:
{json.dumps(context, indent=2, default=str)}

Answer the operator's question using the system data above. Be specific and cite actual numbers from the data."""

    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="chat")


# ---------------------------------------------------------------------------
# New LLM features
# ---------------------------------------------------------------------------

def generate_entry_reasoning(signal: dict, portfolio_context: dict,
                              regime: dict, lessons: list[str]) -> str:
    """Generate rich entry reasoning before a trade executes.

    NOT a go/no-go decision — the rules already decided. This generates
    the narrative explaining WHY for the journal and dashboard.
    """
    prompt = f"""A trade is about to execute. Generate a rich entry reasoning narrative.

Signal: {json.dumps(signal, default=str)}
Portfolio context: {json.dumps(portfolio_context, default=str)}
Market regime: {json.dumps(regime, default=str)}
Recent lessons: {json.dumps(lessons[:5], default=str)}

Write 3-5 sentences covering:
1. Why this trade makes sense given the signals
2. How it fits the current market regime
3. Key risks to watch
4. How it relates to any recent lessons learned

Be specific with numbers. This is documentation, not a decision."""
    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="pre_trade")


def generate_post_trade_review(trade: dict, entry_reasoning: str,
                                pnl: float, market_conditions: dict) -> str:
    """Analyze a closed trade: was entry/exit optimal? What can be learned?"""
    prompt = f"""A trade just closed. Analyze the result.

Trade: {json.dumps(trade, default=str)}
Original entry reasoning: {entry_reasoning[:500] if entry_reasoning else 'N/A'}
P&L: ${pnl:+.2f}
Market conditions at exit: {json.dumps(market_conditions, default=str)}

Provide:
1. Was the entry timing good? (given the price action since entry)
2. Was the exit optimal or premature/late?
3. What specific lesson should the system learn from this trade?
4. Grade this trade A/B/C/D/F

Keep it under 200 words."""
    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="post_trade")


def synthesize_position_review(structured_assessment: dict,
                                analysis_parts: list[str]) -> str:
    """Take structured position review data and produce a human-readable synthesis.

    Hybrid approach: rules produce the structured assessment, Gemini synthesizes
    a richer narrative from it.
    """
    prompt = f"""You are reviewing an open trading position. The system's rule-based analysis produced this assessment:

Structured data: {json.dumps(structured_assessment, default=str)}
Rule-based analysis:
{chr(10).join(analysis_parts)}

Synthesize this into a 4-6 sentence analysis that:
1. Summarizes the position's status in plain English
2. Explains the recommended action and why
3. Identifies the biggest risk right now
4. Suggests what to watch for next

Be direct and analytical. Use the exact numbers provided."""
    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="agent_synthesis")


def explain_risk_block(signal: dict, reason: str, portfolio_state: dict) -> str:
    """Explain why a trade was blocked by risk checks, in dashboard-friendly language."""
    prompt = f"""The risk manager blocked a trade. Explain to the operator what happened and why.

Blocked trade: {json.dumps(signal, default=str)}
Block reason: {reason}
Portfolio state: {json.dumps(portfolio_state, default=str)}

In 2-3 sentences:
1. What was blocked and why (in plain English, not code)
2. Whether this was the right call
3. What would need to change for this trade to be allowed"""
    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="risk_event")


def generate_weekly_synthesis(journal_entries: list[dict], agent_lessons: list[str],
                               strategy_performance: list[dict],
                               pnl_history: list[dict]) -> str:
    """Deep weekly analysis combining journal entries, agent lessons, and strategy performance."""
    prompt = f"""Generate a comprehensive weekly trading review.

Journal entries this week: {json.dumps(journal_entries[:20], default=str)}
Lessons from autonomous agents: {json.dumps(agent_lessons[:15], default=str)}
Strategy performance: {json.dumps(strategy_performance[:20], default=str)}
Daily P&L: {json.dumps(pnl_history[:7], default=str)}

Produce a structured weekly review covering:
1. **Performance Summary**: Total P&L, win rate, best/worst days
2. **Strategy Analysis**: Which strategies drove returns, which dragged
3. **Risk Events**: Notable blocks, circuit breakers, drawdowns
4. **Lessons Learned**: Patterns from agent recommendations and trade outcomes
5. **Next Week Outlook**: Based on current positions and regime
6. **Action Items**: Specific changes to consider

Use markdown formatting. Be analytical and specific."""
    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="weekly_synthesis")


def analyze_news_impact(headlines: list[dict], positions: list[dict],
                        regime: str, traded_assets: list[str]) -> str:
    """Use Gemini to interpret market news and assess impact on specific assets.

    Returns structured JSON-like analysis with per-asset sentiment,
    key events, actionable signals, and risk alerts.
    """
    # Trim headlines to titles + source for token efficiency
    hl_summary = [{"title": h.get("title", ""), "source": h.get("source", ""),
                    "category": h.get("category", "")}
                   for h in headlines[:20]]

    position_symbols = [p.get("symbol", p.get("coin", "")) for p in positions[:15]]

    prompt = f"""You are a senior market analyst. Analyze these recent news headlines
and determine their impact on the assets we actively trade.

**Headlines:**
{json.dumps(hl_summary, indent=1)}

**Our open positions:** {json.dumps(position_symbols)}
**Current market regime:** {regime}
**All assets we trade:** {json.dumps(traded_assets[:30])}

Provide your analysis in this exact format:

## Key Events
List the 3 most market-moving headlines and explain WHY they matter.

## Asset Impacts
For each asset affected by this news, provide:
- Asset name
- Impact score: -1.0 (very bearish) to +1.0 (very bullish)
- One-sentence explanation

Only list assets where news has a CLEAR directional impact (skip neutral).

## Trading Signals
Based on this news, recommend specific actions:
- BUY [asset] — [reason] (strength: 0.0-1.0)
- SELL [asset] — [reason] (strength: 0.0-1.0)
Only include high-conviction signals (strength >= 0.5).

## Risk Alerts
Flag any headlines suggesting we should reduce exposure or be cautious.
Include specific assets or the portfolio as a whole.

Be direct. Cite specific headlines. Don't hedge excessively."""

    return ask_llm(TRADING_SYSTEM_PROMPT, prompt, call_type="news_analysis")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_llm_availability() -> dict:
    """Check if Gemini is configured and reachable."""
    status = {
        "gemini": {"configured": bool(os.getenv("GEMINI_API_KEY")), "reachable": False},
    }

    if status["gemini"]["configured"]:
        result = _call_gemini("Say OK", "ping")
        status["gemini"]["reachable"] = result is not None

    status["any_available"] = status["gemini"]["reachable"]
    return status
