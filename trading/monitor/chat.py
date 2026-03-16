"""Dashboard chat assistant — answers questions by querying the trading system.

Enhanced with full system awareness: backtests, market intelligence,
autonomous agents, portfolio allocation, trailing stops, and more.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

from trading.db.store import (
    get_db, get_trades, get_signals, get_daily_pnl,
    get_action_log, get_reviews, get_journal, search_knowledge,
    get_pending_adaptations, get_recommendation_history,
    get_pending_recommendations,
)


def handle_chat(message: str) -> str:
    """Route a user question — always use LLM if available, conversational fallback otherwise."""
    msg = message.lower().strip()

    # Handle greetings and casual messages without needing data or LLM
    greeting_response = _handle_greeting(msg)
    if greeting_response:
        return greeting_response

    # Step 1: Gather relevant data based on intent (fast regex matching)
    structured_data = _gather_intent_data(msg, message)

    # Step 2: Send to LLM with gathered data for a conversational response
    llm_answer = _llm_respond(message, structured_data)
    if llm_answer:
        return llm_answer

    # Step 3: If LLM unavailable, return data with conversational framing
    if structured_data:
        return _conversational_wrap(msg, structured_data)

    # Step 4: Try a general portfolio summary for unrecognized questions
    try:
        portfolio_summary = _portfolio_answer()
        if portfolio_summary:
            return (
                f"I'm not sure exactly what you're asking about, but here's a quick overview of your system:\n\n"
                f"{portfolio_summary}\n\n"
                f"*Try asking about specific topics — positions, strategies, trades, risk, signals, P&L, or type \"help\" for the full list.*"
            )
    except Exception:
        pass

    return _help_answer()


def _handle_greeting(msg: str) -> str | None:
    """Handle greetings and casual messages conversationally, no LLM needed."""
    greetings = ["hello", "hi", "hey", "yo", "sup", "what's up", "whats up",
                 "good morning", "good evening", "good afternoon", "howdy", "greetings"]
    thanks = ["thank", "thanks", "thx", "cheers", "appreciate"]
    goodbyes = ["bye", "goodbye", "see ya", "later", "good night", "gn"]

    if any(msg.startswith(g) or msg == g for g in greetings):
        # Gather a quick status snapshot
        try:
            from trading.execution.router import get_account, get_positions_from_aster
            acct = get_account()
            positions = get_positions_from_aster()
            pv = acct.get("portfolio_value", 0) if acct else 0
            n_pos = len(positions) if positions else 0
            mode = "paper" if (acct or {}).get("paper", True) else "live"

            total_pnl = sum(p.get("unrealized_pnl", 0) for p in (positions or []))
            pnl_sign = "+" if total_pnl >= 0 else ""
            pnl_emoji = "up" if total_pnl >= 0 else "down"

            return (
                f"Hey! Welcome to **OT-CORP Trading Command**.\n\n"
                f"Here's your quick snapshot:\n"
                f"- **Portfolio:** ${pv:,.2f} ({mode} mode)\n"
                f"- **Open Positions:** {n_pos}\n"
                f"- **Unrealized P&L:** {pnl_sign}${total_pnl:,.2f} ({pnl_emoji})\n\n"
                f"What would you like to know? I can help with *positions, strategies, trades, risk, signals, P&L, market intelligence,* and more."
            )
        except Exception:
            return (
                "Hey! Welcome to **OT-CORP Trading Command**.\n\n"
                "I'm your trading assistant. Ask me about:\n"
                "- **Positions** & portfolio status\n"
                "- **Strategies** & performance\n"
                "- **Trades** & execution history\n"
                "- **Risk** exposure & stops\n"
                "- **Market intelligence** & signals\n\n"
                "What would you like to know?"
            )

    if any(t in msg for t in thanks):
        return "You're welcome! Let me know if you need anything else about your portfolio or trading system."

    if any(g in msg for g in goodbyes):
        return "See you later! Your trading system continues running autonomously. I'll be here when you need me."

    return None


def _conversational_wrap(msg: str, data: str) -> str:
    """Wrap raw structured data in a conversational frame when LLM is unavailable."""
    # Determine the topic for a natural intro
    if any(w in msg for w in ["position", "holding"]):
        intro = "Here's what I found about your positions:"
    elif any(w in msg for w in ["portfolio", "account", "balance", "status", "how am i"]):
        intro = "Here's your current portfolio status:"
    elif any(w in msg for w in ["trade", "execution", "order", "bought", "sold"]):
        intro = "Here's your recent trading activity:"
    elif any(w in msg for w in ["strategy", "strat"]):
        intro = "Here's the strategy breakdown:"
    elif any(w in msg for w in ["pnl", "p&l", "profit", "loss", "return", "performance"]):
        intro = "Here's your P&L summary:"
    elif any(w in msg for w in ["signal"]):
        intro = "Here are the current signals:"
    elif any(w in msg for w in ["risk", "exposure", "drawdown"]):
        intro = "Here's your risk overview:"
    elif any(w in msg for w in ["leverage"]):
        intro = "Here's the leverage information:"
    elif any(w in msg for w in ["intelligence", "sentiment", "market"]):
        intro = "Here's the latest market intelligence:"
    elif any(w in msg for w in ["agent", "autonomous"]):
        intro = "Here's what the autonomous agents are doing:"
    elif any(w in msg for w in ["backtest"]):
        intro = "Here are the backtest results:"
    else:
        intro = "Here's what I found:"

    return f"{intro}\n\n{data}\n\n*Ask me anything else about your trading system, or type \"help\" for all available topics.*"


def _gather_intent_data(msg: str, original: str) -> str:
    """Use regex intent matching to gather structured system data for LLM context."""

    if _match(msg, ["backtest", "back test", "backtested", "historical performance",
                     "how would", "simulate", "paper test"]):
        return _backtest_answer(msg)

    if _match(msg, ["agent", "autonomous", "self-evolving", "improvement",
                     "recommendation", "agent recommendation", "what did the agents"]):
        return _agents_answer(msg)

    if _match(msg, ["intelligence", "briefing", "sentiment", "market mood",
                     "fear", "greed", "macro", "regime", "what's the market"]):
        return _intelligence_answer(msg)

    if _match(msg, ["allocation", "sizing", "how much to buy", "position size",
                     "budget", "how is capital allocated"]):
        return _allocation_answer()

    if _match(msg, ["trailing", "watermark", "profit target", "take profit",
                     "stop trail", "trailing stop"]):
        return _trailing_stops_answer()

    if _match(msg, ["adaptation", "parameter", "tuning", "optimize", "param change",
                     "pending change", "suggested change"]):
        return _adaptations_answer()

    if _match(msg, ["deferred", "pending signal", "etf queue", "market open",
                     "waiting signal"]):
        return _deferred_signals_answer()

    if _match(msg, ["strategy", "strat", "which strategy", "best strategy", "worst strategy",
                     "kalman", "momentum", "divergence", "regime", "factor", "whale",
                     "funding", "basis", "pairs", "meme", "gold", "equity"]):
        return _strategy_answer(msg)

    if _match(msg, ["position", "holding", "open position", "what do i hold", "what am i holding"]):
        return _positions_answer(msg)

    if _match(msg, ["portfolio", "account", "balance", "how much", "total value", "net worth",
                     "how am i doing", "how's it going", "status", "overview"]):
        return _portfolio_answer()

    if _match(msg, ["pnl", "p&l", "profit", "loss", "return", "performance", "daily pnl",
                     "how did i do", "earnings", "made money", "lost money"]):
        return _pnl_answer(msg)

    if _match(msg, ["trade", "execution", "filled", "order", "bought", "sold", "recent trade"]):
        return _trades_answer(msg)

    if _match(msg, ["signal", "buy signal", "sell signal", "what should i"]):
        return _signals_answer(msg)

    if _match(msg, ["risk", "drawdown", "stop loss", "exposure", "max loss",
                     "risk block", "blocked"]):
        return _risk_answer(msg)

    if _match(msg, ["health", "daemon", "running", "system", "uptime", "error", "last run",
                     "is it working", "alive"]):
        return _health_answer()

    if _match(msg, ["leverage", "leverag"]):
        return _leverage_answer()

    if _match(msg, ["review", "weekly", "monthly", "period"]):
        return _reviews_answer()

    if _match(msg, ["journal", "rationale", "lesson", "why did"]):
        return _journal_answer(msg)

    if _match(msg, ["knowledge", "research", "what do you know about", "tell me about"]):
        return _knowledge_answer(msg)

    if _match(msg, ["help", "what can you", "commands", "how to"]):
        return _help_answer()

    # No specific intent matched — knowledge search
    knowledge = search_knowledge(original, limit=3)
    if knowledge:
        answer = "Knowledge base results:\n"
        for k in knowledge:
            answer += f"- {k['title']} ({k['category']}): {(k.get('key_rules') or '')[:200]}\n"
        return answer

    return ""


def _llm_respond(message: str, structured_data: str) -> str | None:
    """Send message + gathered data to LLM for a conversational response."""
    try:
        from trading.llm.engine import ask_llm, TRADING_SYSTEM_PROMPT
    except ImportError as e:
        log.debug("LLM engine not available: %s", e)
        return None

    # Check if any LLM provider is configured before doing expensive context gather
    import os
    has_claude = bool(os.getenv("ANTHROPIC_API_KEY", ""))
    has_gemini = bool(os.getenv("GEMINI_API_KEY", ""))
    if not has_claude and not has_gemini:
        log.debug("No LLM API keys configured (ANTHROPIC_API_KEY or GEMINI_API_KEY)")
        return None

    # Also gather live system context
    context = _gather_system_context()

    prompt = f"""The operator asks: "{message}"

