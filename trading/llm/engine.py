"""LLM engine for OT-CORP trading system — two-tier cost routing.

Claude Reasoning Tier (ANTHROPIC_API_KEY): chat, chat_full, agent_synthesis,
    weekly_synthesis, journal, explain_trade, performance — ~$12-18/month.

Groq Free Tier (GROQ_API_KEY): all other automated calls — $0/month.

Gemini Flash (GEMINI_API_KEY): emergency fallback for either tier.

Fallback chains:
    Reasoning tier:  Claude → Groq → Gemini
    Groq/free tier:  Groq → Gemini → Claude

Usage:
    from trading.llm.engine import ask_llm, explain_trade, generate_journal

Environment variables:
    ANTHROPIC_API_KEY — Claude Sonnet (reasoning tier, ~$12-18/mo)
    GROQ_API_KEY      — Groq free tier ($0/mo, 14,400 req/day)
    GEMINI_API_KEY    — Gemini Flash fallback ($0/mo)
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

# Groq models (free tier)
GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# Gemini models (fallback)
GEMINI_MODEL_FLASH = "gemini-2.5-flash"

# Claude model (reasoning tier)
CLAUDE_MODEL = "claude-sonnet-4-6"

# Calls routed to Claude first (high-stakes reasoning, ~$12-18/mo total)
CLAUDE_TIER: frozenset[str] = frozenset({
    "chat", "chat_full",
    "agent_synthesis", "weekly_synthesis",
    "journal", "explain_trade", "performance",
})

# Calls routed to Groq first (summarization/formatting, $0/mo)
GROQ_TIER: frozenset[str] = frozenset({
    "signal_summary", "annotate", "narrative",
    "pre_trade", "post_trade", "news_analysis", "risk_event",
})

# Per-call-type token budgets — tightened on high-frequency automated calls
CALL_PROFILES: dict[str, dict] = {
    "chat":             {"max_tokens": 4096},
    "chat_full":        {"max_tokens": 6144},
    "narrative":        {"max_tokens": 512},    # 1024 → 512
    "explain_trade":    {"max_tokens": 1024},   # 2048 → 1024
    "journal":          {"max_tokens": 1536},   # 3072 → 1536
    "performance":      {"max_tokens": 1024},   # 2048 → 1024
    "risk_event":       {"max_tokens": 512},    # 1024 → 512
    "signal_summary":   {"max_tokens": 256},    # 512 → 256
    "annotate":         {"max_tokens": 128},    # 256 → 128
    "pre_trade":        {"max_tokens": 512},    # 1024 → 512
    "post_trade":       {"max_tokens": 768},    # 1536 → 768
    "agent_synthesis":  {"max_tokens": 1500},   # 2048 → 1500
    "weekly_synthesis": {"max_tokens": 2048},   # 3072 → 2048
    "news_analysis":    {"max_tokens": 1024},   # 2560 → 1024
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
# Provider: Groq (primary) — OpenAI-compatible API
# ---------------------------------------------------------------------------

_groq_client = None
_groq_init_attempted = False


def _get_groq_client():
    """Lazy-init the Groq client (uses OpenAI SDK with custom base_url)."""
    global _groq_client, _groq_init_attempted
    if _groq_client is not None:
        return _groq_client

    # Ensure dotenv is loaded
    try:
        import trading.config  # noqa: F401 — side-effect: loads .env
    except Exception:
        pass

    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        if not _groq_init_attempted:
            _groq_init_attempted = True
            log.info("GROQ_API_KEY not set — will try Gemini fallback")
        return None

    try:
        from openai import OpenAI
        _groq_client = OpenAI(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1",
        )
        log.info("Groq client initialized (model: %s)", GROQ_MODEL)
        return _groq_client
    except ImportError:
        log.warning("openai package not installed — pip install openai")
        return None
    except Exception as e:
        log.warning("Failed to initialize Groq client: %s", e)
        return None


def _call_groq(system: str, prompt: str, max_tokens: int = 1500) -> str | None:
    """Call Groq API via OpenAI-compatible SDK. Returns response text or None."""
    client = _get_groq_client()
    if client is None:
        return None
    _rate_limit()
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.7,
        )
        return response.choices[0].message.content
    except Exception as e:
        log.warning("Groq API error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Provider: Gemini (fallback)
# ---------------------------------------------------------------------------

_genai_client = None
_genai_init_attempted = False


def _get_genai_client():
    """Lazy-init the google.genai Client singleton (fallback only)."""
    global _genai_client, _genai_init_attempted
    if _genai_client is not None:
        return _genai_client
    try:
        import trading.config  # noqa: F401
    except Exception:
        pass
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        if not _genai_init_attempted:
            _genai_init_attempted = True
            log.warning("GEMINI_API_KEY not set — LLM fallback unavailable")
        return None
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
    from google import genai
    _genai_client = genai.Client(api_key=api_key)
    return _genai_client


def _call_gemini(system: str, prompt: str, max_tokens: int = 1500) -> str | None:
    """Call Gemini API via google.genai SDK. Fallback provider."""
    client = _get_genai_client()
    if client is None:
        return None
    _rate_limit()
    try:
        config: dict = {
            "system_instruction": system,
            "max_output_tokens": max_tokens,
        }
        response = client.models.generate_content(
            model=GEMINI_MODEL_FLASH,
            contents=prompt,
            config=config,
        )
        return response.text
    except Exception as e:
        log.warning("Gemini API error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Provider: Claude (reasoning tier)
# ---------------------------------------------------------------------------

_claude_client = None
_claude_init_attempted = False


def _get_claude_client():
    """Lazy-init the Anthropic client singleton."""
    global _claude_client, _claude_init_attempted
    if _claude_client is not None:
        return _claude_client
    try:
        import trading.config  # noqa: F401
    except Exception:
        pass
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        if not _claude_init_attempted:
            _claude_init_attempted = True
            log.info("ANTHROPIC_API_KEY not set — reasoning tier will use Groq")
        return None
    try:
        import anthropic
        _claude_client = anthropic.Anthropic(api_key=api_key)
        log.info("Claude client initialized (model: %s)", CLAUDE_MODEL)
        return _claude_client
    except ImportError:
        log.warning("anthropic package not installed — pip install anthropic")
        return None
    except Exception as e:
        log.warning("Failed to initialize Claude client: %s", e)
        return None


def _call_claude(system: str, prompt: str, max_tokens: int = 4096) -> str | None:
    """Call Claude Sonnet via Anthropic SDK. Returns response text or None."""
    client = _get_claude_client()
    if client is None:
        return None
    _rate_limit()
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as e:
        log.warning("Claude API error: %s", e)
        return None


def ask_llm(system: str, prompt: str, call_type: str = "chat") -> str:
    """Send prompt to LLM with call-type-specific token budget and tier routing.

    Reasoning tier (Claude → Groq → Gemini):
        chat, chat_full, agent_synthesis, weekly_synthesis,
        journal, explain_trade, performance

    Free tier (Groq → Gemini → Claude emergency):
        signal_summary, annotate, narrative, pre_trade, post_trade,
        news_analysis, risk_event

    Always returns a string (graceful degradation to a static message).
    """
    max_tokens = CALL_PROFILES.get(call_type, CALL_PROFILES["chat"])["max_tokens"]

    if call_type in CLAUDE_TIER:
        # Reasoning tier: Claude first, free providers as fallback
        result = _call_claude(system, prompt, max_tokens=max_tokens)
        if result:
            log.debug("ask_llm [%s] → claude", call_type)
            return result
        result = _call_groq(system, prompt, max_tokens=max_tokens)
        if result:
            log.debug("ask_llm [%s] → groq (claude unavailable)", call_type)
            return result
        result = _call_gemini(system, prompt, max_tokens=max_tokens)
        if result:
            log.debug("ask_llm [%s] → gemini (claude+groq unavailable)", call_type)
            return result
    else:
        # Free tier: Groq first, Claude only as emergency fallback
        result = _call_groq(system, prompt, max_tokens=max_tokens)
        if result:
            log.debug("ask_llm [%s] → groq", call_type)
            return result
        result = _call_gemini(system, prompt, max_tokens=max_tokens)
        if result:
            log.debug("ask_llm [%s] → gemini (groq unavailable)", call_type)
            return result
        result = _call_claude(system, prompt, max_tokens=max_tokens)
        if result:
            log.debug("ask_llm [%s] → claude (groq+gemini unavailable)", call_type)
            return result

    return "(LLM unavailable — add ANTHROPIC_API_KEY and GROQ_API_KEY to Render)"


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
    live_ctx = _build_live_context()
    trade_data = json.dumps(trade, indent=2, default=str)
    ctx_block = f"\nAdditional context:\n{json.dumps(context, indent=2, default=str)}" if context else ""

    # Extract key metrics for richer prompt
    strategy = trade.get("strategy", "unknown")
    symbol = trade.get("symbol", "unknown")
    side = trade.get("side", "unknown")
    pnl = trade.get("pnl")
    pnl_str = f"P&L: {pnl:+.4f}" if pnl is not None else "P&L: open"
    data_field = trade.get("data") or {}
    if isinstance(data_field, str):
        try:
            data_field = json.loads(data_field)
        except Exception:
            data_field = {}
    strength = data_field.get("signal_strength") or data_field.get("strength")
    regime_at_entry = data_field.get("regime", "unknown")

    prompt = f"""Explain this trade decision in detail.

