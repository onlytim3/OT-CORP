"""LLM engine for OT-CORP trading system — Claude-primary design.

Claude Reasoning Tier (ANTHROPIC_API_KEY): all user-visible outputs —
    chat, chat_full, agent_synthesis, weekly_synthesis, journal,
    explain_trade, performance, news_analysis, risk_event,
    narrative, pre_trade, post_trade — ~$12-18/month.
    If Claude is unavailable, returns a clear operator-facing message.
    Never silently falls back to a weaker model.

Groq Background Tier (GROQ_API_KEY): internal logging only —
    signal_summary, annotate — $0/month.
    Falls back to Claude if Groq is down.

Fallback chains:
    Claude tier:      Claude → operator-visible error (no silent fallback)
    Background tier:  Groq → Claude → static error string

Usage:
    from trading.llm.engine import ask_llm, explain_trade, generate_journal

Environment variables:
    ANTHROPIC_API_KEY — Claude Sonnet (all reasoning, ~$12-18/mo)
    GROQ_API_KEY      — Groq free tier (background tasks only, $0/mo)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
import unicodedata
from datetime import datetime, timezone

log = logging.getLogger(__name__)


_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]+")

def _ascii_safe(text: str) -> str:
    """Strip all non-ASCII characters from text — guaranteed to never raise.

    Uses regex substitution which is immune to codec edge-cases (lone surrogates,
    unusual normalisation forms, codec bugs). Previous encode/decode approach
    could still raise UnicodeEncodeError for certain character sequences.
    """
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return ""
    try:
        return _NON_ASCII_RE.sub("", text)
    except Exception:
        # Last resort: char-by-char filter
        return "".join(c for c in text if ord(c) < 128)


# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------

# Groq models (background tasks only) — llama-3.3-70b-versatile is confirmed free-tier
GROQ_MODEL = "llama-3.3-70b-versatile"

# Claude model (primary reasoning engine — all user-visible outputs)
CLAUDE_MODEL = "claude-sonnet-4-6"

# High-value reasoning — Claude only, no silent fallback to weaker models
# These are operator-visible analysis outputs where quality matters most
CLAUDE_TIER: frozenset[str] = frozenset({
    "chat", "chat_full",
    "agent_synthesis", "weekly_synthesis",
    "journal", "explain_trade", "performance",
    "news_analysis",
})

# Groq first, Claude fallback — routine per-cycle outputs, high call volume
# Using Groq here preserves Claude credits for the high-value tier above
GROQ_TIER: frozenset[str] = frozenset({
    "signal_summary", "annotate",
    "narrative",    # action card descriptions — many per cycle, short output
    "pre_trade",    # entry reasoning logged to DB — Groq quality sufficient
    "post_trade",   # post-trade review — Groq quality sufficient
    "risk_event",   # risk event narration — brief, factual
})

# Per-call-type token budgets
CALL_PROFILES: dict[str, dict] = {
    "chat":             {"max_tokens": 4096},
    "chat_full":        {"max_tokens": 4096},   # full operator chat
    "narrative":        {"max_tokens": 1024},   # action card narratives
    "explain_trade":    {"max_tokens": 2048},   # trade explanations
    "journal":          {"max_tokens": 4096},   # daily journal — needs full space
    "performance":      {"max_tokens": 2048},   # performance analysis
    "risk_event":       {"max_tokens": 1536},   # risk event narration
    "signal_summary":   {"max_tokens": 512},    # signal batch summary
    "annotate":         {"max_tokens": 256},    # one-liner annotations
    "pre_trade":        {"max_tokens": 1024},   # entry reasoning
    "post_trade":       {"max_tokens": 1536},   # post-trade review
    "agent_synthesis":  {"max_tokens": 2048},   # autonomous agent reasoning
    "weekly_synthesis": {"max_tokens": 4096},   # weekly review — comprehensive
    "news_analysis":    {"max_tokens": 3072},   # news impact analysis — JSON output + reasoning
}

assert CALL_PROFILES.keys() == CLAUDE_TIER | GROQ_TIER, (
    f"CALL_PROFILES keys not covered by tiers: "
    f"uncovered={set(CALL_PROFILES.keys()) - CLAUDE_TIER - GROQ_TIER}"
)


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
# Provider circuit breakers — skip a provider that keeps failing
# ---------------------------------------------------------------------------

_provider_failures: dict[str, int] = {}        # provider → consecutive failure count
_provider_disabled_until: dict[str, float] = {}  # provider → monotonic time when re-enabled
_provider_last_error: dict[str, str] = {}        # provider → last error message (for diagnostics)
_CIRCUIT_OPEN_AFTER = 5    # sustained failures before disabling (each failure = 3 attempts already tried)
_CIRCUIT_COOLDOWN = 30.0   # 30s cooldown — short because retries already absorbed transient errors


def _provider_ok(name: str) -> bool:
    """Return True if provider is not circuit-broken."""
    disabled_until = _provider_disabled_until.get(name, 0.0)
    if time.monotonic() < disabled_until:
        return False
    return True


def reset_provider_circuit_breaker(name: str) -> None:
    """Manually reset a provider's circuit breaker — called by operator or debug endpoint."""
    _provider_failures.pop(name, None)
    _provider_disabled_until.pop(name, None)
    _provider_last_error.pop(name, None)
    log.info("LLM circuit breaker RESET for '%s'", name)