RELEVANT SYSTEM DATA (gathered from live queries):
{structured_data if structured_data else "(No specific data matched — use the system context below)"}

CURRENT SYSTEM STATE:
{json.dumps(context, indent=2, default=str)}

Instructions:
- Answer conversationally and naturally, like a knowledgeable trading co-pilot
- Reference specific numbers, strategies, and timestamps from the data above
- Be concise but thorough — highlight what matters most
- Use markdown formatting (bold for key numbers, bullet points for lists)
- If the data shows something concerning (large losses, risk blocks, system errors), proactively mention it
- Don't just reformat the data — add insight, analysis, and actionable observations"""

    try:
        result = ask_llm(TRADING_SYSTEM_PROMPT, prompt, tier="quality")
        if result and "LLM unavailable" not in result:
            return result
        log.warning("LLM returned unavailable response: %s", result[:100] if result else "None")
    except Exception as e:
        log.warning("LLM call failed: %s", e)

    return None


# ---------------------------------------------------------------------------
# Intent matching
# ---------------------------------------------------------------------------

def _match(msg: str, keywords: list[str]) -> bool:
    return any(kw in msg for kw in keywords)


def _extract_symbol(msg: str) -> str | None:
    """Try to extract a coin/symbol name from the message."""
    coins = {
        "btc": "BTC", "bitcoin": "BTC",
        "eth": "ETH", "ethereum": "ETH",
        "sol": "SOL", "solana": "SOL",
        "doge": "DOGE", "dogecoin": "DOGE",
        "avax": "AVAX", "avalanche": "AVAX",
        "ada": "ADA", "cardano": "ADA",
        "xrp": "XRP", "ripple": "XRP",
        "dot": "DOT", "polkadot": "DOT",
        "link": "LINK", "chainlink": "LINK",
        "uni": "UNI", "uniswap": "UNI",
        "sui": "SUI",
        "bnb": "BNB", "ton": "TON", "toncoin": "TON",
        "near": "NEAR", "apt": "APT", "aptos": "APT",
        "arb": "ARB", "arbitrum": "ARB",
        "op": "OP", "optimism": "OP",
        "aave": "AAVE", "pepe": "PEPE",
        "shib": "SHIB", "shiba": "SHIB",
        "bonk": "BONK", "floki": "FLOKI",
        "trump": "TRUMP", "gold": "XAU", "xau": "XAU",
    }
    for key, sym in coins.items():
        if re.search(r'\b' + key + r'\b', msg):
            return sym
    return None