Trade: {side.upper()} {symbol} via {strategy}
{pnl_str}
Signal strength at entry: {strength if strength is not None else "unknown"}
Regime at entry: {regime_at_entry}

Full trade record:
{trade_data}
{ctx_block}

Live system state at review:
{live_ctx}

Explain:
1. What triggered this trade and why the strategy fired
2. Whether the signal strength and regime context supported the decision
3. The outcome assessment (if closed) — was the sizing and timing appropriate?
4. One specific lesson or observation from this trade"""
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


def _build_live_context() -> str:
    """Assemble live system state into a context block for LLM prompts."""
    try:
        from trading.db.store import get_setting, get_db
        regime = get_setting("current_regime", "neutral") or "neutral"
        regime_score = get_setting("current_regime_score", "0.0") or "0.0"
        interval = get_setting("next_cycle_interval_hours", "4.0") or "4.0"
        risk_stage = get_setting("risk_stage", "0") or "0"
        cycle_reason = get_setting("next_cycle_reason", "") or ""

        # Pull portfolio snapshot
        with get_db() as conn:
            row = conn.execute(
                "SELECT portfolio_value, daily_pnl, peak_value FROM daily_pnl ORDER BY date DESC LIMIT 1"
            ).fetchone()
            open_count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status='open'"
            ).fetchone()[0]
            recent_wins = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status='closed' AND pnl > 0 ORDER BY timestamp DESC LIMIT 20"
            ).fetchone()[0]
            recent_total = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status='closed' ORDER BY timestamp DESC LIMIT 20"
            ).fetchone()[0]

        portfolio_val = row["portfolio_value"] if row else None
        daily_pnl = row["daily_pnl"] if row else None
        peak = row["peak_value"] if row else None
        drawdown = round((portfolio_val - peak) / peak * 100, 2) if (portfolio_val and peak and peak > 0) else 0.0
        win_rate = round(recent_wins / recent_total * 100, 1) if recent_total else 0.0

        risk_labels = {"0": "Normal", "1": "Tighten", "2": "Conservative", "3": "Halt"}

        lines = [
            f"Regime: {regime} (score {float(regime_score):+.3f})",
            f"Risk stage: {risk_labels.get(risk_stage, risk_stage)}",
            f"Cycle frequency: every {interval}h ({cycle_reason})",
            f"Open positions: {open_count}",
        ]
        if portfolio_val:
            lines.append(f"Portfolio value: ${portfolio_val:,.2f}")
        if daily_pnl is not None:
            lines.append(f"Today's P&L: ${daily_pnl:+,.2f}")
        if drawdown:
            lines.append(f"Drawdown from peak: {drawdown:+.2f}%")
        if recent_total:
            lines.append(f"Recent win rate (last {recent_total}): {win_rate}%")
        return "\n".join(lines)
    except Exception:
        return "(live context unavailable)"


def chat_with_context(message: str, context: dict) -> str:
    """Answer an operator question with full trading system context."""
    live_ctx = _build_live_context()
    prompt = f"""The operator asks: "{message}"