def _record_failure(name: str, error: str = "") -> None:
    _provider_failures[name] = _provider_failures.get(name, 0) + 1
    if error:
        _provider_last_error[name] = error[:300]
    if _provider_failures[name] >= _CIRCUIT_OPEN_AFTER:
        _provider_disabled_until[name] = time.monotonic() + _CIRCUIT_COOLDOWN
        log.warning("LLM circuit breaker OPEN for '%s' — disabled for %ds", name, int(_CIRCUIT_COOLDOWN))


def _record_success(name: str) -> None:
    _provider_failures[name] = 0
    _provider_disabled_until.pop(name, None)


def get_provider_health() -> dict:
    """Return health status of each LLM provider (for diagnostics)."""
    now = time.monotonic()
    result = {}
    for name in ("groq", "claude"):
        disabled_until = _provider_disabled_until.get(name, 0.0)
        failures = _provider_failures.get(name, 0)
        last_error = _provider_last_error.get(name, "")
        if now < disabled_until:
            result[name] = {
                "status": "circuit_open",
                "failures": failures,
                "retry_in_seconds": int(disabled_until - now),
                "last_error": last_error,
            }
        else:
            result[name] = {
                "status": "ok" if failures == 0 else "degraded",
                "failures": failures,
                "last_error": last_error,
            }
    return result



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
            log.info("GROQ_API_KEY not set — background tasks will fall back to Claude")
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
    if not _provider_ok("groq"):
        return None
    client = _get_groq_client()
    if client is None:
        return None
    _rate_limit()
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": _ascii_safe(system)},
                {"role": "user", "content": _ascii_safe(prompt)},
            ],
            max_tokens=max_tokens,
            temperature=0.7,
        )
        _record_success("groq")
        return response.choices[0].message.content
    except Exception as e:
        err = str(e)
        if "429" in err or "rate_limit" in err.lower() or "quota" in err.lower():
            log.warning("Groq rate limited — backing off 5s before fallback")
            time.sleep(5)
        elif "403" in err or "401" in err or "forbidden" in err.lower() or "unauthorized" in err.lower():
            log.warning("Groq auth/access error [%s]: %s — circuit breaker triggered", type(e).__name__, err[:200])
        else:
            log.warning("Groq API error [%s]: %s", type(e).__name__, err[:200])
        _record_failure("groq", err)
        return None




# ---------------------------------------------------------------------------
# Provider: Claude (reasoning tier)
# ---------------------------------------------------------------------------

_claude_client = None
_claude_init_attempted = False