# ---------------------------------------------------------------------------
# NEW: Backtest results
# ---------------------------------------------------------------------------

def _backtest_answer(msg: str) -> str:
    """Show backtest results from saved JSON files."""
    bt_dir = Path(__file__).parent.parent / "knowledge" / "backtests"
    if not bt_dir.exists():
        return "No backtest results found. Run `python -m trading.backtest.run_all` to generate."

    # Find the most recent backtest file
    bt_files = sorted(bt_dir.glob("backtest_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not bt_files:
        return "No backtest results found."

    # Check if asking for leverage analysis
    if "leverage" in msg:
        lev_files = sorted(bt_dir.glob("leverage_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if lev_files:
            return _format_leverage_analysis(lev_files[0])

    # Load most recent backtest
    latest = bt_files[0]
    try:
        data = json.loads(latest.read_text())
    except Exception:
        return "Failed to parse backtest results file."

    days = data.get("days", "?")
    capital = data.get("starting_capital", 100_000)
    run_date = data.get("run_date", "unknown")[:10]
    strategies = data.get("strategies", [])

    if not strategies:
        return "Backtest file is empty."

    answer = f"**Backtest Results** ({days}-day | ${capital:,.0f} capital)\n"
    answer += f"*Run: {run_date}*\n\n"
    answer += "| Strategy | Return | Trades | Win Rate | Sharpe |\n|---|---|---|---|---|\n"

    for s in strategies[:20]:
        ret = s.get("total_return_pct", 0)
        m = s.get("metrics", {})
        trades = m.get("total_trades", 0)
        wr = m.get("win_rate", 0) * 100
        sharpe = m.get("sharpe_ratio", 0)
        color = "+" if ret >= 0 else ""
        answer += f"| {s['strategy']} | {color}{ret:.2f}% | {trades} | {wr:.0f}% | {sharpe:.2f} |\n"

    # Summary
    positive = sum(1 for s in strategies if s.get("total_return_pct", 0) > 0)
    answer += f"\n{positive}/{len(strategies)} strategies profitable"

    # Show available backtest files
    if len(bt_files) > 1:
        answer += f"\n\n*{len(bt_files)} backtest runs available*"

    return answer


def _format_leverage_analysis(filepath: Path) -> str:
    """Format leverage analysis results."""
    try:
        data = json.loads(filepath.read_text())
    except Exception:
        return "Failed to parse leverage analysis file."

    answer = "**Leverage Analysis Results**\n\n"

    if isinstance(data, dict):
        for strat, info in list(data.items())[:15]:
            if isinstance(info, dict):
                rec = info.get("recommended_leverage", info.get("leverage", "?"))
                sharpe = info.get("sharpe", info.get("sharpe_ratio", 0))
                answer += f"- **{strat}**: {rec}x leverage (Sharpe: {sharpe:.2f})\n"

    return answer


# ---------------------------------------------------------------------------
# NEW: Autonomous agents
# ---------------------------------------------------------------------------

def _agents_answer(msg: str) -> str:
    """Show autonomous agent recommendations and activity."""
    answer = "**Autonomous Agent Activity**\n\n"

    # Recent recommendations from the agent system
    try:
        recs = get_recommendation_history(limit=20)
    except Exception:
        recs = []

    if not recs:
        # Try action log for agent activity
        with get_db() as conn:
            agent_actions = [dict(r) for r in conn.execute(
                "SELECT * FROM action_log WHERE category IN ('review', 'system', 'scheduler') "
                "ORDER BY timestamp DESC LIMIT 15"
            ).fetchall()]

        if agent_actions:
            answer += "**Recent System Actions:**\n\n"
            for a in agent_actions[:10]:
                ts = (a.get("timestamp") or "")[:16]
                answer += f"- `{ts}` [{a['category']}] {a['action']}"
                if a.get("details"):
                    answer += f" — {a['details'][:80]}"
                answer += "\n"
            return answer

        return answer + "No agent recommendations recorded yet. The autonomous cycle runs periodically."

    # Show recommendations grouped by status
    pending = [r for r in recs if r.get("status") == "pending"]
    resolved = [r for r in recs if r.get("status") != "pending"]

    if pending:
        answer += f"**Pending Recommendations ({len(pending)}):**\n"
        for r in pending[:8]:
            answer += f"- [{r.get('from_agent', '?')} → {r.get('to_agent', '?')}] "
            answer += f"{r.get('category', '')}: {r.get('recommendation', '')[:100]}\n"
        answer += "\n"

    if resolved:
        answer += f"**Recent Resolved ({len(resolved)}):**\n"
        for r in resolved[:8]:
            status = r.get("status", "?")
            answer += f"- [{status}] {r.get('category', '')}: {r.get('recommendation', '')[:80]}\n"

    return answer


# ---------------------------------------------------------------------------
# NEW: Market intelligence
# ---------------------------------------------------------------------------

def _intelligence_answer(msg: str) -> str:
    """Show market intelligence briefing and sentiment."""
    answer = "**Market Intelligence**\n\n"

    # Fear & Greed from recent data
    try:
        from trading.data.crypto import get_fear_greed_index
        fg = get_fear_greed_index()
        if fg:
            latest = fg[0] if isinstance(fg, list) else fg
            if isinstance(latest, dict):
                val = latest.get("value", "?")
                classification = latest.get("value_classification", "")
                answer += f"**Fear & Greed Index:** {val} ({classification})\n\n"
    except Exception:
        pass

    # Try to get latest intelligence briefing from action log
    with get_db() as conn:
        # Look for intelligence-related actions
        intel_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM action_log WHERE "
            "(action LIKE '%briefing%' OR action LIKE '%intelligence%' OR action LIKE '%regime%' OR action LIKE '%sentiment%') "
            "ORDER BY timestamp DESC LIMIT 5"
        ).fetchall()]

        # Check for regime detection signals
        regime_signals = [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE strategy IN ('hmm_regime', 'volatility_regime', 'regime_mean_reversion') "
            "ORDER BY timestamp DESC LIMIT 5"
        ).fetchall()]

    if intel_rows:
        answer += "**Recent Briefings:**\n"
        for row in intel_rows[:3]:
            ts = (row.get("timestamp") or "")[:16]
            answer += f"- `{ts}` {row['action']}"
            if row.get("details"):
                answer += f": {row['details'][:120]}"
            answer += "\n"
        answer += "\n"

    if regime_signals:
        answer += "**Regime Signals:**\n"
        for s in regime_signals[:5]:
            ts = (s.get("timestamp") or "")[:16]
            sig = (s.get("signal") or "").upper()
            strength = s.get("strength", 0)
            answer += f"- `{ts}` {s['strategy']} → **{sig}** {s.get('symbol', '')} (strength: {strength:.2f})\n"

            # Try to parse signal data for regime info
            data = s.get("data")
            if data and isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = None
            if isinstance(data, dict):
                regime = data.get("regime") or data.get("current_regime") or data.get("market_regime")
                if regime:
                    answer += f"  Current regime: **{regime}**\n"

    if not intel_rows and not regime_signals:
        answer += "No intelligence briefings recorded yet.\n"
        answer += "The system generates market briefings before each trading cycle."

    return answer


# ---------------------------------------------------------------------------
# NEW: Portfolio allocation
# ---------------------------------------------------------------------------

def _allocation_answer() -> str:
    """Explain how capital is allocated across strategies."""
    answer = "**Portfolio Allocation System**\n\n"

    try:
        from trading.config import STRATEGY_ENABLED
        from trading.risk.portfolio import STRATEGY_BUDGETS, DEFAULT_BUDGET
    except ImportError:
        return answer + "Allocation module not available."

    enabled = {k for k, v in STRATEGY_ENABLED.items() if v}

    answer += "Capital allocation uses **6 multiplicative factors**:\n\n"
    answer += "1. **Base Budget** — floor allocation per strategy (3-6%)\n"
    answer += "2. **Confluence Boost** — more strategies agreeing = bigger position\n"
    answer += "3. **Regime Alignment** — intelligence briefing confirms direction\n"
    answer += "4. **Performance Tilt** — recent win rate shifts capital to winners\n"
    answer += "5. **Volatility Scaling** — ATR-based inverse vol sizing\n"
    answer += "6. **Signal Strength** — raw confidence from strategy\n\n"

    # Show base budgets
    answer += "**Base Budgets:**\n"
    answer += "| Strategy | Base % |\n|---|---|\n"
    for strat in sorted(enabled):
        base = STRATEGY_BUDGETS.get(strat, DEFAULT_BUDGET) * 100
        answer += f"| {strat} | {base:.1f}% |\n"

    return answer


# ---------------------------------------------------------------------------
# NEW: Trailing stops / watermarks
# ---------------------------------------------------------------------------

def _trailing_stops_answer() -> str:
    """Show active trailing stop watermarks for open positions."""
    from trading.db.store import load_watermarks

    answer = "**Active Trailing Stops & Profit Targets**\n\n"

    try:
        from trading.risk.profit_manager import TAKE_PROFIT_PCT, TRAILING_STOP_ACTIVATE, TRAILING_STOP_PCT
        answer += f"- Take Profit: **{TAKE_PROFIT_PCT*100:.0f}%** gain\n"
        answer += f"- Trail Activates: **{TRAILING_STOP_ACTIVATE*100:.0f}%** gain\n"
        answer += f"- Trail Distance: **{TRAILING_STOP_PCT*100:.0f}%** below high\n\n"
    except ImportError:
        pass

    try:
        watermarks = load_watermarks()
    except Exception:
        watermarks = {}

    if not watermarks:
        answer += "No active watermarks (no positions have reached trail activation)."
        return answer

    answer += "**Active Watermarks:**\n"
    answer += "| Symbol | High Watermark | Trail Stop |\n|---|---|---|\n"
    for symbol, high in sorted(watermarks.items()):
        try:
            trail_price = high * (1 - TRAILING_STOP_PCT)
        except Exception:
            trail_price = high * 0.96
        answer += f"| {symbol} | ${high:,.2f} | ${trail_price:,.2f} |\n"

    return answer


# ---------------------------------------------------------------------------
# NEW: Parameter adaptations
# ---------------------------------------------------------------------------

def _adaptations_answer() -> str:
    """Show pending and recent parameter adaptation suggestions."""
    answer = "**Parameter Optimization Pipeline**\n\n"

    try:
        pending = get_pending_adaptations()
    except Exception:
        pending = []

    if pending:
        answer += f"**Pending Adaptations ({len(pending)}):**\n\n"
        for p in pending[:10]:
            answer += f"- **{p.get('strategy', '?')}** — {p.get('param_name', '?')}: "
            answer += f"`{p.get('old_value', '?')}` → `{p.get('new_value', '?')}`\n"
            if p.get("reason"):
                answer += f"  Reason: {p['reason'][:100]}\n"
    else:
        answer += "No pending parameter changes.\n"

    # Show recent adaptations from action log
    with get_db() as conn:
        recent = [dict(r) for r in conn.execute(
            "SELECT * FROM action_log WHERE action LIKE '%adapt%' OR action LIKE '%param%' "
            "ORDER BY timestamp DESC LIMIT 5"
        ).fetchall()]

    if recent:
        answer += "\n**Recent Adaptation Activity:**\n"
        for a in recent:
            ts = (a.get("timestamp") or "")[:16]
            answer += f"- `{ts}` {a['action']}: {a.get('details', '')[:80]}\n"

    return answer


# ---------------------------------------------------------------------------
# NEW: Deferred signals
# ---------------------------------------------------------------------------

def _deferred_signals_answer() -> str:
    """Show signals waiting for market open (ETF queue)."""
    from trading.db.store import get_deferred_signals

    try:
        deferred = get_deferred_signals()
    except Exception:
        deferred = []

    if not deferred:
        return "**Deferred Signals Queue**\n\nNo pending signals. All signals are being processed in real-time."

    answer = f"**Deferred Signals ({len(deferred)})**\n\n"
    answer += "These signals are queued for the next market open (ETF/equity assets):\n\n"
    answer += "| Symbol | Signal | Strategy | Strength |\n|---|---|---|---|\n"
    for d in deferred:
        answer += f"| {d.get('symbol', '?')} | {(d.get('signal', '')).upper()} | {d.get('strategy', '')} | {d.get('strength', 0):.2f} |\n"

    return answer


# ---------------------------------------------------------------------------
# NEW: Knowledge base search
# ---------------------------------------------------------------------------

def _knowledge_answer(msg: str) -> str:
    """Search the knowledge base for research and strategy documentation."""
    # Extract search query — remove common prefixes
    query = msg
    for prefix in ["knowledge", "research", "tell me about", "what do you know about"]:
        query = query.replace(prefix, "").strip()

    if not query or len(query) < 3:
        # List all knowledge entries
        from trading.db.store import get_knowledge
        try:
            docs = get_knowledge(limit=20)
        except Exception:
            docs = []

        if not docs:
            return "No knowledge base entries yet. Ingest research docs to build the knowledge base."

        answer = f"**Knowledge Base ({len(docs)} documents)**\n\n"
        for d in docs:
            answer += f"- **{d['title']}** ({d.get('category', 'general')})\n"
        return answer

    results = search_knowledge(query, limit=5)
    if not results:
        return f"No knowledge base results for \"{query}\"."

    answer = f"**Knowledge Search: \"{query}\"**\n\n"
    for k in results:
        answer += f"**{k['title']}** ({k.get('category', '')})\n"
        if k.get("key_rules"):
            answer += f"{k['key_rules'][:300]}\n"
        answer += "\n"

    return answer


# ---------------------------------------------------------------------------
# Original answer generators (enhanced)
# ---------------------------------------------------------------------------

def _portfolio_answer() -> str:
    from trading.execution.router import get_account, get_positions_from_aster
    try:
        account = get_account()
    except Exception:
        account = {"portfolio_value": 0, "cash": 0, "buying_power": 0, "equity": 0}

    try:
        positions = get_positions_from_aster()
    except Exception:
        positions = []

    total_pnl = sum(p.get("unrealized_pnl", 0) or 0 for p in positions)
    pos_value = sum(
        p.get("market_value", p.get("qty", 0) * p.get("current_price", 0))
        for p in positions
    )

    pv = account.get("portfolio_value", 0)
    cash = account.get("cash", 0)

    pnl_sign = "+" if total_pnl >= 0 else ""
    answer = f"**Portfolio Overview**\n\n"
    answer += f"- Portfolio Value: **${pv:,.2f}**\n"
    answer += f"- Cash: **${cash:,.2f}**\n"
    answer += f"- Positions Value: **${pos_value:,.2f}**\n"
    answer += f"- Unrealized P&L: **{pnl_sign}${total_pnl:,.2f}**\n"
    answer += f"- Open Positions: **{len(positions)}**\n"

    # Exposure breakdown
    if pv > 0:
        cash_pct = cash / pv * 100
        exposure_pct = pos_value / pv * 100
        answer += f"- Cash Reserve: {cash_pct:.1f}% | Exposure: {exposure_pct:.1f}%\n"

    if positions:
        best = max(positions, key=lambda p: p.get("unrealized_pnl", 0) or 0)
        worst = min(positions, key=lambda p: p.get("unrealized_pnl", 0) or 0)
        best_pnl = best.get("unrealized_pnl", 0) or 0
        worst_pnl = worst.get("unrealized_pnl", 0) or 0
        answer += f"\nBest position: **{best['symbol']}** (+${best_pnl:,.2f})\n"
        answer += f"Worst position: **{worst['symbol']}** (${worst_pnl:,.2f})\n"

    # Add recent P&L trend
    pnl_data = get_daily_pnl(limit=7)
    if len(pnl_data) >= 2:
        first = pnl_data[-1]["portfolio_value"]
        last = pnl_data[0]["portfolio_value"]
        week_ret = (last - first) / first * 100 if first else 0
        answer += f"\n7-day trend: **{'+'if week_ret>=0 else ''}{week_ret:.2f}%**"

    return answer


def _positions_answer(msg: str) -> str:
    from trading.execution.router import get_positions_from_aster
    try:
        positions = get_positions_from_aster()
    except Exception:
        return "Unable to fetch positions right now. The broker connection may be down."

    if not positions:
        return "No open positions currently."

    # Check if asking about a specific symbol
    sym = _extract_symbol(msg)
    if sym:
        matches = [p for p in positions if sym in p["symbol"]]
        if not matches:
            return f"No open position for {sym}."
        p = matches[0]
        pnl = p.get("unrealized_pnl", 0) or 0
        pnl_pct = ((p["current_price"] - p["avg_cost"]) / p["avg_cost"] * 100) if p["avg_cost"] else 0
        answer = f"**{p['symbol']} Position**\n\n"
        answer += f"- Quantity: {p['qty']:.6f}\n"
        answer += f"- Avg Cost: ${p['avg_cost']:,.2f}\n"
        answer += f"- Current Price: ${p['current_price']:,.2f}\n"
        answer += f"- Market Value: ${p.get('market_value', p['qty'] * p['current_price']):,.2f}\n"
        answer += f"- P&L: {'+'if pnl>=0 else ''}${pnl:,.2f} ({'+'if pnl_pct>=0 else ''}{pnl_pct:.2f}%)\n"

        # Show contributing strategies
        strat_trades = get_trades(limit=50)
        strat_trades = [t for t in strat_trades if sym in (t.get("symbol") or "") and t["side"] == "buy"]
        strategies = list({t["strategy"] for t in strat_trades if t.get("strategy")})
        if strategies:
            answer += f"- Strategies: {', '.join(strategies)}\n"

        return answer

    answer = f"**Open Positions ({len(positions)})**\n\n"
    answer += "| Symbol | Qty | Price | P&L |\n|---|---|---|---|\n"
    for p in sorted(positions, key=lambda x: abs(x.get("unrealized_pnl", 0) or 0), reverse=True):
        pnl = p.get("unrealized_pnl", 0) or 0
        answer += f"| {p['symbol']} | {p['qty']:.4f} | ${p['current_price']:,.2f} | {'+'if pnl>=0 else ''}${pnl:,.2f} |\n"
    return answer


def _pnl_answer(msg: str) -> str:
    pnl_data = get_daily_pnl(limit=14)
    if not pnl_data:
        return "No P&L data recorded yet. The system needs to complete at least one trading cycle."

    answer = "**Daily P&L History**\n\n"
    answer += "| Date | Portfolio | Return |\n|---|---|---|\n"
    for p in pnl_data[:10]:
        ret = (p.get("cumulative_return", 0) or 0) * 100
        answer += f"| {p['date']} | ${p['portfolio_value']:,.2f} | {'+'if ret>=0 else ''}{ret:.2f}% |\n"

    # Summary
    if len(pnl_data) >= 2:
        first = pnl_data[-1]["portfolio_value"]
        last = pnl_data[0]["portfolio_value"]
        total_ret = (last - first) / first * 100 if first else 0
        answer += f"\nPeriod return: **{'+'if total_ret>=0 else ''}{total_ret:.2f}%** "
        answer += f"(${first:,.2f} → ${last:,.2f})"

    return answer


def _trades_answer(msg: str) -> str:
    sym = _extract_symbol(msg)

    # Check for time scope
    limit = 20
    if "today" in msg:
        limit = 50  # fetch more, filter below

    trades = get_trades(limit=limit)

    if "today" in msg:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades = [t for t in trades if t["timestamp"].startswith(today)]

    if sym:
        trades = [t for t in trades if sym in (t.get("symbol") or "")]

    if not trades:
        scope = f" for {sym}" if sym else " today" if "today" in msg else ""
        return f"No trades found{scope}."

    answer = f"**Recent Trades ({len(trades)})**\n\n"
    answer += "| Time | Symbol | Side | Amount | Strategy |\n|---|---|---|---|---|\n"
    for t in trades[:15]:
        ts = t["timestamp"][5:16] if t["timestamp"] else ""
        side = (t["side"] or "").upper()
        total = t.get("total", 0) or 0
        strat = t.get("strategy", "") or ""
        answer += f"| {ts} | {t['symbol']} | {side} | ${total:,.2f} | {strat} |\n"

    # Summary
    buys = sum(1 for t in trades if t["side"] == "buy")
    sells = len(trades) - buys
    total_volume = sum(t.get("total", 0) or 0 for t in trades)
    answer += f"\n{buys} buys, {sells} sells — ${total_volume:,.2f} total volume"

    return answer


def _signals_answer(msg: str) -> str:
    signals = get_signals(limit=30)
    if not signals:
        return "No signals recorded yet."

    sym = _extract_symbol(msg)
    if sym:
        signals = [s for s in signals if sym in (s.get("symbol") or "")]

    if not signals:
        return f"No recent signals for {sym}."

    # Group by signal type
    buys = [s for s in signals if s["signal"] == "buy"]
    sells = [s for s in signals if s["signal"] == "sell"]

    answer = f"**Recent Signals** ({len(buys)} buys, {len(sells)} sells)\n\n"

    if buys:
        answer += "**Buy Signals:**\n"
        for s in buys[:8]:
            answer += f"- {s['symbol']} — {s['strategy']} (strength: {s.get('strength', 0):.2f})\n"

    if sells:
        answer += "\n**Sell Signals:**\n"
        for s in sells[:8]:
            answer += f"- {s['symbol']} — {s['strategy']} (strength: {s.get('strength', 0):.2f})\n"

    return answer


def _strategy_answer(msg: str) -> str:
    from trading.config import STRATEGY_ENABLED

    # Check if asking about a specific strategy
    strategy_names = list(STRATEGY_ENABLED.keys())
    asked_strat = None
    for name in strategy_names:
        # Match both underscored and space-separated forms
        if name.replace("_", " ") in msg or name in msg:
            asked_strat = name
            break

    if asked_strat:
        return _single_strategy_answer(asked_strat)

    # General strategy overview
    enabled = {k: v for k, v in STRATEGY_ENABLED.items() if v}
    disabled = {k: v for k, v in STRATEGY_ENABLED.items() if not v}

    answer = f"**Strategy Overview**\n\n"
    answer += f"Enabled: **{len(enabled)}** | Disabled: **{len(disabled)}**\n\n"

    # Get signal counts per strategy from recent signals
    signals = get_signals(limit=200)
    strat_counts: dict[str, dict] = {}
    for s in signals:
        name = s["strategy"]
        if name not in strat_counts:
            strat_counts[name] = {"total": 0, "buys": 0, "sells": 0}
        strat_counts[name]["total"] += 1
        if s["signal"] == "buy":
            strat_counts[name]["buys"] += 1
        elif s["signal"] == "sell":
            strat_counts[name]["sells"] += 1

    # Get trade counts per strategy
    trades = get_trades(limit=200)
    strat_trades: dict[str, int] = {}
    for t in trades:
        name = t.get("strategy", "")
        if name:
            strat_trades[name] = strat_trades.get(name, 0) + 1

    answer += "| Strategy | Signals | Trades | Status |\n|---|---|---|---|\n"
    for name in sorted(enabled.keys()):
        sc = strat_counts.get(name, {"total": 0})
        tc = strat_trades.get(name, 0)
        answer += f"| {name} | {sc['total']} | {tc} | Active |\n"

    if disabled:
        answer += f"\n**Disabled ({len(disabled)}):** {', '.join(sorted(disabled.keys()))}"

    return answer


def _single_strategy_answer(name: str) -> str:
    signals = get_signals(limit=100)
    strat_signals = [s for s in signals if s["strategy"] == name]

    trades = get_trades(limit=100, strategy=name)

    answer = f"**Strategy: {name}**\n\n"
    answer += f"- Recent signals: {len(strat_signals)}\n"
    answer += f"- Recent trades: {len(trades)}\n"

    if strat_signals:
        buys = sum(1 for s in strat_signals if s["signal"] == "buy")
        sells = sum(1 for s in strat_signals if s["signal"] == "sell")
        holds = len(strat_signals) - buys - sells
        answer += f"- Signal breakdown: {buys} buys, {sells} sells, {holds} holds\n"

        # Latest signal
        latest = strat_signals[0]
        answer += f"- Latest signal: **{latest['signal'].upper()}** {latest['symbol']} "
        answer += f"(strength: {latest.get('strength', 0):.2f}) at {latest['timestamp'][:16]}\n"

    if trades:
        total_pnl = sum(t.get("pnl", 0) or 0 for t in trades if t.get("pnl"))
        closed = [t for t in trades if t.get("closed_at")]
        answer += f"- Closed trades: {len(closed)}\n"
        if closed:
            wins = sum(1 for t in closed if (t.get("pnl") or 0) > 0)
            answer += f"- Win rate: {wins/len(closed)*100:.0f}%\n"
            answer += f"- Total P&L: {'+'if total_pnl>=0 else ''}${total_pnl:,.2f}\n"

    # Symbols traded
    symbols = list({s["symbol"] for s in strat_signals})
    if symbols:
        answer += f"- Symbols: {', '.join(symbols[:10])}\n"

    # Check for backtest data
    bt_dir = Path(__file__).parent.parent / "knowledge" / "backtests"
    if bt_dir.exists():
        bt_files = sorted(bt_dir.glob("backtest_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if bt_files:
            try:
                data = json.loads(bt_files[0].read_text())
                for s in data.get("strategies", []):
                    if s["strategy"] == name:
                        ret = s.get("total_return_pct", 0)
                        m = s.get("metrics", {})
                        answer += f"\n**Backtest ({data.get('days', '?')}d):**\n"
                        answer += f"- Return: {'+'if ret>=0 else ''}{ret:.2f}%\n"
                        answer += f"- Sharpe: {m.get('sharpe_ratio', 0):.2f}\n"
                        answer += f"- Max DD: {m.get('max_drawdown', 0)*100:.1f}%\n"
                        break
            except Exception:
                pass

    return answer


def _risk_answer(msg: str) -> str:
    from trading.config import RISK
    from trading.execution.router import get_account, get_positions_from_aster

    try:
        account = get_account()
        positions = get_positions_from_aster()
    except Exception:
        account = {"portfolio_value": 0, "cash": 0}
        positions = []

    pv = account.get("portfolio_value", 0)
    cash = account.get("cash", 0)
    pos_value = sum(
        p.get("market_value", p.get("qty", 0) * p.get("current_price", 0))
        for p in positions
    )

    # Risk metrics
    cash_pct = (cash / pv * 100) if pv else 0
    exposure_pct = (pos_value / pv * 100) if pv else 0

    # Risk blocks today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as conn:
        risk_blocks = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE category='risk_block' AND timestamp LIKE ?",
            (f"{today}%",)
        ).fetchone()[0]

        # Recent risk block reasons
        block_reasons = [dict(r) for r in conn.execute(
            "SELECT action, details FROM action_log WHERE category='risk_block' "
            "ORDER BY timestamp DESC LIMIT 5"
        ).fetchall()]

    answer = "**Risk Overview**\n\n"
    answer += f"- Portfolio Value: ${pv:,.2f}\n"
    answer += f"- Cash Reserve: ${cash:,.2f} ({cash_pct:.1f}%)\n"
    answer += f"- Market Exposure: ${pos_value:,.2f} ({exposure_pct:.1f}%)\n"
    answer += f"- Open Positions: {len(positions)}\n"
    answer += f"- Risk Blocks Today: {risk_blocks}\n\n"

    answer += "**Risk Parameters:**\n"
    answer += f"- Max Position Size: {RISK['max_position_pct']*100:.0f}%\n"
    answer += f"- Stop Loss: {RISK['stop_loss_pct']*100:.0f}%\n"
    answer += f"- Max Daily Loss: {RISK['max_daily_loss_pct']*100:.0f}%\n"
    answer += f"- Max Drawdown (halt): {RISK['max_drawdown_pct']*100:.0f}%\n"
    answer += f"- Cash Reserve Min: {RISK['min_cash_reserve_pct']*100:.0f}%\n"
    answer += f"- Max Trades/Day: {RISK['max_trades_per_day']}\n"

    # Position concentration
    if positions and pv:
        largest = max(positions, key=lambda p: p.get("market_value", 0) or 0)
        largest_pct = (largest.get("market_value", 0) or 0) / pv * 100
        answer += f"\nLargest position: **{largest['symbol']}** ({largest_pct:.1f}% of portfolio)"

    # Recent risk blocks
    if block_reasons:
        answer += "\n\n**Recent Risk Blocks:**\n"
        for b in block_reasons[:3]:
            answer += f"- {b['action']}: {b.get('details', '')[:80]}\n"

    return answer


def _health_answer() -> str:
    import time
    from trading.monitor.web import _START_TIME

    with get_db() as conn:
        # Last cycle
        row = conn.execute(
            "SELECT timestamp FROM action_log WHERE action='cycle_complete' "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        last_cycle = row["timestamp"] if row else "Never"

        # Recent errors
        errors = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE category='error' AND timestamp > ?",
            ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),)
        ).fetchone()[0]

        # Total actions today
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        actions_today = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE timestamp LIKE ?",
            (f"{today}%",)
        ).fetchone()[0]

        # Cycle frequency
        cycles = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE action='cycle_complete' AND timestamp > ?",
            ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),)
        ).fetchone()[0]

        # Recent error details
        recent_errors = [dict(r) for r in conn.execute(
            "SELECT action, details, timestamp FROM action_log WHERE category='error' "
            "ORDER BY timestamp DESC LIMIT 3"
        ).fetchall()]

    uptime = time.monotonic() - _START_TIME
    hours = int(uptime // 3600)
    minutes = int((uptime % 3600) // 60)

    answer = "**System Health**\n\n"
    answer += f"- Status: **{'Operational' if errors < 5 else 'Degraded'}**\n"
    answer += f"- Dashboard Uptime: {hours}h {minutes}m\n"
    answer += f"- Last Cycle: {last_cycle[:19] if last_cycle != 'Never' else 'Never'}\n"
    answer += f"- Cycles (24h): {cycles}\n"
    answer += f"- Errors (24h): {errors}\n"
    answer += f"- Actions Today: {actions_today}\n"

    if recent_errors:
        answer += "\n**Recent Errors:**\n"
        for e in recent_errors:
            ts = (e.get("timestamp") or "")[:16]
            answer += f"- `{ts}` {e.get('details', e.get('action', ''))[:80]}\n"

    return answer


def _leverage_answer() -> str:
    from trading.config import (
        LEVERAGE_PROFILE, LEVERAGE_CONSERVATIVE, LEVERAGE_MODERATE,
        LEVERAGE_AGGRESSIVE, LEVERAGE_GREEDY,
    )

    profiles = {
        "conservative": LEVERAGE_CONSERVATIVE,
        "moderate": LEVERAGE_MODERATE,
        "aggressive": LEVERAGE_AGGRESSIVE,
        "greedy": LEVERAGE_GREEDY,
    }

    answer = f"**Leverage Configuration**\n\n"
    answer += f"Active Profile: **{LEVERAGE_PROFILE}**\n\n"

    # Show only active profile in detail
    active = profiles.get(LEVERAGE_PROFILE, {})
    if active:
        answer += "| Strategy | Leverage |\n|---|---|\n"
        for strat, lev in sorted(active.items()):
            answer += f"| {strat} | {lev}x |\n"

    answer += f"\n*Available profiles: {', '.join(profiles.keys())}*"

    return answer


def _reviews_answer() -> str:
    reviews = get_reviews(limit=5)
    if not reviews:
        return "No performance reviews recorded yet."

    answer = "**Performance Reviews**\n\n"
    for r in reviews:
        answer += f"**{r['period']}** ({r['start_date']} → {r['end_date']})\n"
        answer += f"- Trades: {r['total_trades']} | Win Rate: {(r['win_rate'] or 0)*100:.0f}%\n"
        total_pnl = r.get("total_pnl", 0) or 0
        answer += f"- P&L: {'+'if total_pnl>=0 else ''}${total_pnl:,.2f}\n"
        answer += f"- Sharpe: {r.get('sharpe_ratio', 0):.2f} | Max DD: {(r.get('max_drawdown', 0) or 0)*100:.1f}%\n"
        if r.get("best_strategy"):
            answer += f"- Best: {r['best_strategy']} | Worst: {r.get('worst_strategy', 'N/A')}\n"
        answer += "\n"

    return answer


def _journal_answer(msg: str) -> str:
    journal = get_journal(limit=10)
    if not journal:
        return "No journal entries recorded yet."

    answer = "**Trade Journal** (recent entries)\n\n"
    for j in journal[:5]:
        sym = j.get("symbol", "")
        side = (j.get("side", "") or "").upper()
        answer += f"**{sym} {side}** — {j.get('strategy', '')}\n"
        answer += f"{j.get('rationale', 'No rationale')}\n"
        if j.get("lesson"):
            answer += f"*Lesson: {j['lesson']}*\n"
        answer += f"_{j['timestamp'][:16]}_\n\n"

    return answer


def _help_answer() -> str:
    return (
        "**Chat Assistant — Full System Awareness**\n\n"
        "I have access to every part of the trading system. Ask me about:\n\n"
        "**Trading:**\n"
        "- *\"How is my portfolio?\"* — balance, P&L, exposure\n"
        "- *\"Show my positions\"* — all open holdings\n"
        "- *\"Tell me about my BTC position\"* — specific asset details\n"
        "- *\"What trades happened today?\"* — recent executions\n"
        "- *\"What signals are active?\"* — strategy recommendations\n\n"
        "**Strategies:**\n"
        "- *\"Strategy overview\"* — all enabled strategies\n"
        "- *\"How is kalman_trend doing?\"* — specific strategy stats\n"
        "- *\"Show backtest results\"* — historical performance\n"
        "- *\"How is capital allocated?\"* — position sizing factors\n\n"
        "**Risk & System:**\n"
        "- *\"What's my risk exposure?\"* — risk metrics & blocks\n"
        "- *\"Show trailing stops\"* — active watermarks & targets\n"
        "- *\"What leverage am I using?\"* — leverage profiles\n"
        "- *\"Is the system healthy?\"* — daemon, errors, uptime\n\n"
        "**Intelligence:**\n"
        "- *\"What's the market sentiment?\"* — fear/greed, regime\n"
        "- *\"What are the agents recommending?\"* — autonomous system\n"
        "- *\"Any pending parameter changes?\"* — optimization pipeline\n"
        "- *\"Show deferred signals\"* — ETF queue\n"
        "- *\"Search knowledge base\"* — research & strategy docs\n"
        "- *\"Show reviews\"* — performance summaries\n"
        "- *\"Show journal\"* — trade rationale & lessons\n"
    )


def _gather_system_context() -> dict:
    """Collect current system state for LLM context injection."""
    import logging as _logging
    log = _logging.getLogger(__name__)

    context: dict = {}

    # Portfolio
    try:
        from trading.execution.router import get_account, get_positions_from_aster
        context["account"] = get_account()
        context["positions"] = get_positions_from_aster()[:10]
    except Exception:
        context["account"] = {}
        context["positions"] = []

    # Recent trades
    try:
        context["recent_trades"] = get_trades(limit=10)
    except Exception:
        context["recent_trades"] = []

    # P&L
    try:
        context["pnl"] = get_daily_pnl(limit=7)
    except Exception:
        context["pnl"] = []

    # Active signals
    try:
        context["signals"] = get_signals(limit=10)
    except Exception:
        context["signals"] = []

    # Recent system actions
    try:
        context["actions"] = get_action_log(limit=10)
    except Exception:
        context["actions"] = []

    # Strategy config
    try:
        from trading.config import STRATEGY_ENABLED, LEVERAGE_PROFILE
        context["enabled_strategies"] = [k for k, v in STRATEGY_ENABLED.items() if v]
        context["leverage_profile"] = LEVERAGE_PROFILE
    except Exception:
        pass

    # Agent recommendations
    try:
        context["pending_recommendations"] = get_pending_recommendations()[:5]
    except Exception:
        context["pending_recommendations"] = []

    # Timestamp
    context["timestamp"] = datetime.now(timezone.utc).isoformat()

    return context