Live system state:
{live_ctx}

Additional context:
{json.dumps(context, indent=2, default=str)}

Answer the operator's question using the data above. Be specific and cite actual numbers."""

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

    Hybrid approach: rules produce the structured assessment, LLM synthesizes
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
    """Interpret market news and assess impact on specific assets.

    Returns structured analysis with per-asset sentiment,
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
    """Check which LLM providers are configured and reachable."""
    try:
        import trading.config  # noqa: F401
    except Exception:
        pass

    status = {
        "claude": {"configured": bool(os.getenv("ANTHROPIC_API_KEY")), "reachable": False},
        "groq":   {"configured": bool(os.getenv("GROQ_API_KEY")),      "reachable": False},
        "gemini": {"configured": bool(os.getenv("GEMINI_API_KEY")),     "reachable": False},
    }

    if status["claude"]["configured"]:
        result = _call_claude("Say OK", "ping", max_tokens=10)
        status["claude"]["reachable"] = result is not None

    if status["groq"]["configured"]:
        result = _call_groq("Say OK", "ping", max_tokens=10)
        status["groq"]["reachable"] = result is not None

    if status["gemini"]["configured"]:
        result = _call_gemini("Say OK", "ping", max_tokens=10)
        status["gemini"]["reachable"] = result is not None

    reachable = {k for k, v in status.items() if v["reachable"]}
    status["any_available"] = bool(reachable)
    status["active_provider"] = (
        "+".join(sorted(reachable)) if reachable else "none"
    )
    status["reasoning_tier"] = "claude" if status["claude"]["reachable"] else (
        "groq" if status["groq"]["reachable"] else "none"
    )
    status["free_tier"] = "groq" if status["groq"]["reachable"] else (
        "gemini" if status["gemini"]["reachable"] else "none"
    )
    return status