def _bytes_safe(text: str) -> str:
    """Final-resort bytes-level ASCII safety — encode to ASCII bytes then decode.

    More aggressive than _ascii_safe() regex: catches any characters that the
    regex misses due to edge cases in Python's unicode database. Guarantees
    the result survives .encode('ascii') without raising UnicodeEncodeError.
    """
    return text.encode("ascii", errors="ignore").decode("ascii")


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
            log.warning("ANTHROPIC_API_KEY not set — Claude-tier calls will return unavailable message")
        return None
    # Validate key is ASCII-clean — HTTP/1.1 headers must be ASCII.
    # Cyrillic characters (e.g. В ≈ B on a Russian keyboard) silently corrupt keys.
    try:
        api_key.encode("ascii")
    except UnicodeEncodeError as e:
        if not _claude_init_attempted:
            _claude_init_attempted = True
            log.error(
                "ANTHROPIC_API_KEY contains a non-ASCII character at position %d "
                "(likely a Cyrillic look-alike typed on a Russian keyboard). "
                "Re-enter the key in Render env vars using only ASCII characters.",
                e.start,
            )
        return None
    try:
        import anthropic
        import httpx
        _claude_client = anthropic.Anthropic(
            api_key=api_key,
            timeout=httpx.Timeout(45.0, connect=10.0),  # 45s total, 10s connect
        )
        log.info("Claude client initialized (model: %s, timeout=45s)", CLAUDE_MODEL)
        return _claude_client
    except ImportError:
        log.warning("anthropic package not installed — pip install anthropic")
        return None
    except Exception as e:
        log.warning("Failed to initialize Claude client: %s", e)
        return None


def _call_claude(system: str, prompt: str, max_tokens: int = 4096, cache_system: bool = False) -> str | None:
    """Call Claude Sonnet via Anthropic SDK. Returns response text or None.

    Retries up to 3 times with exponential backoff before counting a failure.
    This means a single network blip never trips the circuit breaker — only
    sustained, repeated failures do.

    cache_system=True enables prompt caching on the system prompt — use for
    repeated calls with the same large system prompt (saves ~90% input token cost).
    """
    if not _provider_ok("claude"):
        return None
    client = _get_claude_client()
    if client is None:
        return None
    max_tokens = min(max_tokens, 8096)

    # Build system param once (outside retry loop).
    # Two-pass sanitisation: regex strips non-ASCII code points, then bytes
    # encoding guarantees the result is truly ASCII-clean at the codec level.
    def _clean(t: str) -> str:
        return _bytes_safe(_ascii_safe(t))

    if cache_system:
        system_param = [{"type": "text", "text": _clean(system), "cache_control": {"type": "ephemeral"}}]
    else:
        system_param = _clean(system)
    user_content = _clean(prompt)

    last_err: str = ""
    unicode_errors_only = True  # track whether ALL failures were encoding issues
    for attempt in range(3):
        if attempt > 0:
            time.sleep(2 ** attempt)  # 2s then 4s backoff
        _rate_limit()
        try:
            msg = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=system_param,
                messages=[{"role": "user", "content": user_content}],
            )
            _record_success("claude")
            return msg.content[0].text
        except UnicodeEncodeError as e:
            # Non-ASCII in the request (content or API key) — NOT an API availability
            # issue, so do NOT count against the circuit breaker.
            err_msg = str(e)
            log.warning("Claude UnicodeEncodeError (attempt %d/3): %s", attempt + 1, err_msg)
            _provider_last_error["claude"] = f"UnicodeEncodeError: {err_msg}"
            # Re-strip content (fixes content issues; can't fix a bad API key)
            if isinstance(system_param, list):
                for item in system_param:
                    if isinstance(item, dict) and "text" in item:
                        item["text"] = _bytes_safe(item["text"])
            elif isinstance(system_param, str):
                system_param = _bytes_safe(system_param)
            user_content = _bytes_safe(user_content)
        except Exception as e:
            unicode_errors_only = False
            err = _ascii_safe(str(e))
            if "402" in err or "credit" in err.lower() or "billing" in err.lower() or "payment" in err.lower() or "insufficient" in err.lower():
                log.error("Claude BILLING ERROR — top up at console.anthropic.com: %s", err[:200])
                _provider_last_error["claude"] = f"BILLING: {err[:200]}"
                return None  # Billing: don't retry, don't trip circuit breaker
            last_err = f"{type(e).__name__}: {err}"
            log.warning("Claude API attempt %d/3 failed [%s]: %s", attempt + 1, type(e).__name__, err[:200])

    # If ALL failures were Unicode encoding errors, it's a content/key issue — not
    # an API availability problem. Don't trip the circuit breaker.
    if unicode_errors_only:
        log.error(
            "Claude call failed with UnicodeEncodeError on all 3 attempts. "
            "Check ANTHROPIC_API_KEY in Render env vars for non-ASCII characters "
            "(Cyrillic В looks identical to Latin B but breaks ASCII header encoding)."
        )
        return None

    # Real API failures — count against circuit breaker
    log.warning("Claude API error after 3 attempts: %s", last_err[:300])
    _record_failure("claude", last_err)
    return None


_CLAUDE_UNAVAILABLE = (
    "LLM unavailable — Claude is temporarily offline. "
    "Check /api/debug/llm for last_error details, or POST /api/debug/llm/reset to clear the circuit breaker."
)


def ask_llm(system: str, prompt: str, call_type: str = "chat", max_tokens: int | None = None) -> str:
    """Send prompt to LLM with call-type-specific token budget and tier routing.

    Claude tier (all user-visible outputs — no silent fallback):
        If Claude is unavailable, returns a clear operator-facing message
        rather than silently substituting a weaker model.

    Groq tier (background/internal only):
        Groq → Claude fallback → static error string.

    Always returns a string.
    """
    system = _ascii_safe(system)
    prompt = _ascii_safe(prompt)

    if max_tokens is None:
        max_tokens = CALL_PROFILES.get(call_type, CALL_PROFILES["chat"])["max_tokens"]

    if call_type in CLAUDE_TIER:
        # Cache system prompt for calls that reuse the large trading context (saves ~90% input cost)
        cache = call_type in {"chat", "chat_full", "news_analysis", "agent_synthesis", "journal", "performance"}
        result = _call_claude(system, prompt, max_tokens=max_tokens, cache_system=cache)
        if result:
            log.debug("ask_llm [%s] → claude", call_type)
            return result
        # Check if it's a billing error — give a clear message instead of the generic unavailable msg
        last_err = _provider_last_error.get("claude", "")
        if "BILLING" in last_err:
            log.debug("ask_llm [%s]: Claude billing error — credit exhausted", call_type)
            return "LLM unavailable — Anthropic API credit exhausted. Please top up your balance at console.anthropic.com."
        # Log at DEBUG when circuit is already open (not WARNING — avoids log spam per cycle)
        log.debug("ask_llm [%s]: Claude unavailable (circuit open or API error)", call_type)
        return _CLAUDE_UNAVAILABLE

    if call_type in GROQ_TIER:
        result = _call_groq(system, prompt, max_tokens=max_tokens)
        if result:
            log.debug("ask_llm [%s] → groq", call_type)
            return result
        result = _call_claude(system, prompt, max_tokens=max_tokens)
        if result:
            log.debug("ask_llm [%s] → claude (groq unavailable)", call_type)
            return result
        log.error("ask_llm [%s]: all providers failed", call_type)
        return "(LLM unavailable — add ANTHROPIC_API_KEY and GROQ_API_KEY to Render)"

    # Unknown call_type — treat as Claude tier (safe default)
    log.warning("ask_llm: unknown call_type '%s', routing to Claude", call_type)
    result = _call_claude(system, prompt, max_tokens=max_tokens)
    if result:
        return result
    return _CLAUDE_UNAVAILABLE


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

        # Inject accumulated system memory (distilled by memory agent, updated daily)
        try:
            raw_memory = get_setting("system_memory")
            if raw_memory:
                memory_data = json.loads(raw_memory)
                memory_content = memory_data.get("content", "")
                memory_date = memory_data.get("generated_at", "")[:10]
                if memory_content:
                    lines.append(
                        f"\n--- Accumulated System Memory (as of {memory_date}) ---\n"
                        + memory_content
                    )
        except Exception:
            pass

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


# Patterns that look like prompt-injection attempts embedded in news headlines.
# If any pattern matches, the headline is dropped before being sent to Claude.
_INJECTION_PATTERNS = [
    r"ignore (previous|above|prior|all) instructions",
    r"system\s*prompt",
    r"you are now",
    r"act as (a|an|the)\b",
    r"\bdisregard\b",
    r"new (role|persona|task|instruction)",
    r"forget (everything|previous|prior)",
    r"<\s*(script|iframe|svg)",        # HTML injection attempts
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def _sanitize_headline(title: str) -> str | None:
    """Return None if title looks like a prompt-injection attempt; else cap length."""
    if _INJECTION_RE.search(title):
        log.warning("news: dropped suspicious headline (possible injection): %.80s", title)
        return None
    return _ascii_safe(title[:300])


def analyze_news_impact(headlines: list[dict], positions: list[dict],
                        regime: str, traded_assets: list[str]) -> dict:
    """Interpret market news and return structured impact assessment.

    Returns dict with keys: key_events, asset_impacts, signals, risk_alerts, model_used.
    Falls back to empty structure on parse failure — never raises.
    """
    hl_summary = []
    for h in headlines[:25]:
        raw_title = h.get("title", "")
        if not raw_title or not raw_title.strip():
            continue
        clean_title = _sanitize_headline(raw_title)
        if clean_title is None:
            continue
        hl_summary.append({
            "title": clean_title,
            "source": h.get("source", ""),
            "category": h.get("category", ""),
            "published": h.get("published", h.get("timestamp", "")),
        })

    # Filter stale headlines (> 4 hours old) before sending to LLM.
    # dateutil is not in requirements so we use a lightweight heuristic:
    # if the published string doesn't contain a known recent year, skip timing check.
    RECENT_YEARS = {"2024", "2025", "2026"}
    fresh_headlines: list[dict] = []
    stale_count = 0
    for h in hl_summary:
        pub = h.get("published", "")
        if not pub:
            fresh_headlines.append(h)  # No timestamp — include with caution
            continue
        # Only attempt time filtering if the published value looks like a full
        # ISO-8601 timestamp (contains 'T' or ':') so we don't misclassify
        # date-only strings or relative labels.
        if ("T" in pub or (":" in pub and any(yr in pub for yr in RECENT_YEARS))):
            try:
                from datetime import timedelta
                # Parse ISO-8601 subset: "2025-04-22T10:30:00Z" or "+00:00"
                ts = pub.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
                if dt >= cutoff:
                    fresh_headlines.append(h)
                else:
                    stale_count += 1
            except Exception:
                fresh_headlines.append(h)
        else:
            fresh_headlines.append(h)

    if not fresh_headlines:
        fresh_headlines = hl_summary  # All stale? Use anyway with a flag

    position_symbols = [p.get("symbol", p.get("coin", "")) for p in positions[:20]]

    system = """You are a quantitative analyst at a crypto hedge fund.
Your job: assess the directional impact of news headlines on specific assets.

Impact score scale (probability of 2%+ move in this direction within 24h):
  +1.0  certain catalyst (ETF approval, major exchange listing, government adoption)
  +0.7  likely positive (favorable regulation, major partnership, protocol upgrade)
  +0.3  weak positive (minor positive sentiment, analyst upgrade)
   0.0  neutral or conflicting signals — do NOT include in output
  -0.3  weak negative (minor concerns, skeptical commentary)
  -0.7  likely negative (regulation crackdown, exchange issues, protocol exploit)
  -1.0  existential threat (insolvency, major hack, total ban)

Rules:
- Only include assets where a headline DIRECTLY mentions them or their protocol/chain
- DO NOT include assets based on general "crypto market" sentiment alone
- A signal requires strength >= 0.5 to be actionable
- Output ONLY valid JSON — no markdown, no prose, no explanation outside the JSON"""

    base_prompt = f"""Analyze these news headlines and return a JSON assessment.

Current market regime: {regime}
Our open positions: {json.dumps(position_symbols)}
All assets we trade: {json.dumps(traded_assets[:40])}
Stale headlines excluded: {stale_count}

Headlines (newest first):
{json.dumps(fresh_headlines, indent=1)}

Return EXACTLY this JSON structure (no other text):
{{
  "key_events": [
    {{"headline": "exact headline text", "impact": "why this matters in 1 sentence", "urgency": "high|medium|low"}}
  ],
  "asset_impacts": {{
    "SYMBOL": {{"score": 0.7, "reason": "1 sentence reason", "headline_count": 2}}
  }},
  "signals": [
    {{"action": "buy|sell", "asset": "SYMBOL", "strength": 0.7, "reason": "1 sentence", "time_horizon": "hours|day|week"}}
  ],
  "risk_alerts": [
    {{"asset": "SYMBOL or portfolio", "alert": "1 sentence risk", "severity": "high|medium|low"}}
  ],
  "headline_count_used": {len(fresh_headlines)},
  "stale_excluded": {stale_count}
}}

Only include signals with strength >= 0.5. Only include key_events that are market-moving (3 max). asset_impacts only for directly-mentioned assets."""

    prompt = base_prompt

    # Try up to 2 times to get valid JSON
    for attempt in range(2):
        try:
            raw = ask_llm(system, prompt, call_type="news_analysis")
            if not raw or "LLM unavailable" in raw:
                break
            # Strip any markdown fences if LLM adds them despite instructions
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```")[1]
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
            cleaned = cleaned.strip()
            result = json.loads(cleaned)
            result["model_used"] = "llm"
            return result
        except (json.JSONDecodeError, Exception) as e:
            log.warning("News analysis JSON parse failed (attempt %d): %s", attempt + 1, e)
            if attempt == 0:
                # Add explicit retry instruction to prompt
                prompt = base_prompt + "\n\nIMPORTANT: Your previous response was not valid JSON. Return ONLY the JSON object, starting with { and ending with }."

    # Fallback structure
    log.warning("analyze_news_impact: returning empty fallback after parse failures")
    return {
        "key_events": [],
        "asset_impacts": {},
        "signals": [],
        "risk_alerts": [],
        "headline_count_used": len(fresh_headlines),
        "stale_excluded": stale_count,
        "model_used": "fallback",
    }


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def _probe_claude_raw() -> tuple[bool, str]:
    """Make a minimal test call to Claude WITHOUT affecting the circuit breaker.
    Returns (success, error_message).
    """
    client = _get_claude_client()
    if client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            return False, "ANTHROPIC_API_KEY not set"
        return False, "anthropic package not installed or client init failed"
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=10,
            system="You are a test probe.",
            messages=[{"role": "user", "content": "Reply with OK"}],
        )
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def check_llm_availability() -> dict:
    """Check which LLM providers are configured and reachable.
    Diagnostic probes do NOT affect the circuit breaker state.
    """
    try:
        import trading.config  # noqa: F401
    except Exception:
        pass

    health = get_provider_health()

    claude_configured = bool(os.getenv("ANTHROPIC_API_KEY"))
    groq_configured = bool(os.getenv("GROQ_API_KEY"))

    # Only probe if configured and circuit is not open (avoid hammering a known-broken provider)
    claude_reachable = False
    claude_probe_error = ""
    if claude_configured and health.get("claude", {}).get("status") != "circuit_open":
        claude_reachable, claude_probe_error = _probe_claude_raw()
    elif health.get("claude", {}).get("status") == "circuit_open":
        claude_probe_error = f"circuit open — {health['claude'].get('retry_in_seconds', '?')}s until retry"

    groq_reachable = False
    groq_probe_error = ""
    if groq_configured and health.get("groq", {}).get("status") != "circuit_open":
        result = _call_groq("Say OK", "ping", max_tokens=10)
        groq_reachable = result is not None

    status = {
        "claude": {
            "configured": claude_configured,
            "reachable": claude_reachable,
            "circuit_status": health.get("claude", {}).get("status", "ok"),
            "failures": health.get("claude", {}).get("failures", 0),
            "last_error": health.get("claude", {}).get("last_error") or claude_probe_error,
        },
        "groq": {
            "configured": groq_configured,
            "reachable": groq_reachable,
            "circuit_status": health.get("groq", {}).get("status", "ok"),
            "failures": health.get("groq", {}).get("failures", 0),
            "last_error": health.get("groq", {}).get("last_error") or groq_probe_error,
        },
    }

    reachable = {k for k, v in status.items() if v["reachable"]}
    status["any_available"] = bool(reachable)
    status["active_provider"] = "+".join(sorted(reachable)) if reachable else "none"
    status["reasoning_tier"] = "claude" if status["claude"]["reachable"] else "none"
    status["background_tier"] = "groq" if status["groq"]["reachable"] else "none"
    return status
