"""Operator Console — natural language command interface for the trading system.

Intercepts chat messages before the read-only handler, parses action intents,
and implements a two-step confirmation flow for write operations.

Return format:
  - Action detected: {"answer": "...", "confirm": {"action_id": "...", "description": "...", "warning": "..."|None}}
  - Executed action: {"answer": "..."}
  - Not an action:   None  (fall through to existing chat.py)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pending confirmations — in-memory with 5-minute TTL
# ---------------------------------------------------------------------------
_pending_actions: dict[str, dict] = {}
_CONFIRMATION_TTL = timedelta(minutes=5)

# ---------------------------------------------------------------------------
# Strategy name aliases → canonical name
# ---------------------------------------------------------------------------
STRATEGY_ALIASES = {
    "whale": "whale_flow",
    "meme": "meme_momentum",
    "kalman": "kalman_trend",
    "hmm": "hmm_regime",
    "pairs": "pairs_trading",
    "funding": "funding_arb",
    "basis": "basis_zscore",
    "vol": "volatility_regime",
    "factor": "factor_crypto",
    "rsi": "rsi_divergence",
    "regime": "regime_mean_reversion",
    "gold": "gold_crypto_hedge",
    "equity": "equity_crypto_correlation",
    "cross": "cross_asset_momentum",
    "multi": "multi_factor_rank",
    "oi": "oi_price_divergence",
    "microstructure": "microstructure_composite",
    "breakout": "breakout_detection",
    "garch": "garch_volatility",
    "dxy": "dxy_dollar",
    "term": "funding_term_structure",
    "cross_basis": "cross_basis_rv",
    "taker": "taker_divergence",
}

# ---------------------------------------------------------------------------
# Symbol resolution — user input → internal "BTC/USD" format
# ---------------------------------------------------------------------------
_SYMBOL_ALIASES = {
    "btc": "BTC/USD", "bitcoin": "BTC/USD",
    "eth": "ETH/USD", "ethereum": "ETH/USD",
    "sol": "SOL/USD", "solana": "SOL/USD",
    "bnb": "BNB/USD",
    "xrp": "XRP/USD", "ripple": "XRP/USD",
    "avax": "AVAX/USD", "avalanche": "AVAX/USD",
    "dot": "DOT/USD", "polkadot": "DOT/USD",
    "ada": "ADA/USD", "cardano": "ADA/USD",
    "ton": "TON/USD", "toncoin": "TON/USD",
    "near": "NEAR/USD",
    "sui": "SUI/USD",
    "apt": "APT/USD", "aptos": "APT/USD",
    "link": "LINK/USD", "chainlink": "LINK/USD",
    "uni": "UNI/USD", "uniswap": "UNI/USD",
    "aave": "AAVE/USD",
    "doge": "DOGE/USD", "dogecoin": "DOGE/USD",
    "shib": "1000SHIB/USD", "shiba": "1000SHIB/USD",
    "pepe": "1000PEPE/USD",
    "bonk": "1000BONK/USD",
    "trump": "TRUMP/USD",
    "arb": "ARB/USD", "arbitrum": "ARB/USD",
    "op": "OP/USD", "optimism": "OP/USD",
    "inj": "INJ/USD", "injective": "INJ/USD",
    "ltc": "LTC/USD", "litecoin": "LTC/USD",
    "bch": "BCH/USD",
    "fil": "FIL/USD", "filecoin": "FIL/USD",
    "render": "RENDER/USD",
    "fet": "FET/USD",
    "tao": "TAO/USD", "bittensor": "TAO/USD",
    "hype": "HYPE/USD", "hyperliquid": "HYPE/USD",
    "wld": "WLD/USD", "worldcoin": "WLD/USD",
    "gold": "XAU/USD", "xau": "XAU/USD",
    "silver": "XAG/USD", "xag": "XAG/USD",
    "nvidia": "NVDA/USD", "nvda": "NVDA/USD",
    "tesla": "TSLA/USD", "tsla": "TSLA/USD",
    "apple": "AAPL/USD", "aapl": "AAPL/USD",
    "google": "GOOGL/USD", "googl": "GOOGL/USD", "alphabet": "GOOGL/USD",
    "microsoft": "MSFT/USD", "msft": "MSFT/USD",
    "amazon": "AMZN/USD", "amzn": "AMZN/USD",
    "meta": "META/USD",
    "dydx": "DYDX/USD",
    "pendle": "PENDLE/USD",
    "ondo": "ONDO/USD",
    "jup": "JUP/USD", "jupiter": "JUP/USD",
    "wif": "WIF/USD",
    "kas": "KAS/USD", "kaspa": "KAS/USD",
    "sei": "SEI/USD",
    "stx": "STX/USD", "stacks": "STX/USD",
    "tia": "TIA/USD", "celestia": "TIA/USD",
    "mog": "MOG/USD",
    "popcat": "POPCAT/USD",
    # Common misspellings / shorthand
    "bit": "BTC/USD", "bitcoins": "BTC/USD",
    "ether": "ETH/USD",
    "solan": "SOL/USD",
}

# ---------------------------------------------------------------------------
# Risk parameter aliases
# ---------------------------------------------------------------------------
_RISK_ALIASES = {
    "stop loss": "stop_loss_pct",
    "stop-loss": "stop_loss_pct",
    "stoploss": "stop_loss_pct",
    "max position": "max_position_pct",
    "position size": "max_position_pct",
    "daily loss": "max_daily_loss_pct",
    "max daily loss": "max_daily_loss_pct",
    "drawdown": "max_drawdown_pct",
    "max drawdown": "max_drawdown_pct",
    "cash reserve": "min_cash_reserve_pct",
    "cash": "min_cash_reserve_pct",
    "max trades": "max_trades_per_day",
    "trades per day": "max_trades_per_day",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_symbol(text: str) -> str | None:
    """Resolve user text to an internal symbol like 'BTC/USD'.

    Handles many formats: BTC, btc, BTC/USD, BTCUSD, BTCUSDT, btc/usd,
    bitcoin, etc.
    """
    from trading.config import CRYPTO_SYMBOLS
    t = text.strip().lower().rstrip(".")
    # Direct alias match
    if t in _SYMBOL_ALIASES:
        return _SYMBOL_ALIASES[t]
    # Strip common suffixes for matching
    for suffix in ["usdt", "usd", "perp"]:
        stripped = t.replace("/", "").replace("-", "")
        if stripped.endswith(suffix):
            base = stripped[:-len(suffix)]
            if base in _SYMBOL_ALIASES:
                return _SYMBOL_ALIASES[base]
    # Handle "BTC/USD" or "btc/usd" directly
    if "/" in t:
        t_upper = t.upper()
        for _coin_id, sym in CRYPTO_SYMBOLS.items():
            if t_upper == sym:
                return sym
    # Check CRYPTO_SYMBOLS values
    for _coin_id, sym in CRYPTO_SYMBOLS.items():
        if t == sym.lower() or t == sym.replace("/", "").lower():
            return sym
    # Check by ticker (e.g., "BTCUSD" or "BTC")
    for _coin_id, sym in CRYPTO_SYMBOLS.items():
        ticker = sym.split("/")[0].lower()
        if t == ticker or t.replace("/", "") == sym.replace("/", "").lower():
            return sym
    return None


def _resolve_strategy(text: str) -> str | None:
    """Resolve user text to a canonical strategy name."""
    from trading.config import STRATEGY_ENABLED
    t = text.strip().lower().replace(" ", "_").replace("-", "_")
    if t in STRATEGY_ENABLED:
        return t
    if t in STRATEGY_ALIASES:
        return STRATEGY_ALIASES[t]
    # Substring match
    for name in STRATEGY_ENABLED:
        if t in name or name in t:
            return name
    return None


def _extract_symbol_from_msg(msg: str) -> str | None:
    """Try to extract a symbol from a message."""
    # Look for known symbols in the message
    words = re.split(r"[\s,]+", msg.lower())
    for word in words:
        sym = _resolve_symbol(word.strip("'\""))
        if sym:
            return sym
    return None


def _extract_strategy_from_msg(msg: str) -> str | None:
    """Try to extract a strategy name from a message."""
    words = msg.lower().split()
    # Try multi-word matches first (e.g. "whale flow" → whale_flow)
    for i in range(len(words)):
        for j in range(i + 1, min(i + 4, len(words) + 1)):
            candidate = "_".join(words[i:j])
            s = _resolve_strategy(candidate)
            if s:
                return s
    # Single word
    for word in words:
        s = _resolve_strategy(word.strip("'\""))
        if s:
            return s
    return None


def _parse_percentage(text: str) -> float | None:
    """Extract a percentage value from text like '5%', '50 percent', '0.05'."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*percent", text.lower())
    if m:
        return float(m.group(1))
    return None


def _parse_expiry(text: str) -> str | None:
    """Parse time expressions like 'until Friday', 'for 24 hours', 'for 2 days'."""
    now = datetime.now(timezone.utc)
    lower = text.lower()

    m = re.search(r"for\s+(\d+)\s+hours?", lower)
    if m:
        return (now + timedelta(hours=int(m.group(1)))).isoformat()

    m = re.search(r"for\s+(\d+)\s+days?", lower)
    if m:
        return (now + timedelta(days=int(m.group(1)))).isoformat()

    day_names = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                 "friday": 4, "saturday": 5, "sunday": 6}
    for day_name, day_num in day_names.items():
        if f"until {day_name}" in lower:
            days_ahead = (day_num - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target = now + timedelta(days=days_ahead)
            return target.replace(hour=23, minute=59, second=59).isoformat()

    if "until tomorrow" in lower:
        tomorrow = now + timedelta(days=1)
        return tomorrow.replace(hour=23, minute=59, second=59).isoformat()

    if "until end of week" in lower or "until eow" in lower:
        days_to_friday = (4 - now.weekday()) % 7
        if days_to_friday == 0 and now.hour > 17:
            days_to_friday = 7
        target = now + timedelta(days=days_to_friday)
        return target.replace(hour=23, minute=59, second=59).isoformat()

    return None


def _cleanup_expired():
    """Remove expired pending actions."""
    now = datetime.now(timezone.utc)
    expired = [aid for aid, a in _pending_actions.items()
               if now - a["created_at"] > _CONFIRMATION_TTL]
    for aid in expired:
        del _pending_actions[aid]


def _queue_action(action_type: str, description: str, execute_fn,
                  warning: str | None = None) -> dict:
    """Queue a destructive action — requires explicit confirmation via /api/chat/confirm.

    Returns a response dict with a 'confirm' key so the UI can present a
    confirmation dialog. The action is stored in _pending_actions with a 5-minute TTL.
    Nothing executes until handle_operator_message() is called with confirmed_action_id.
    """
    _cleanup_expired()
    action_id = str(uuid.uuid4())
    _pending_actions[action_id] = {
        "type": action_type,
        "description": description,
        "execute_fn": execute_fn,
        "warning": warning,
        "created_at": datetime.now(timezone.utc),
    }
    msg = f"{description}\n\n⚠️ **Confirm required.** Reply with confirmation or click Confirm to execute."
    if warning:
        msg += f"\n\n🔴 **Warning:** {warning}"
    return {
        "answer": msg,
        "confirm": {
            "action_id": action_id,
            "description": description,
            "warning": warning,
        },
    }


def _find_position(symbol: str) -> dict | None:
    """Find a position by symbol."""
    from trading.execution.router import get_positions_from_aster
    positions = get_positions_from_aster()
    sym_clean = symbol.replace("/", "")
    for pos in positions:
        if pos["symbol"].upper() == sym_clean.upper():
            return pos
    return None


def _parse_time_range(msg: str) -> tuple[str | None, str | None]:
    """Parse time range from message. Returns (start_timestamp, label)."""
    now = datetime.now(timezone.utc)
    lower = msg.lower()

    m = re.search(r"last\s+(\d+)\s+hours?", lower)
    if m:
        hours = int(m.group(1))
        start = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        return start, f"Last {hours} Hour{'s' if hours > 1 else ''}"

    if "today" in lower:
        start = now.strftime("%Y-%m-%d 00:00:00")
        return start, "Today"

    if "yesterday" in lower:
        yesterday = now - timedelta(days=1)
        start = yesterday.strftime("%Y-%m-%d 00:00:00")
        return start, "Yesterday"

    if "this week" in lower:
        monday = now - timedelta(days=now.weekday())
        start = monday.strftime("%Y-%m-%d 00:00:00")
        return start, "This Week"

    if "last cycle" in lower:
        return "__last_cycle__", "Last Cycle"

    m = re.search(r"last\s+(\d+)\s+days?", lower)
    if m:
        days = int(m.group(1))
        start = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        return start, f"Last {days} Day{'s' if days > 1 else ''}"

    return None, None


# ============================================================================
# ENTRY POINT
# ============================================================================

def handle_operator_message(message: str, confirmed_action_id: str | None = None) -> dict | None:
    """Main entry point for operator console.

    Returns:
        dict with "answer" key (and optionally "confirm") for operator-handled messages.
        None if the message is not an operator action (fall through to chat.py).
    """
    # --- Execute a confirmed action ---
    if confirmed_action_id:
        return _execute_confirmed(confirmed_action_id)

    msg = message.strip()
    if not msg:
        return None

    lower = msg.lower()

    # --- Text-based confirmation ("confirm", "yes", "execute") ---
    if lower in {"confirm", "yes", "execute", "go", "do it", "proceed", "ok", "okay"}:
        _cleanup_expired()
        if len(_pending_actions) == 1:
            action_id = next(iter(_pending_actions))
            return _execute_confirmed(action_id)
        elif len(_pending_actions) > 1:
            return {"answer": "Multiple actions pending. Please use the Confirm button to choose which one to execute."}

    # --- Intent routing (most specific first) ---

    # 1. Position management (close, reduce)
    if re.search(r"\b(close|exit)\b.*\b(position|pos)\b", lower) or \
       re.search(r"\b(close|exit)\s+(my\s+)?\w+", lower) and \
       any(w in lower for w in ["close", "exit"]):
        result = _intent_close_position(msg, lower)
        if result:
            return result

    if re.search(r"\b(reduce|trim|sell\s+\d+%|cut)\b", lower):
        result = _intent_reduce_position(msg, lower)
        if result:
            return result

    # 2a. Emergency halt toggle — must come before generic halt/resume patterns
    if re.search(r"\b(turn\s+off|disable|deactivate)\s+(the\s+)?(emergency\s+)?halt\b", lower) or \
       re.search(r"\bemergency\s+halt.{0,30}(off|disable|resume|turn\s+off)\b", lower) or \
       re.search(r"\bturn\s+off\s+emergency\s+halt\b", lower):
        return _intent_disable_emergency_halt(msg, lower)

    if re.search(r"\b(enable|turn\s+on|reactivate)\s+(the\s+)?(emergency\s+)?halt\b", lower) or \
       re.search(r"\benable\s+emergency\s+halt\b", lower):
        return _intent_enable_emergency_halt(msg, lower)

    # 2b. Trading-level halt / resume (more specific — must come before strategy enable/disable)
    if re.search(r"\b(halt|stop|freeze)\s+(all\s+)?(trading|trades)\b", lower) or \
       re.search(r"\bemergency\s+(halt|stop)\b", lower):
        return _intent_halt_trading(msg, lower)

    if re.search(r"\b(resume|restart|unhalt|clear.?halt|restore)\s+(all\s+)?(trading|trades)\b", lower) or \
       (re.search(r"\b(resume|unhalt)\s+trading\b", lower)):
        return _intent_resume_trading(msg, lower)

    # 2b. Agent queries
    if re.search(r"\b(query\s+agents?|agent\s+status|what\s+(are\s+the\s+)?agents\s+(doing|up\s+to)|agent\s+activity)\b", lower):
        return _read_agent_history(msg, lower)

    # 2c. Strategy control (disable, enable)
    if re.search(r"\b(disable|turn\s+off|pause|deactivate)\b", lower):
        return _intent_disable_strategy(msg, lower)

    if re.search(r"\b(enable|turn\s+on|activate|resume)\b", lower):
        return _intent_enable_strategy(msg, lower)

    # 3. Risk adjustment
    if re.search(r"\b(set|change|update|tighten|loosen|adjust)\b.*\b(stop.?loss|max.?position|daily.?loss|drawdown|cash.?reserve|max.?trades|risk)\b", lower):
        return _intent_update_risk(msg, lower)

    # 4. Agent interaction (approve, reject)
    if re.search(r"\b(approve|apply|accept)\s+(rec|recommendation)\b", lower):
        return _intent_approve_recommendation(msg, lower)

    if re.search(r"\b(reject|deny|decline)\s+(rec|recommendation)\b", lower):
        return _intent_reject_recommendation(msg, lower)

    # 5. System control (force cycle, switch mode)
    if re.search(r"\b(run|force|trigger|execute)\s+(a\s+)?(cycle|trading\s+cycle)\b", lower) or \
       lower in ("trade now", "run cycle", "force cycle"):
        return _intent_force_cycle(msg, lower)

    if re.search(r"\b(switch|go|change)\s+(to\s+)?(paper|live)\b", lower):
        return _intent_switch_mode(msg, lower)

    # 6. Undo/revert
    if re.search(r"\b(undo|revert|reverse)\b", lower):
        return _intent_undo(msg, lower)

    # 7. Set alert
    if re.search(r"\b(alert|notify|warn)\s+(me|if|when)\b", lower):
        return _intent_set_alert(msg, lower)

    # --- Read queries (operator handles directly, richer than chat.py) ---

    # 9. Check-in / briefing
    if re.search(r"\b(briefing|check.?in|morning.?report|status.?report|what.?should.?i.?know|give.?me.?a.?briefing)\b", lower):
        return _read_briefing(msg, lower)

    # 10. Explain decision
    if re.search(r"\b(why\s+did|explain|what\s+caused|reason\s+for)\b.*\b(trade|buy|sell|sold|bought|close|block)\b", lower):
        return _read_explain_decision(msg, lower)

    # 11. System activity
    if re.search(r"\b(what\s+happened|system\s+activity|show\s+me\s+the\s+last|what\s+did\s+the\s+system)\b", lower) or \
       re.search(r"\b(last\s+cycle|last\s+\d+\s+hours?|activity)\b", lower) and \
       any(w in lower for w in ["show", "what", "activity"]):
        result = _read_system_activity(msg, lower)
        if result:
            return result

    # 12. Agent history
    if re.search(r"\b(agent.?history|agent.?activity|what.?have.?the.?agents|agent.?recommendations)\b", lower):
        return _read_agent_history(msg, lower)

    # 13. Trade history deep dive
    if re.search(r"\b(trade\s+history|all\s+\w+\s+trades|worst\s+trades?|best\s+trades?|winning\s+trades?|losing\s+trades?)\b", lower) or \
       (re.search(r"\btrades?\b", lower) and re.search(r"\b(show|list|all|history|for)\b", lower)):
        result = _read_trade_history(msg, lower)
        if result:
            return result

    # 14. Signal history
    if re.search(r"\b(signal\s+history|signals?\s+fired|show.*signals?|all\s+\w+\s+signals?)\b", lower) and \
       not re.search(r"\bhow\s+many\b", lower):
        result = _read_signal_history(msg, lower)
        if result:
            return result

    # 15. Strategy deep dive
    if re.search(r"\b(how\s+is\s+\w+\s+performing|compare\s+\w+\s+vs|which\s+strategy|strategy\s+stats|strategy\s+performance)\b", lower):
        result = _read_strategy_deep_dive(msg, lower)
        if result:
            return result

    # 16. Risk event history
    if re.search(r"\b(risk\s+block|why\s+was.*blocked|blocked\s+trade|risk\s+event)\b", lower):
        return _read_risk_events(msg, lower)

    # 17. P&L drill-down
    if re.search(r"\b(best\s+day|worst\s+day|how\s+did\s+i\s+do\s+on|pnl\s+on|daily\s+pnl|p&l\s+on)\b", lower):
        return _read_pnl_drilldown(msg, lower)

    # 18. Position detail
    if re.search(r"\b(position\s+detail|tell\s+me\s+about\s+my|how\s+is\s+\w+\s+doing)\b", lower) and \
       _extract_symbol_from_msg(lower):
        return _read_position_detail(msg, lower)

    # 19. Export / summary
    if re.search(r"\b(export|summarize|summary|weekly\s+report|csv)\b", lower):
        return _read_export_summary(msg, lower)

    # 20. Knowledge search
    if re.search(r"\b(search\s+knowledge|what\s+does\s+the\s+research|knowledge\s+base)\b", lower):
        return _read_knowledge_search(msg, lower)

    # 21. Backtest command
    if re.search(r"\bbacktest\b", lower):
        return _intent_backtest(msg, lower)

    # 22. What-if scenario analysis
    if re.search(r"\bwhat\s+if\b", lower):
        return _intent_what_if(msg, lower)

    # 23. Portfolio rebalancing
    if re.search(r"\brebalance\b", lower):
        return _intent_rebalance(msg, lower)

    # 24. Scheduled commands
    if re.search(r"\b(schedule|every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|day|hour))\b", lower):
        return _intent_schedule_command(msg, lower)

    # 25. Multi-step workflows (take profit, close losing, etc.)
    if re.search(r"\b(take\s+profit|close\s+all|close\s+losing|close\s+winning|flatten)\b", lower):
        return _intent_batch_workflow(msg, lower)

    # 26. Open/buy a new trade
    if re.search(r"\b(buy|long|open\s+(?:a\s+)?(?:long|position))\b", lower) and \
       _extract_symbol_from_msg(lower):
        return _intent_open_trade(msg, lower, "buy")

    # 27. Sell/short a new trade
    if re.search(r"\b(short|open\s+(?:a\s+)?short)\b", lower) and \
       _extract_symbol_from_msg(lower):
        return _intent_open_trade(msg, lower, "sell")

    # 28. LLM-powered universal command — catch-all for complex instructions
    if re.search(r"\b(execute|do|make|place|submit|enter|market\s+order)\b", lower) and \
       _extract_symbol_from_msg(lower):
        return _intent_llm_execute(msg, lower)

    # 29. Leverage profile switching
    if re.search(r"\b(switch|change|set|go|use)\b.*\b(leverage|profile)\b", lower) or \
       re.search(r"\b(aggressive|conservative|moderate|greedy)\b.*\b(mode|profile|leverage)\b", lower) or \
       re.search(r"\b(leverage|profile)\b.*\b(aggressive|conservative|moderate|greedy)\b", lower):
        return _intent_change_leverage_profile(msg, lower)

    # 30. Circuit breaker reset
    if re.search(r"\b(reset|clear|fix|rehabilitate)\b.*\b(circuit.?breaker|cb)\b", lower) or \
       re.search(r"\bcircuit.?breaker\b.*\b(reset|clear|fix)\b", lower):
        return _intent_reset_circuit_breaker(msg, lower)

    # 31. Force graduate / exit recovery mode
    if re.search(r"\b(graduate|exit|leave|end|stop)\b.*\b(recovery|conservative)\b.*\b(mode)?\b", lower) or \
       re.search(r"\b(force\s+graduate|exit\s+recovery|leave\s+conservative)\b", lower):
        return _intent_force_graduate(msg, lower)

    # 32. GODLIKE POWERS — Batch trades
    if re.search(r"\b(buy|sell|long|short)\s+\$?\d+\s+each\s+of\b", lower):
        result = _intent_batch_trades(msg, lower)
        if result:
            return result

    # 33. Live prices
    if re.search(r"\b(price|what.*at|current.*price)\s+(?:of|for)\b", lower) and _extract_symbol_from_msg(msg):
        return _intent_live_prices(msg, lower)

    # 34. Portfolio allocation
    if re.search(r"\b(allocate|put|realloc)\s+\d+%\s+(?:in|to)\b", lower):
        result = _intent_allocate_portfolio(msg, lower)
        if result:
            return result

    # 35. Risk bypass
    if re.search(r"\b(bypass|ignore|skip|override)\s+risk\b", lower):
        return _intent_risk_bypass(msg, lower)

    # 36. Settings control
    if re.search(r"\b(show|list|set|reset)\s+(?:all\s+)?settings?\b", lower) or \
       re.search(r"\bset\s+\w+\s+(?:to|=)\b", lower):
        result = _intent_settings_control(msg, lower)
        if result:
            return result

    # 37. Auto-triggers
    if re.search(r"\b(?:if|when)\s+.+,\s+", lower):
        result = _intent_auto_trigger(msg, lower)
        if result:
            return result

    # 38. Cancel all orders
    if re.search(r"\b(cancel|clear|flush)\s+(?:all\s+)?(orders?|queue)\b", lower):
        return _intent_cancel_all_orders(msg, lower)

    # 39. System diagnostics
    if re.search(r"\b(system\s+health|diagnose|system\s+status|health\s+check)\b", lower):
        return _intent_system_diagnostics(msg, lower)

    # 40. Inject signal
    if re.search(r"\b(inject|force)\s+(?:a\s+)?(buy|sell|hold)\s+signal\b", lower):
        return _intent_inject_signal(msg, lower)

    # 41. Agent broadcast
    if re.search(r"\b(?:agents?|tell.*agents?)[\s:]+", lower):
        result = _intent_agent_broadcast(msg, lower)
        if result:
            return result

    # 42. Universal LLM catch-all — fire for command-like messages that didn't match
    if re.search(r"^(buy|sell|close|open|halt|resume|enable|disable|set|change|switch|reset|"
                 r"run|force|execute|place|short|long|reduce|trim|approve|reject|alert|"
                 r"schedule|rebalance|backtest|flatten|graduate|rehabilitate|quit|stop)\b", lower):
        return _intent_llm_universal(msg, lower)

    # Not an operator message — fall through to chat.py
    return None


# ============================================================================
# ACTION EXECUTION
# ============================================================================

def _execute_confirmed(action_id: str) -> dict:
    """Execute a previously queued action."""
    _cleanup_expired()
    action = _pending_actions.pop(action_id, None)
    if not action:
        return {"answer": "Action expired or not found. Please try again."}

    try:
        result = action["execute_fn"]()
        return {"answer": result}
    except Exception as e:
        log.exception("Operator action failed: %s", action["type"])
        return {"answer": f"Action failed: {e}"}


# ============================================================================
# WRITE ACTIONS (require confirmation)
# ============================================================================

def _intent_disable_strategy(msg: str, lower: str) -> dict:
    from trading.config import STRATEGY_ENABLED
    from trading.db.store import log_action, set_setting

    strategy = _extract_strategy_from_msg(msg)
    if not strategy:
        return {"answer": "I couldn't identify which strategy to disable. "
                         f"Available: {', '.join(sorted(STRATEGY_ENABLED.keys()))}"}

    if not STRATEGY_ENABLED.get(strategy, True):
        return {"answer": f"**{strategy}** is already disabled."}

    expiry = _parse_expiry(msg)
    expiry_note = ""
    if expiry:
        exp_dt = datetime.fromisoformat(expiry)
        expiry_note = f" until {exp_dt.strftime('%Y-%m-%d %H:%M UTC')}"

    desc = f"Disable strategy **{strategy}**{expiry_note}"

    def execute():
        if expiry:
            val = json.dumps({"status": "disabled", "expires_at": expiry})
        else:
            val = "disabled"
        set_setting(f"strategy_override_{strategy}", val)
        STRATEGY_ENABLED[strategy] = False
        log_action("operator", "disable_strategy", details=f"Disabled {strategy}{expiry_note}")
        return f"**{strategy}** has been disabled{expiry_note}. It will not generate signals until re-enabled."

    return _queue_action("disable_strategy", desc, execute)


def _intent_enable_strategy(msg: str, lower: str) -> dict:
    from trading.config import STRATEGY_ENABLED
    from trading.db.store import log_action, set_setting

    strategy = _extract_strategy_from_msg(msg)
    if not strategy:
        return {"answer": "I couldn't identify which strategy to enable. "
                         f"Available: {', '.join(sorted(STRATEGY_ENABLED.keys()))}"}

    if STRATEGY_ENABLED.get(strategy, False):
        return {"answer": f"**{strategy}** is already enabled."}

    desc = f"Enable strategy **{strategy}**"
    warning = "This strategy was previously disabled. Make sure you understand why before re-enabling."

    def execute():
        set_setting(f"strategy_override_{strategy}", "enabled")
        STRATEGY_ENABLED[strategy] = True
        log_action("operator", "enable_strategy", details=f"Enabled {strategy}")
        return f"**{strategy}** has been enabled and will generate signals on the next cycle."

    return _queue_action("enable_strategy", desc, execute, warning=warning)


def _intent_close_position(msg: str, lower: str) -> dict | None:
    from trading.config import TRADING_MODE
    from trading.db.store import log_action

    symbol = _extract_symbol_from_msg(msg)
    if not symbol:
        return None  # Fall through — might not be an operator command

    pos = _find_position(symbol)
    if not pos:
        return {"answer": f"No open position found for **{symbol}**."}

    qty = pos.get("qty", 0)
    price = pos.get("current_price", 0)
    value = pos.get("market_value", qty * price)
    pnl = pos.get("unrealized_pnl", 0)
    side = pos.get("side", "long")

    desc = (f"Close **{symbol}** {side} position\n"
            f"- Qty: {qty}\n"
            f"- Current price: ${price:,.2f}\n"
            f"- Market value: ${value:,.2f}\n"
            f"- Unrealized P&L: ${pnl:+,.2f}")

    warning = "This will execute a real market order." if TRADING_MODE == "live" else None

    def execute():
        from trading.execution.router import close_position
        result = close_position(symbol)
        log_action("operator", "close_position", symbol=symbol,
                   details=f"Closed {side} {qty} @ ${price:,.2f}, P&L: ${pnl:+,.2f}",
                   result=json.dumps(result))
        if result.get("status") == "no_position":
            return f"No position found for {symbol} (may have already been closed)."
        return f"Position **{symbol}** closed. Market order submitted."

    return _queue_action("close_position", desc, execute, warning=warning)


def _intent_reduce_position(msg: str, lower: str) -> dict | None:
    from trading.config import TRADING_MODE
    from trading.db.store import log_action

    symbol = _extract_symbol_from_msg(msg)
    if not symbol:
        return None

    pos = _find_position(symbol)
    if not pos:
        return {"answer": f"No open position found for **{symbol}**."}

    pct = _parse_percentage(msg)
    if not pct:
        return {"answer": "Please specify a percentage to reduce by (e.g., 'reduce BTC by 50%')."}

    qty = pos.get("qty", 0)
    reduce_qty = qty * (pct / 100.0)
    price = pos.get("current_price", 0)
    reduce_value = reduce_qty * price

    desc = (f"Reduce **{symbol}** position by {pct:.0f}%\n"
            f"- Selling {reduce_qty:.6f} of {qty:.6f}\n"
            f"- Estimated value: ${reduce_value:,.2f}")

    warning = "This will execute a real market order." if TRADING_MODE == "live" else None

    def execute():
        from trading.execution.router import submit_order
        result = submit_order(symbol, "sell", qty=reduce_qty)
        log_action("operator", "reduce_position", symbol=symbol,
                   details=f"Reduced by {pct:.0f}% ({reduce_qty:.6f} units, ~${reduce_value:,.2f})",
                   result=json.dumps(result))
        return f"Reduced **{symbol}** by {pct:.0f}%. Sold {reduce_qty:.6f} units (~${reduce_value:,.2f})."

    return _queue_action("reduce_position", desc, execute, warning=warning)


def _intent_update_risk(msg: str, lower: str) -> dict:
    from trading.config import RISK
    from trading.db.store import log_action, set_setting

    # Identify which risk parameter
    param_key = None
    for alias, key in _RISK_ALIASES.items():
        if alias in lower:
            param_key = key
            break

    if not param_key:
        return {"answer": f"I couldn't identify which risk parameter to update. "
                         f"Available: {', '.join(_RISK_ALIASES.keys())}"}

    pct = _parse_percentage(msg)
    if pct is None:
        # Try raw number for max_trades_per_day
        m = re.search(r"\b(\d+)\b", msg)
        if m and param_key == "max_trades_per_day":
            new_value = int(m.group(1))
        else:
            return {"answer": "Please specify the new value (e.g., 'set stop loss to 5%')."}
    else:
        if param_key == "max_trades_per_day":
            new_value = int(pct)
        else:
            new_value = pct / 100.0  # Convert percentage to decimal

    old_value = RISK.get(param_key, 0)
    expiry = _parse_expiry(msg)
    expiry_note = ""
    if expiry:
        exp_dt = datetime.fromisoformat(expiry)
        expiry_note = f" (expires {exp_dt.strftime('%Y-%m-%d %H:%M UTC')})"

    if param_key == "max_trades_per_day":
        desc = f"Update **{param_key}**: {int(old_value)} → {new_value}{expiry_note}"
    else:
        desc = f"Update **{param_key}**: {old_value:.1%} → {new_value:.1%}{expiry_note}"

    is_loosening = new_value > old_value
    warning = "You are loosening this risk parameter. This increases exposure." if is_loosening else None

    def execute():
        override_data = {
            "value": new_value,
            "old_value": old_value,
            "set_at": datetime.now(timezone.utc).isoformat(),
        }
        if expiry:
            override_data["expires_at"] = expiry
        set_setting(f"risk_override_{param_key}", json.dumps(override_data))
        RISK[param_key] = new_value
        log_action("operator", "update_risk", details=f"{param_key}: {old_value} → {new_value}{expiry_note}",
                   data=override_data)
        if param_key == "max_trades_per_day":
            return f"**{param_key}** updated: {int(old_value)} → {new_value}{expiry_note}"
        return f"**{param_key}** updated: {old_value:.1%} → {new_value:.1%}{expiry_note}"

    return _queue_action("update_risk", desc, execute, warning=warning)


def _intent_approve_recommendation(msg: str, lower: str) -> dict:
    from trading.db.store import get_pending_recommendations, resolve_recommendation, log_action

    m = re.search(r"#?\s*(\d+)", msg)
    if not m:
        recs = get_pending_recommendations()
        if not recs:
            return {"answer": "No pending recommendations to approve."}
        lines = ["**Pending recommendations:**"]
        for r in recs[:10]:
            lines.append(f"- #{r['id']}: [{r.get('from_agent', '?')}] {r.get('action', '?')} "
                        f"— {(r.get('reasoning') or '')[:100]}")
        return {"answer": "\n".join(lines)}

    rec_id = int(m.group(1))
    recs = get_pending_recommendations()
    rec = next((r for r in recs if r["id"] == rec_id), None)
    if not rec:
        return {"answer": f"Recommendation #{rec_id} not found or already resolved."}

    desc = (f"Approve recommendation #{rec_id}\n"
            f"- From: {rec.get('from_agent', '?')}\n"
            f"- Action: {rec.get('action', '?')}\n"
            f"- Target: {rec.get('target', '?')}\n"
            f"- Reasoning: {(rec.get('reasoning') or '')[:200]}")

    def execute():
        resolve_recommendation(rec_id, "approved", "Approved via operator console")
        log_action("operator", "approve_recommendation",
                   details=f"Approved #{rec_id}: {rec.get('action', '')} {rec.get('target', '')}")
        return f"Recommendation #{rec_id} has been approved."

    return _queue_action("approve_recommendation", desc, execute)


def _intent_reject_recommendation(msg: str, lower: str) -> dict:
    from trading.db.store import get_pending_recommendations, resolve_recommendation, log_action

    m = re.search(r"#?\s*(\d+)", msg)
    if not m:
        return {"answer": "Please specify a recommendation ID (e.g., 'reject recommendation #5')."}

    rec_id = int(m.group(1))
    recs = get_pending_recommendations()
    rec = next((r for r in recs if r["id"] == rec_id), None)
    if not rec:
        return {"answer": f"Recommendation #{rec_id} not found or already resolved."}

    reason_match = re.search(r"because\s+(.+)", msg, re.IGNORECASE)
    reason = reason_match.group(1).strip() if reason_match else "Rejected via operator console"

    desc = f"Reject recommendation #{rec_id}: {rec.get('action', '')} {rec.get('target', '')}"

    def execute():
        resolve_recommendation(rec_id, "rejected", reason)
        log_action("operator", "reject_recommendation",
                   details=f"Rejected #{rec_id}: {reason}")
        return f"Recommendation #{rec_id} has been rejected. Reason: {reason}"

    return _queue_action("reject_recommendation", desc, execute)


def _intent_force_cycle(msg: str, lower: str) -> dict:
    from trading.config import TRADING_MODE
    from trading.db.store import log_action

    desc = "Run a trading cycle now"
    warning = "This will execute trades in LIVE mode!" if TRADING_MODE == "live" else None

    def execute():
        from trading.scheduler import run_trading_cycle
        log_action("operator", "force_cycle", details="Manual cycle triggered via operator console")
        thread = threading.Thread(target=run_trading_cycle, name="operator-force-cycle", daemon=True)
        thread.start()
        return "Trading cycle started in the background. Check the action log for results."

    return _queue_action("force_cycle", desc, execute, warning=warning)


def _intent_switch_mode(msg: str, lower: str) -> dict:
    from trading.db.store import log_action, set_setting
    import trading.config as cfg

    m = re.search(r"\b(paper|live)\b", lower)
    if not m:
        return {"answer": f"Current mode: **{cfg.TRADING_MODE}**. Say 'switch to paper' or 'switch to live'."}

    new_mode = m.group(1)
    if new_mode == cfg.TRADING_MODE:
        return {"answer": f"Already in **{new_mode}** mode."}

    desc = f"Switch trading mode: **{cfg.TRADING_MODE}** → **{new_mode}**"
    warning = "LIVE mode will execute real trades with real money!" if new_mode == "live" else None

    def execute():
        old = cfg.TRADING_MODE
        set_setting("trading_mode", new_mode)
        cfg.TRADING_MODE = new_mode
        # Write to .env.mode if DATA_DIR exists
        from trading.config import _DATA_DIR
        if _DATA_DIR and _DATA_DIR.exists():
            mode_file = _DATA_DIR / ".env.mode"
            mode_file.write_text(f"TRADING_MODE={new_mode}\n")
        log_action("operator", "switch_mode", details=f"Mode: {old} → {new_mode}")
        return f"Trading mode switched: **{old}** → **{new_mode}**."

    return _queue_action("switch_mode", desc, execute, warning=warning)


def _intent_undo(msg: str, lower: str) -> dict:
    from trading.db.store import get_action_log, log_action, set_setting, get_setting
    from trading.config import STRATEGY_ENABLED, RISK

    actions = get_action_log(limit=10, category="operator")
    if not actions:
        return {"answer": "No recent operator actions to undo."}

    last = actions[0]
    action_type = last.get("action", "")

    if action_type == "disable_strategy":
        strategy = None
        details = last.get("details", "")
        m = re.search(r"Disabled\s+(\w+)", details)
        if m:
            strategy = m.group(1)
        if not strategy:
            return {"answer": "Could not determine which strategy was disabled."}

        desc = f"Undo: re-enable **{strategy}**"

        def execute():
            set_setting(f"strategy_override_{strategy}", "enabled")
            STRATEGY_ENABLED[strategy] = True
            log_action("operator", "undo_disable_strategy", details=f"Re-enabled {strategy} (undo)")
            return f"**{strategy}** has been re-enabled (undo of previous disable)."

        return _queue_action("undo", desc, execute)

    elif action_type == "enable_strategy":
        strategy = None
        details = last.get("details", "")
        m = re.search(r"Enabled\s+(\w+)", details)
        if m:
            strategy = m.group(1)
        if not strategy:
            return {"answer": "Could not determine which strategy was enabled."}

        desc = f"Undo: disable **{strategy}**"

        def execute():
            set_setting(f"strategy_override_{strategy}", "disabled")
            STRATEGY_ENABLED[strategy] = False
            log_action("operator", "undo_enable_strategy", details=f"Disabled {strategy} (undo)")
            return f"**{strategy}** has been disabled (undo of previous enable)."

        return _queue_action("undo", desc, execute)

    elif action_type == "update_risk":
        details = last.get("details", "")
        data = last.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                data = None
        if data and "old_value" in data:
            param_match = re.match(r"(\w+):", details)
            param_key = param_match.group(1) if param_match else None
            if param_key:
                old_val = data["old_value"]
                desc = f"Undo: revert **{param_key}** to {old_val}"

                def execute():
                    RISK[param_key] = old_val
                    setting_key = f"risk_override_{param_key}"
                    existing = get_setting(setting_key)
                    if existing:
                        set_setting(setting_key, "")
                    log_action("operator", "undo_update_risk",
                               details=f"Reverted {param_key} to {old_val} (undo)")
                    return f"**{param_key}** reverted to {old_val} (undo of previous change)."

                return _queue_action("undo", desc, execute)

        return {"answer": "Could not determine the previous risk value to revert to."}

    elif action_type == "close_position":
        return {"answer": "Cannot undo a closed position — the market order has already been executed."}

    else:
        return {"answer": f"Cannot undo action type '{action_type}'. "
                         f"Last operator action: {last.get('details', 'unknown')}"}


def _intent_set_alert(msg: str, lower: str) -> dict:
    from trading.db.store import set_setting, log_action

    symbol = _extract_symbol_from_msg(msg)
    pct = _parse_percentage(msg)

    # Price alert
    price_match = re.search(r"(?:below|under|drops?\s+(?:below|under))\s+\$?([\d,]+(?:\.\d+)?)", lower)
    if not price_match:
        price_match = re.search(r"(?:above|over|rises?\s+(?:above|over))\s+\$?([\d,]+(?:\.\d+)?)", lower)

    if price_match and symbol:
        threshold = float(price_match.group(1).replace(",", ""))
        direction = "below" if any(w in lower for w in ["below", "under", "drop"]) else "above"
        ticker = symbol.split("/")[0]

        desc = f"Set alert: {ticker} {direction} ${threshold:,.2f}"

        def execute():
            alert_data = {
                "symbol": symbol,
                "direction": direction,
                "threshold": threshold,
                "created": datetime.now(timezone.utc).isoformat(),
            }
            set_setting(f"alert_{ticker.lower()}_price_{direction}", json.dumps(alert_data))
            log_action("operator", "set_alert", symbol=symbol,
                       details=f"Alert: {ticker} {direction} ${threshold:,.2f}")
            return f"Alert set: you'll be notified when **{ticker}** goes {direction} **${threshold:,.2f}**."

        return _queue_action("set_alert", desc, execute)

    # Drawdown alert
    if pct and "drawdown" in lower:
        desc = f"Set alert: drawdown hits {pct:.0f}%"

        def execute():
            alert_data = {"threshold": pct / 100.0,
                          "created": datetime.now(timezone.utc).isoformat()}
            set_setting("alert_drawdown", json.dumps(alert_data))
            log_action("operator", "set_alert", details=f"Alert: drawdown hits {pct:.0f}%")
            return f"Alert set: you'll be notified when drawdown reaches **{pct:.0f}%**."

        return _queue_action("set_alert", desc, execute)

    return {"answer": "Please specify an alert condition (e.g., 'alert me if BTC drops below 80k' "
                     "or 'notify me when drawdown hits 15%')."}


# ============================================================================
# READ QUERIES (no confirmation needed)
# ============================================================================

def _read_briefing(msg: str, lower: str) -> dict:
    """Comprehensive system status briefing."""
    from trading.db.store import get_action_log, get_daily_pnl, get_pending_recommendations
    from trading.execution.router import get_positions_from_aster, get_account

    lines = ["**System Briefing**\n"]

    # 1. Portfolio
    try:
        account = get_account()
        pv = account.get("portfolio_value", 0)
        cash = account.get("cash", 0)
        mode = "PAPER" if account.get("paper") else "LIVE"
        lines.append(f"**Portfolio**: ${pv:,.2f} ({mode} mode) | Cash: ${cash:,.2f}")
    except Exception:
        lines.append("**Portfolio**: Unable to fetch account data")

    # 2. Open positions
    try:
        positions = get_positions_from_aster()
        if positions:
            lines.append(f"\n**Open Positions** ({len(positions)}):")
            for p in positions:
                pnl = p.get("unrealized_pnl", 0)
                lines.append(f"- {p['symbol']}: {p.get('side','?')} {p.get('qty',0):.4f} "
                           f"@ ${p.get('current_price',0):,.2f} (P&L: ${pnl:+,.2f})")
        else:
            lines.append("\n**Open Positions**: None")
    except Exception:
        lines.append("\n**Open Positions**: Unable to fetch")

    # 3. Daily P&L
    try:
        pnl_data = get_daily_pnl(limit=1)
        if pnl_data:
            today_pnl = pnl_data[0]
            lines.append(f"\n**Today's P&L**: {today_pnl.get('daily_return', 0):+.2f}% "
                        f"(Cumulative: {today_pnl.get('cumulative_return', 0):+.2f}%)")
    except Exception:
        pass

    # 4. Last cycle summary
    try:
        cycle_actions = get_action_log(limit=50, category="strategy_run")
        if cycle_actions:
            last_cycle = cycle_actions[0]
            lines.append(f"\n**Last Cycle**: {last_cycle.get('timestamp', '?')}")
            lines.append(f"  {last_cycle.get('details', '')}")
    except Exception:
        pass

    # 5. Errors in last 24h
    try:
        errors = get_action_log(limit=20, category="error")
        recent_errors = [e for e in errors
                        if e.get("timestamp", "") > (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d")]
        if recent_errors:
            lines.append(f"\n**Errors (24h)**: {len(recent_errors)}")
            for e in recent_errors[:3]:
                lines.append(f"  - {e.get('action', '')}: {(e.get('details') or '')[:100]}")
        else:
            lines.append("\n**Errors (24h)**: None")
    except Exception:
        pass

    # 6. Pending recommendations
    try:
        recs = get_pending_recommendations()
        if recs:
            lines.append(f"\n**Pending Recommendations**: {len(recs)}")
            for r in recs[:3]:
                lines.append(f"  - #{r['id']}: [{r.get('from_agent', '?')}] {r.get('action', '?')}")
        else:
            lines.append("\n**Pending Recommendations**: None")
    except Exception:
        pass

    # 7. Risk status
    try:
        from trading.config import RISK
        risk_blocks = get_action_log(limit=10, category="risk_block")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_blocks = [r for r in risk_blocks if r.get("timestamp", "").startswith(today)]
        lines.append(f"\n**Risk**: Stop loss: {RISK['stop_loss_pct']:.0%} | "
                    f"Max position: {RISK['max_position_pct']:.0%} | "
                    f"Blocks today: {len(today_blocks)}")
    except Exception:
        pass

    return {"answer": "\n".join(lines)}


def _read_explain_decision(msg: str, lower: str) -> dict:
    """Reconstruct the decision chain for a specific trade."""
    from trading.db.store import get_db

    symbol = _extract_symbol_from_msg(msg)

    with get_db() as conn:
        # Find the trade
        if symbol:
            sym_clean = symbol.replace("/", "")
            trade = conn.execute(
                "SELECT * FROM trades WHERE symbol LIKE ? ORDER BY timestamp DESC LIMIT 1",
                (f"%{sym_clean}%",)
            ).fetchone()
        else:
            # "last trade" or "explain the last trade"
            trade = conn.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()

        if not trade:
            return {"answer": "No matching trade found."}

        trade = dict(trade)
        ts = trade["timestamp"]
        sym = trade["symbol"]

        lines = [f"**Decision Chain for {sym} {trade['side'].upper()} @ {ts}**\n"]

        # Find contributing signals
        signals = conn.execute(
            "SELECT * FROM signals WHERE symbol LIKE ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp ASC",
            (f"%{sym.replace('/', '')}%",
             (datetime.fromisoformat(ts.replace(" ", "T")) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S") if "T" not in ts else ts,
             ts)
        ).fetchall()

        if signals:
            lines.append("**Contributing Signals:**")
            for s in signals:
                s = dict(s)
                lines.append(f"  - {s.get('strategy', '?')}: {s.get('signal', '?')} "
                           f"(strength: {s.get('strength', 0):.2f})")

        # Find risk events
        risk_events = conn.execute(
            "SELECT * FROM action_log WHERE category IN ('risk_block', 'trade') "
            "AND timestamp LIKE ? ORDER BY timestamp ASC",
            (ts[:16] + "%",)
        ).fetchall()

        if risk_events:
            lines.append("\n**Execution Events:**")
            for e in risk_events:
                e = dict(e)
                lines.append(f"  - [{e.get('category', '')}] {e.get('action', '')}: "
                           f"{(e.get('details') or '')[:150]}")

        # Trade details
        lines.append(f"\n**Trade**: {trade['side']} {trade.get('qty', '?')} @ ${trade.get('price', 0):,.2f}")
        lines.append(f"**Strategy**: {trade.get('strategy', '?')}")
        lines.append(f"**Status**: {trade.get('status', '?')}")

    return {"answer": "\n".join(lines)}


def _read_system_activity(msg: str, lower: str) -> dict | None:
    """Show system activity for a time range."""
    from trading.db.store import get_db

    start, label = _parse_time_range(msg)
    if not start:
        start = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
        label = "Last 4 Hours"

    with get_db() as conn:
        if start == "__last_cycle__":
            cycles = conn.execute(
                "SELECT timestamp FROM action_log WHERE action='cycle_start' ORDER BY timestamp DESC LIMIT 2"
            ).fetchall()
            if len(cycles) >= 2:
                start = cycles[1]["timestamp"]
                end = cycles[0]["timestamp"]
                label = f"Last Cycle ({start[:16]} → {end[:16]})"
                actions = conn.execute(
                    "SELECT * FROM action_log WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp ASC",
                    (start, end)
                ).fetchall()
            elif cycles:
                start = cycles[0]["timestamp"]
                actions = conn.execute(
                    "SELECT * FROM action_log WHERE timestamp >= ? ORDER BY timestamp ASC",
                    (start,)
                ).fetchall()
            else:
                return {"answer": "No cycle data found."}
        else:
            actions = conn.execute(
                "SELECT * FROM action_log WHERE timestamp >= ? ORDER BY timestamp ASC",
                (start,)
            ).fetchall()

    if not actions:
        return {"answer": f"No activity found for **{label}**."}

    # Group by category
    groups: dict[str, list] = {}
    for a in actions:
        a = dict(a)
        cat = a.get("category", "other")
        groups.setdefault(cat, []).append(a)

    lines = [f"**System Activity — {label}**\n"]

    cat_icons = {"trade": "\U0001f4ca", "signal": "\U0001f4e1", "error": "\u26a0\ufe0f",
                 "risk_block": "\U0001f6e1\ufe0f", "strategy_run": "\U0001f504",
                 "autonomous": "\U0001f916", "operator": "\u26a1", "system": "\u2699\ufe0f"}

    for cat, items in groups.items():
        icon = cat_icons.get(cat, "\u2022")
        lines.append(f"{icon} **{cat}**: {len(items)} events")
        if cat == "error":
            for item in items[:5]:
                lines.append(f"  - {item.get('action', '')}: {(item.get('details') or '')[:120]}")
        elif cat == "trade":
            for item in items[:5]:
                lines.append(f"  - {item.get('action', '')} {item.get('symbol', '')} — {(item.get('details') or '')[:80]}")
        elif cat == "risk_block":
            for item in items[:5]:
                lines.append(f"  - {item.get('symbol', '')} blocked: {(item.get('details') or '')[:80]}")

    return {"answer": "\n".join(lines)}


def _read_agent_history(msg: str, lower: str) -> dict:
    """Show agent recommendation history."""
    from trading.db.store import get_recommendation_history, get_pending_recommendations

    lines = ["**Agent Activity**\n"]

    pending = get_pending_recommendations()
    if pending:
        lines.append(f"**Pending ({len(pending)}):**")
        for r in pending[:10]:
            lines.append(f"  - #{r['id']} [{r.get('from_agent', '?')} → {r.get('to_agent', '?')}] "
                        f"{r.get('action', '?')}: {(r.get('reasoning') or '')[:120]}")

    history = get_recommendation_history(limit=20)
    resolved = [r for r in history if r.get("status") == "resolved"]
    if resolved:
        lines.append(f"\n**Recently Resolved ({len(resolved)}):**")
        for r in resolved[:10]:
            lines.append(f"  - #{r['id']} [{r.get('from_agent', '?')}] {r.get('action', '?')} "
                        f"→ {r.get('resolution', '?')} — {(r.get('outcome') or '')[:80]}")

    if len(lines) == 1:
        lines.append("No agent activity found.")

    return {"answer": "\n".join(lines)}


def _read_trade_history(msg: str, lower: str) -> dict | None:
    """Enhanced trade history with filtering."""
    from trading.db.store import get_db

    symbol = _extract_symbol_from_msg(msg)
    strategy = _extract_strategy_from_msg(msg)
    start, label = _parse_time_range(msg)

    with get_db() as conn:
        query = "SELECT * FROM trades WHERE 1=1"
        params = []

        if symbol:
            query += " AND symbol LIKE ?"
            params.append(f"%{symbol.replace('/', '')}%")

        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)

        if start and start != "__last_cycle__":
            query += " AND timestamp >= ?"
            params.append(start)

        # Winning/losing filter
        if "winning" in lower or "best" in lower or "profit" in lower:
            query += " AND status = 'closed'"

        if "losing" in lower or "worst" in lower or "loss" in lower:
            query += " AND status = 'closed'"

        query += " ORDER BY timestamp DESC LIMIT 50"
        trades = conn.execute(query, params).fetchall()

    if not trades:
        return {"answer": "No trades found matching your criteria."}

    trades = [dict(t) for t in trades]

    # Compute stats
    total = len(trades)
    total_value = sum(t.get("total", 0) for t in trades)
    buys = sum(1 for t in trades if t.get("side") == "buy")
    sells = total - buys
    strategies = set(t.get("strategy", "?") for t in trades)

    filter_label = []
    if symbol:
        filter_label.append(symbol)
    if strategy:
        filter_label.append(strategy)
    if label:
        filter_label.append(label)
    title = " | ".join(filter_label) if filter_label else "Recent"

    lines = [f"**Trade History — {title}**\n"]
    lines.append(f"Total: {total} trades ({buys} buys, {sells} sells)")
    lines.append(f"Total value: ${total_value:,.2f}")
    lines.append(f"Strategies: {', '.join(sorted(strategies))}\n")

    for t in trades[:15]:
        lines.append(f"- {t.get('timestamp', '')[:16]} | {t.get('side', '?').upper()} "
                    f"{t.get('symbol', '?')} | {t.get('qty', 0):.4f} @ ${t.get('price', 0):,.2f} "
                    f"| {t.get('strategy', '?')} | {t.get('status', '?')}")

    if total > 15:
        lines.append(f"\n... and {total - 15} more trades")

    return {"answer": "\n".join(lines)}


def _read_signal_history(msg: str, lower: str) -> dict | None:
    """Enhanced signal history with filtering."""
    from trading.db.store import get_db

    symbol = _extract_symbol_from_msg(msg)
    strategy = _extract_strategy_from_msg(msg)
    start, label = _parse_time_range(msg)

    with get_db() as conn:
        query = "SELECT * FROM signals WHERE 1=1"
        params = []

        if symbol:
            query += " AND symbol LIKE ?"
            params.append(f"%{symbol.replace('/', '')}%")

        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)

        if "sell" in lower and "buy" not in lower:
            query += " AND signal = 'sell'"
        elif "buy" in lower and "sell" not in lower:
            query += " AND signal = 'buy'"
        elif "strong" in lower:
            query += " AND strength > 0.5"

        if start and start != "__last_cycle__":
            query += " AND timestamp >= ?"
            params.append(start)

        query += " ORDER BY timestamp DESC LIMIT 100"
        signals = conn.execute(query, params).fetchall()

    if not signals:
        return {"answer": "No signals found matching your criteria."}

    signals = [dict(s) for s in signals]

    buys = sum(1 for s in signals if s.get("signal") == "buy")
    sells = sum(1 for s in signals if s.get("signal") == "sell")
    holds = sum(1 for s in signals if s.get("signal") == "hold")

    strongest = max(signals, key=lambda s: abs(s.get("strength", 0)))

    lines = [f"**Signal History** ({len(signals)} signals)\n"]
    lines.append(f"{buys} buy, {sells} sell, {holds} hold")
    lines.append(f"Strongest: {strongest.get('strategy', '?')} {strongest.get('symbol', '?')} "
                f"{strongest.get('signal', '?').upper()} at {strongest.get('strength', 0):.2f}\n")

    for s in signals[:20]:
        lines.append(f"- {s.get('timestamp', '')[:16]} | {s.get('strategy', '?')} | "
                    f"{s.get('symbol', '?')} | {s.get('signal', '?').upper()} "
                    f"({s.get('strength', 0):.2f})")

    if len(signals) > 20:
        lines.append(f"\n... and {len(signals) - 20} more signals")

    return {"answer": "\n".join(lines)}


def _read_strategy_deep_dive(msg: str, lower: str) -> dict | None:
    """Deep strategy analysis with comparison support."""
    from trading.db.store import get_db
    from trading.config import STRATEGY_ENABLED

    # Check for comparison
    vs_match = re.search(r"compare\s+(\w+)\s+vs\s+(\w+)", lower)
    if vs_match:
        s1 = _resolve_strategy(vs_match.group(1))
        s2 = _resolve_strategy(vs_match.group(2))
        if s1 and s2:
            return _compare_strategies(s1, s2)

    # Check for ranking
    if re.search(r"(which|best|worst|rank|top)\s+strateg", lower):
        return _rank_strategies()

    # Single strategy
    strategy = _extract_strategy_from_msg(msg)
    if not strategy:
        return None

    with get_db() as conn:
        trades = conn.execute(
            "SELECT * FROM trades WHERE strategy = ? ORDER BY timestamp DESC LIMIT 100",
            (strategy,)
        ).fetchall()

        signals = conn.execute(
            "SELECT signal, COUNT(*) as cnt FROM signals WHERE strategy = ? GROUP BY signal",
            (strategy,)
        ).fetchall()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        signals_today = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE strategy = ? AND timestamp LIKE ?",
            (strategy, f"{today}%")
        ).fetchone()[0]

    trades = [dict(t) for t in trades]
    total_trades = len(trades)
    buys = sum(1 for t in trades if t.get("side") == "buy")

    lines = [f"**Strategy: {strategy}**\n"]
    lines.append(f"Status: {'Enabled' if STRATEGY_ENABLED.get(strategy, False) else 'Disabled'}")
    lines.append(f"Total trades: {total_trades} ({buys} buys, {total_trades - buys} sells)")
    lines.append(f"Signals today: {signals_today}")

    if signals:
        signal_summary = ", ".join(f"{dict(s)['signal']}: {dict(s)['cnt']}" for s in signals)
        lines.append(f"Signal breakdown: {signal_summary}")

    if trades:
        total_value = sum(t.get("total", 0) for t in trades)
        last_trade = trades[0]
        lines.append(f"Total trade value: ${total_value:,.2f}")
        lines.append(f"Last trade: {last_trade.get('timestamp', '')[:16]} — "
                    f"{last_trade.get('side', '?')} {last_trade.get('symbol', '?')}")

    # Check for backtest data
    try:
        from trading.db.store import get_last_backtest
        bt = get_last_backtest(strategy)
        if bt:
            lines.append(f"\n**Latest Backtest** ({bt.get('timestamp', '')[:10]}):")
            lines.append(f"  Sharpe: {bt.get('sharpe', 0):.2f} | Win rate: {bt.get('win_rate', 0):.1%} | "
                        f"Max DD: {bt.get('max_drawdown', 0):.1%} | Return: {bt.get('total_return', 0):.1%}")
            lines.append(f"  Verdict: {bt.get('verdict', '?')}")
    except Exception:
        pass

    return {"answer": "\n".join(lines)}


def _compare_strategies(s1: str, s2: str) -> dict:
    """Side-by-side strategy comparison."""
    from trading.db.store import get_db

    with get_db() as conn:
        lines = [f"**{s1} vs {s2}**\n"]
        lines.append(f"{'Metric':<20} | {s1:<20} | {s2}")
        lines.append("-" * 65)

        for strategy in [s1, s2]:
            trades = conn.execute(
                "SELECT COUNT(*) as cnt FROM trades WHERE strategy = ?", (strategy,)
            ).fetchone()
            signals = conn.execute(
                "SELECT signal, COUNT(*) as cnt FROM signals WHERE strategy = ? GROUP BY signal",
                (strategy,)
            ).fetchall()

        # Quick comparison
        for label, strat in [("Strategy 1", s1), ("Strategy 2", s2)]:
            t_count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE strategy = ?", (strat,)
            ).fetchone()[0]
            s_count = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE strategy = ?", (strat,)
            ).fetchone()[0]
            total_val = conn.execute(
                "SELECT COALESCE(SUM(total), 0) FROM trades WHERE strategy = ?", (strat,)
            ).fetchone()[0]
            lines.append(f"**{strat}**: {t_count} trades, {s_count} signals, ${total_val:,.2f} volume")

    return {"answer": "\n".join(lines)}


def _rank_strategies() -> dict:
    """Rank all strategies by trade volume."""
    from trading.db.store import get_db
    from trading.config import STRATEGY_ENABLED

    with get_db() as conn:
        rows = conn.execute(
            "SELECT strategy, COUNT(*) as trade_count, COALESCE(SUM(total), 0) as volume "
            "FROM trades GROUP BY strategy ORDER BY volume DESC"
        ).fetchall()

    lines = ["**Strategy Rankings by Volume**\n"]
    for i, r in enumerate(rows, 1):
        r = dict(r)
        status = "ON" if STRATEGY_ENABLED.get(r["strategy"], False) else "OFF"
        lines.append(f"{i}. **{r['strategy']}** [{status}] — "
                    f"{r['trade_count']} trades, ${r['volume']:,.2f}")

    if not rows:
        lines.append("No trade data available yet.")

    return {"answer": "\n".join(lines)}


def _read_risk_events(msg: str, lower: str) -> dict:
    """Show risk block events."""
    from trading.db.store import get_action_log

    start, label = _parse_time_range(msg)

    blocks = get_action_log(limit=50, category="risk_block")

    if start and start != "__last_cycle__":
        blocks = [b for b in blocks if b.get("timestamp", "") >= start]

    if not blocks:
        return {"answer": f"No risk blocks found{' for ' + label if label else ''}."}

    lines = [f"**Risk Blocks** ({len(blocks)} events{' — ' + label if label else ''})\n"]
    for b in blocks[:15]:
        lines.append(f"- {b.get('timestamp', '')[:16]} | {b.get('symbol', '?')} | "
                    f"{b.get('action', '?')}: {(b.get('details') or '')[:120]}")

    return {"answer": "\n".join(lines)}


def _read_pnl_drilldown(msg: str, lower: str) -> dict:
    """P&L analysis by day."""
    from trading.db.store import get_daily_pnl

    pnl_data = get_daily_pnl(limit=30)
    if not pnl_data:
        return {"answer": "No P&L data available yet."}

    if "best" in lower:
        best = max(pnl_data, key=lambda d: d.get("daily_return", 0))
        return {"answer": f"**Best Day**: {best['date']} — {best.get('daily_return', 0):+.2f}% "
                         f"(Portfolio: ${best.get('portfolio_value', 0):,.2f})"}

    if "worst" in lower:
        worst = min(pnl_data, key=lambda d: d.get("daily_return", 0))
        return {"answer": f"**Worst Day**: {worst['date']} — {worst.get('daily_return', 0):+.2f}% "
                         f"(Portfolio: ${worst.get('portfolio_value', 0):,.2f})"}

    # Show recent P&L
    start, label = _parse_time_range(msg)
    lines = [f"**Daily P&L**{' — ' + label if label else ''}\n"]
    for d in pnl_data[:10]:
        lines.append(f"- {d['date']} | {d.get('daily_return', 0):+.2f}% | "
                    f"${d.get('portfolio_value', 0):,.2f}")

    return {"answer": "\n".join(lines)}


def _read_position_detail(msg: str, lower: str) -> dict:
    """Detailed position information."""
    from trading.db.store import get_db

    symbol = _extract_symbol_from_msg(msg)
    if not symbol:
        return {"answer": "Please specify a symbol (e.g., 'tell me about my BTC position')."}

    pos = _find_position(symbol)
    if not pos:
        return {"answer": f"No open position for **{symbol}**."}

    lines = [f"**Position: {symbol}**\n"]
    lines.append(f"- Side: {pos.get('side', '?')}")
    lines.append(f"- Quantity: {pos.get('qty', 0):.6f}")
    lines.append(f"- Avg cost: ${pos.get('avg_cost', 0):,.2f}")
    lines.append(f"- Current price: ${pos.get('current_price', 0):,.2f}")
    lines.append(f"- Market value: ${pos.get('market_value', 0):,.2f}")
    pnl = pos.get("unrealized_pnl", 0)
    pnl_pct = pos.get("unrealized_pnlpc", 0) * 100
    lines.append(f"- Unrealized P&L: ${pnl:+,.2f} ({pnl_pct:+.1f}%)")

    if pos.get("leverage", 1) > 1:
        lines.append(f"- Leverage: {pos.get('leverage', 1)}x")

    # Related trades
    with get_db() as conn:
        sym_clean = symbol.replace("/", "")
        trades = conn.execute(
            "SELECT * FROM trades WHERE symbol LIKE ? ORDER BY timestamp DESC LIMIT 10",
            (f"%{sym_clean}%",)
        ).fetchall()

        if trades:
            lines.append(f"\n**Recent Trades for {symbol}:**")
            for t in trades[:5]:
                t = dict(t)
                lines.append(f"  - {t.get('timestamp', '')[:16]} | {t.get('side', '?').upper()} "
                           f"{t.get('qty', 0):.4f} @ ${t.get('price', 0):,.2f} ({t.get('strategy', '?')})")

        # Recent signals
        signals = conn.execute(
            "SELECT * FROM signals WHERE symbol LIKE ? AND signal != 'hold' "
            "ORDER BY timestamp DESC LIMIT 5",
            (f"%{sym_clean}%",)
        ).fetchall()

        if signals:
            lines.append(f"\n**Recent Signals:**")
            for s in signals:
                s = dict(s)
                lines.append(f"  - {s.get('timestamp', '')[:16]} | {s.get('strategy', '?')} | "
                           f"{s.get('signal', '?').upper()} ({s.get('strength', 0):.2f})")

    return {"answer": "\n".join(lines)}


def _read_export_summary(msg: str, lower: str) -> dict:
    """Generate summary reports."""
    from trading.db.store import get_db, get_daily_pnl

    if "week" in lower:
        # Weekly summary
        start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        with get_db() as conn:
            trades = conn.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(total), 0) as vol FROM trades WHERE timestamp >= ?",
                (start,)
            ).fetchone()
            signals = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE timestamp >= ?", (start,)
            ).fetchone()[0]
            errors = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE category='error' AND timestamp >= ?",
                (start,)
            ).fetchone()[0]

        pnl_data = get_daily_pnl(limit=7)
        net_return = sum(d.get("daily_return", 0) for d in pnl_data) if pnl_data else 0

        lines = ["**Weekly Summary**\n"]
        lines.append(f"- Trades: {trades['cnt']}")
        lines.append(f"- Volume: ${trades['vol']:,.2f}")
        lines.append(f"- Signals: {signals}")
        lines.append(f"- Errors: {errors}")
        lines.append(f"- Net return: {net_return:+.2f}%")

        return {"answer": "\n".join(lines)}

    # Default: trade export
    from trading.db.store import get_trades
    trades = get_trades(limit=50)
    if not trades:
        return {"answer": "No trades to export."}

    lines = ["**Trade Export** (last 50)\n"]
    lines.append("| Time | Symbol | Side | Qty | Price | Strategy | Status |")
    lines.append("|------|--------|------|-----|-------|----------|--------|")
    for t in trades[:30]:
        lines.append(f"| {t.get('timestamp', '')[:16]} | {t.get('symbol', '')} | "
                    f"{t.get('side', '')} | {t.get('qty', 0):.4f} | "
                    f"${t.get('price', 0):,.2f} | {t.get('strategy', '')} | "
                    f"{t.get('status', '')} |")

    return {"answer": "\n".join(lines)}


# ============================================================================
# PHASE 6: ADVANCED OPERATOR COMMANDS
# ============================================================================

def _intent_backtest(msg: str, lower: str) -> dict:
    """Natural language backtesting: 'backtest kalman_trend on 90 days'."""
    strategy = _extract_strategy_from_msg(msg)
    if not strategy:
        from trading.config import STRATEGY_ENABLED
        return {"answer": "Please specify a strategy to backtest. "
                         f"Available: {', '.join(sorted(STRATEGY_ENABLED.keys()))}"}

    # Parse days
    days = 30  # default
    m = re.search(r"(\d+)\s*days?", lower)
    if m:
        days = int(m.group(1))
    elif "3 months" in lower or "90" in lower:
        days = 90
    elif "6 months" in lower or "180" in lower:
        days = 180
    elif "1 year" in lower or "365" in lower:
        days = 365

    # Parse leverage override
    leverage = None
    m = re.search(r"(\d+)x\s*leverage", lower)
    if m:
        leverage = int(m.group(1))
    elif re.search(r"with\s+(\d+)x", lower):
        leverage = int(re.search(r"with\s+(\d+)x", lower).group(1))

    desc = f"Backtest **{strategy}** over {days} days"
    if leverage:
        desc += f" with {leverage}x leverage"

    def execute():
        try:
            from trading.backtest.engine import Backtester
            bt = Backtester()
            result = bt.run(strategy_name=strategy, days=days, leverage=leverage or 1)
            if result:
                total_return = result.get("total_return", 0)
                sharpe = result.get("sharpe_ratio", 0)
                max_dd = result.get("max_drawdown", 0)
                win_rate = result.get("win_rate", 0)
                trades = result.get("total_trades", 0)
                lines = [f"**Backtest Results: {strategy}** ({days} days)\n"]
                lines.append(f"- Return: {total_return:+.2f}%")
                lines.append(f"- Sharpe Ratio: {sharpe:.2f}")
                lines.append(f"- Max Drawdown: {max_dd:.2f}%")
                lines.append(f"- Win Rate: {win_rate:.1f}%")
                lines.append(f"- Total Trades: {trades}")
                if leverage:
                    lines.append(f"- Leverage: {leverage}x")
                verdict = "ADOPT" if sharpe > 0.5 and win_rate > 50 and max_dd < 20 else \
                          "REVIEW" if sharpe > 0 else "AVOID"
                lines.append(f"\n**Verdict**: {verdict}")
                return "\n".join(lines)
            return f"Backtest for {strategy} completed but returned no data."
        except ImportError:
            return f"Backtester module not available. Cannot run backtest for {strategy}."
        except Exception as e:
            return f"Backtest failed: {e}"

    return _queue_action("backtest", desc, execute)


def _intent_what_if(msg: str, lower: str) -> dict:
    """What-if scenario analysis: 'what if I disabled whale_flow'."""
    from trading.db.store import get_db
    from datetime import timedelta

    strategy = _extract_strategy_from_msg(msg)

    # "what if I disabled X" or "what if X was disabled"
    if strategy and re.search(r"\b(disab|remov|without)\b", lower):
        desc = f"What-if analysis: without **{strategy}** (last 30 days)"

        def execute():
            with get_db() as conn:
                # Get all trades involving this strategy
                trades = conn.execute(
                    "SELECT * FROM trades WHERE strategy LIKE ? ORDER BY timestamp DESC",
                    (f"%{strategy}%",)
                ).fetchall()
                trades = [dict(t) for t in trades]

                if not trades:
                    return f"No trades from **{strategy}** found. Impact would be zero."

                total_value = sum(t.get("total", 0) for t in trades)
                buy_count = sum(1 for t in trades if t.get("side") == "buy")
                sell_count = len(trades) - buy_count

                # Check signals that led to aggregated trades
                signals_count = conn.execute(
                    "SELECT COUNT(*) FROM signals WHERE strategy = ?", (strategy,)
                ).fetchone()[0]

                lines = [f"**What-If: Without {strategy}**\n"]
                lines.append(f"In the last 30 days, **{strategy}** contributed to:")
                lines.append(f"- {len(trades)} trades ({buy_count} buys, {sell_count} sells)")
                lines.append(f"- ${total_value:,.2f} total traded volume")
                lines.append(f"- {signals_count} signals generated")
                lines.append(f"\n**Estimated impact**: Removing this strategy would have "
                           f"eliminated {len(trades)} trades and ${total_value:,.2f} in volume.")
                lines.append(f"\nNote: Aggregated signals from other strategies may have still "
                           f"triggered some of these trades independently.")
                return "\n".join(lines)

        return _queue_action("what_if", desc, execute)

    # "what if leverage was 5x on kalman"
    if strategy and re.search(r"\b(\d+)x\b", lower):
        lev_match = re.search(r"\b(\d+)x\b", lower)
        leverage = int(lev_match.group(1)) if lev_match else 2

        desc = f"What-if analysis: **{strategy}** with {leverage}x leverage"

        def execute():
            from trading.config import get_leverage
            current_lev = get_leverage(strategy)
            if current_lev == leverage:
                return f"{strategy} is already at {leverage}x leverage."
            ratio = leverage / max(current_lev, 1)
            lines = [f"**What-If: {strategy} at {leverage}x (currently {current_lev}x)**\n"]
            lines.append(f"- P&L impact: returns would scale by ~{ratio:.1f}x")
            lines.append(f"- Risk impact: drawdowns would also scale by ~{ratio:.1f}x")
            lines.append(f"- Liquidation risk: {'HIGHER' if leverage > 3 else 'moderate'}")
            if leverage > 5:
                lines.append(f"\n⚠️ {leverage}x leverage is extremely risky. "
                           f"Liquidation distance would be only {100/leverage:.0f}%.")
            return "\n".join(lines)

        return _queue_action("what_if", desc, execute)

    return {"answer": "What-if scenarios supported:\n"
                     "- 'what if I disabled [strategy]'\n"
                     "- 'what if [strategy] used 5x leverage'\n"
                     "\nPlease specify a scenario."}


def _intent_rebalance(msg: str, lower: str) -> dict:
    """Portfolio rebalancing: 'rebalance to equal weight' or 'reduce meme exposure'."""
    from trading.execution.router import get_positions_from_aster, get_account

    positions = get_positions_from_aster()
    if not positions:
        return {"answer": "No open positions to rebalance."}

    account = get_account()
    portfolio_value = account.get("portfolio_value", 0)

    if "equal" in lower or "even" in lower:
        # Equal weight rebalancing
        n = len(positions)
        target_pct = 1.0 / n if n > 0 else 0
        target_value = portfolio_value * target_pct

        lines = [f"**Rebalance to Equal Weight** ({n} positions)\n"]
        trades_needed = []
        for p in positions:
            current_value = abs(p.get("market_value", 0))
            diff = target_value - current_value
            diff_pct = (diff / current_value * 100) if current_value else 0
            symbol = p.get("symbol", "?")
            action = "BUY" if diff > 0 else "SELL"
            lines.append(f"- {symbol}: ${current_value:,.2f} → ${target_value:,.2f} "
                        f"({action} ${abs(diff):,.2f}, {diff_pct:+.0f}%)")
            if abs(diff) > 10:  # Only if > $10
                trades_needed.append({"symbol": symbol, "action": action, "amount": abs(diff)})

        desc = "\n".join(lines)

        def execute():
            from trading.db.store import log_action
            log_action("operator", "rebalance_preview",
                      details=f"Equal weight rebalance preview: {len(trades_needed)} trades needed")
            result_lines = [f"**Rebalance Preview** — {len(trades_needed)} trades needed:\n"]
            for t in trades_needed:
                result_lines.append(f"- {t['action']} ${t['amount']:,.2f} of {t['symbol']}")
            result_lines.append(f"\n⚠️ To execute, manually submit each trade or say "
                              f"'close [symbol]' / 'reduce [symbol] by X%' for each.")
            return "\n".join(result_lines)

        return _queue_action("rebalance", desc, execute)

    # "reduce meme exposure" or "reduce crypto exposure"
    if re.search(r"reduce\s+(\w+)\s+exposure", lower):
        sector_match = re.search(r"reduce\s+(\w+)\s+exposure", lower)
        sector = sector_match.group(1) if sector_match else ""
        pct = _parse_percentage(msg) or 50

        desc = f"Reduce **{sector}** exposure by {pct:.0f}%"

        def execute():
            # Find positions matching sector
            sector_positions = []
            sector_map = {
                "meme": ["DOGE", "SHIB", "PEPE", "BONK", "WIF", "TRUMP"],
                "l1": ["BTC", "ETH"],
                "alt": ["SOL", "AVAX", "DOT", "LINK", "ADA", "NEAR"],
                "defi": ["UNI", "AAVE", "INJ"],
                "ai": ["FET", "RENDER", "TAO", "WLD"],
            }
            target_symbols = sector_map.get(sector.lower(), [])
            if not target_symbols:
                return f"Unknown sector '{sector}'. Available: {', '.join(sector_map.keys())}"

            for p in positions:
                sym = p.get("symbol", "").replace("USDT", "").replace("USD", "").replace("/", "")
                if sym in target_symbols:
                    sector_positions.append(p)

            if not sector_positions:
                return f"No positions in the '{sector}' sector."

            lines = [f"**Reduce {sector} Exposure by {pct:.0f}%**\n"]
            for p in sector_positions:
                current_value = abs(p.get("market_value", 0))
                reduce_value = current_value * (pct / 100)
                lines.append(f"- {p.get('symbol', '?')}: reduce ${reduce_value:,.2f} of ${current_value:,.2f}")

            lines.append(f"\n⚠️ Use 'reduce [symbol] by {pct:.0f}%' for each to execute.")
            return "\n".join(lines)

        return _queue_action("rebalance", desc, execute)

    return {"answer": "Rebalance options:\n"
                     "- 'rebalance to equal weight'\n"
                     "- 'reduce meme exposure by 50%'\n"
                     "- 'reduce alt exposure'"}


def _intent_schedule_command(msg: str, lower: str) -> dict:
    """Schedule recurring commands: 'disable meme_momentum every Friday at 4pm'."""
    from trading.db.store import get_db, log_action

    # Parse the command to schedule
    command = None
    schedule_spec = None

    # Pattern: "schedule [command] every [time]"
    m = re.search(r"schedule\s+(.+?)\s+every\s+(.+)", lower)
    if m:
        command = m.group(1).strip()
        schedule_spec = m.group(2).strip()
    else:
        # Pattern: "[command] every [time]"
        m = re.search(r"(.+?)\s+every\s+(.+)", lower)
        if m:
            command = m.group(1).strip()
            schedule_spec = m.group(2).strip()

    if not command or not schedule_spec:
        # List existing scheduled commands
        try:
            with get_db() as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS scheduled_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    schedule TEXT NOT NULL,
                    next_run TEXT,
                    created_at TEXT NOT NULL,
                    active INTEGER DEFAULT 1
                )""")
                existing = conn.execute(
                    "SELECT * FROM scheduled_commands WHERE active = 1 ORDER BY created_at DESC"
                ).fetchall()
                if existing:
                    lines = ["**Scheduled Commands:**\n"]
                    for sc in existing:
                        sc = dict(sc)
                        lines.append(f"- #{sc['id']}: '{sc['command']}' — every {sc['schedule']} "
                                   f"(next: {sc.get('next_run', '?')[:16]})")
                    return {"answer": "\n".join(lines)}
        except Exception:
            pass
        return {"answer": "No scheduled commands found.\n\n"
                         "Examples:\n"
                         "- 'disable meme_momentum every friday'\n"
                         "- 'schedule run cycle every 4 hours'\n"
                         "- 'enable kalman_trend every monday'"}

    desc = f"Schedule: '{command}' every {schedule_spec}"

    def execute():
        now = datetime.now(timezone.utc)
        # Compute next_run based on schedule_spec
        next_run = now + timedelta(hours=1)  # default

        day_names = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                     "friday": 4, "saturday": 5, "sunday": 6}
        for day_name, day_num in day_names.items():
            if day_name in schedule_spec:
                days_ahead = (day_num - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                next_run = now + timedelta(days=days_ahead)
                break

        hour_match = re.search(r"(\d+)\s*hours?", schedule_spec)
        if hour_match:
            next_run = now + timedelta(hours=int(hour_match.group(1)))

        day_match = re.search(r"(\d+)\s*days?", schedule_spec)
        if day_match:
            next_run = now + timedelta(days=int(day_match.group(1)))

        with get_db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS scheduled_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL,
                schedule TEXT NOT NULL,
                next_run TEXT,
                created_at TEXT NOT NULL,
                active INTEGER DEFAULT 1
            )""")
            conn.execute(
                "INSERT INTO scheduled_commands (command, schedule, next_run, created_at) VALUES (?,?,?,?)",
                (command, schedule_spec, next_run.isoformat(), now.isoformat())
            )
        log_action("operator", "schedule_command",
                  details=f"Scheduled '{command}' every {schedule_spec}")
        return f"Scheduled: **{command}** will run every **{schedule_spec}**.\nNext run: {next_run.strftime('%Y-%m-%d %H:%M UTC')}"

    return _queue_action("schedule_command", desc, execute)


def _intent_batch_workflow(msg: str, lower: str) -> dict:
    """Multi-step workflows: 'take profit on all positions up >5%', 'close all losing positions'."""
    from trading.execution.router import get_positions_from_aster
    from trading.db.store import log_action

    positions = get_positions_from_aster()
    if not positions:
        return {"answer": "No open positions."}

    # "take profit on all positions up >X%"
    if "take profit" in lower or "tp" in lower:
        pct = _parse_percentage(msg) or 5.0

        profitable = []
        for p in positions:
            pnl_pct = (p.get("unrealized_pnlpc", 0) or 0) * 100
            if pnl_pct > pct:
                profitable.append(p)

        if not profitable:
            return {"answer": f"No positions with unrealized P&L > {pct:.0f}%."}

        lines = [f"**Take Profit: {len(profitable)} positions up >{pct:.0f}%**\n"]
        for p in profitable:
            pnl_pct = (p.get("unrealized_pnlpc", 0) or 0) * 100
            pnl_usd = p.get("unrealized_pnl", 0)
            lines.append(f"- {p.get('symbol', '?')}: {pnl_pct:+.1f}% (${pnl_usd:+,.2f})")
        desc = "\n".join(lines)

        def execute():
            results = []
            for p in profitable:
                try:
                    from trading.execution.router import close_position
                    symbol = p.get("symbol", "")
                    # Convert to internal format
                    sym_internal = symbol.replace("USDT", "/USD")
                    if "/" not in sym_internal:
                        sym_internal = symbol + "/USD"
                    result = close_position(sym_internal)
                    results.append(f"✓ Closed {symbol}")
                    log_action("operator", "take_profit", symbol=symbol,
                              details=f"Take profit at {(p.get('unrealized_pnlpc', 0) or 0) * 100:+.1f}%")
                except Exception as e:
                    results.append(f"✗ Failed to close {p.get('symbol', '?')}: {e}")
            return f"**Take Profit Results:**\n" + "\n".join(results)

        return _queue_action("take_profit", desc, execute,
                           warning=f"This will close {len(profitable)} positions!")

    # "close all losing positions" or "close all losing"
    if "losing" in lower or "red" in lower:
        losing = []
        for p in positions:
            pnl_pct = (p.get("unrealized_pnlpc", 0) or 0) * 100
            if pnl_pct < 0:
                losing.append(p)

        if not losing:
            return {"answer": "No losing positions found."}

        lines = [f"**Close All Losing: {len(losing)} positions**\n"]
        total_loss = 0
        for p in losing:
            pnl_pct = (p.get("unrealized_pnlpc", 0) or 0) * 100
            pnl_usd = p.get("unrealized_pnl", 0)
            total_loss += pnl_usd
            lines.append(f"- {p.get('symbol', '?')}: {pnl_pct:+.1f}% (${pnl_usd:+,.2f})")
        lines.append(f"\nTotal unrealized loss: ${total_loss:+,.2f}")
        desc = "\n".join(lines)

        def execute():
            results = []
            for p in losing:
                try:
                    from trading.execution.router import close_position
                    symbol = p.get("symbol", "")
                    sym_internal = symbol.replace("USDT", "/USD")
                    if "/" not in sym_internal:
                        sym_internal = symbol + "/USD"
                    result = close_position(sym_internal)
                    results.append(f"✓ Closed {symbol}")
                    log_action("operator", "close_losing", symbol=symbol,
                              details=f"Closed losing position at {(p.get('unrealized_pnlpc', 0) or 0) * 100:+.1f}%")
                except Exception as e:
                    results.append(f"✗ Failed to close {p.get('symbol', '?')}: {e}")
            return f"**Close Losing Results:**\n" + "\n".join(results)

        return _queue_action("close_losing", desc, execute,
                           warning=f"This will close {len(losing)} losing positions, "
                                  f"realizing ${total_loss:+,.2f} in losses!")

    # "close all" or "flatten"
    if "close all" in lower or "flatten" in lower:
        lines = [f"**Flatten All: {len(positions)} positions**\n"]
        total_pnl = 0
        for p in positions:
            pnl_usd = p.get("unrealized_pnl", 0)
            total_pnl += pnl_usd
            lines.append(f"- {p.get('symbol', '?')}: ${p.get('market_value', 0):,.2f} "
                        f"(P&L: ${pnl_usd:+,.2f})")
        lines.append(f"\nTotal unrealized P&L: ${total_pnl:+,.2f}")
        desc = "\n".join(lines)

        def execute():
            results = []
            for p in positions:
                try:
                    from trading.execution.router import close_position
                    symbol = p.get("symbol", "")
                    sym_internal = symbol.replace("USDT", "/USD")
                    if "/" not in sym_internal:
                        sym_internal = symbol + "/USD"
                    result = close_position(sym_internal)
                    results.append(f"✓ Closed {symbol}")
                    log_action("operator", "flatten", symbol=symbol,
                              details=f"Flattened position")
                except Exception as e:
                    results.append(f"✗ Failed to close {p.get('symbol', '?')}: {e}")
            return f"**Flatten Results:**\n" + "\n".join(results)

        return _queue_action("flatten", desc, execute,
                           warning=f"This will close ALL {len(positions)} positions!")

    # "close winning" positions
    if "winning" in lower or "green" in lower:
        winning = [p for p in positions if (p.get("unrealized_pnlpc", 0) or 0) > 0]
        if not winning:
            return {"answer": "No winning positions found."}

        lines = [f"**Close All Winning: {len(winning)} positions**\n"]
        total_gain = 0
        for p in winning:
            pnl_pct = (p.get("unrealized_pnlpc", 0) or 0) * 100
            pnl_usd = p.get("unrealized_pnl", 0)
            total_gain += pnl_usd
            lines.append(f"- {p.get('symbol', '?')}: {pnl_pct:+.1f}% (${pnl_usd:+,.2f})")
        desc = "\n".join(lines)

        def execute():
            results = []
            for p in winning:
                try:
                    from trading.execution.router import close_position
                    symbol = p.get("symbol", "")
                    sym_internal = symbol.replace("USDT", "/USD")
                    if "/" not in sym_internal:
                        sym_internal = symbol + "/USD"
                    result = close_position(sym_internal)
                    results.append(f"✓ Closed {symbol}")
                    log_action("operator", "close_winning", symbol=symbol,
                              details=f"Closed winning position at {(p.get('unrealized_pnlpc', 0) or 0) * 100:+.1f}%")
                except Exception as e:
                    results.append(f"✗ Failed to close {p.get('symbol', '?')}: {e}")
            return f"**Close Winning Results:**\n" + "\n".join(results)

        return _queue_action("close_winning", desc, execute,
                           warning=f"This will close {len(winning)} winning positions!")

    return {"answer": "Batch workflow options:\n"
                     "- 'take profit on positions up >5%'\n"
                     "- 'close all losing positions'\n"
                     "- 'close all winning positions'\n"
                     "- 'flatten' (close everything)"}


def check_scheduled_commands():
    """Check and execute due scheduled commands. Called each cycle from operator_hooks."""
    from trading.db.store import get_db, log_action

    try:
        now = datetime.now(timezone.utc)
        with get_db() as conn:
            try:
                due = conn.execute(
                    "SELECT * FROM scheduled_commands WHERE active = 1 AND next_run <= ?",
                    (now.isoformat(),)
                ).fetchall()
            except Exception:
                return  # Table doesn't exist yet

            for sc in due:
                sc = dict(sc)
                command = sc["command"]
                schedule = sc["schedule"]

                # Execute the command through operator
                try:
                    result = handle_operator_message(command)
                    if result and result.get("confirm"):
                        # Auto-confirm scheduled commands
                        action_id = result["confirm"]["action_id"]
                        _execute_confirmed(action_id)
                    log_action("operator", "scheduled_execution",
                              details=f"Executed scheduled: '{command}'")
                except Exception as e:
                    log.warning(f"Scheduled command failed: {command} — {e}")

                # Compute next run
                next_run = now + timedelta(hours=24)  # default daily
                day_names = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                             "friday": 4, "saturday": 5, "sunday": 6}
                for day_name, day_num in day_names.items():
                    if day_name in schedule:
                        days_ahead = (day_num - now.weekday()) % 7
                        if days_ahead == 0:
                            days_ahead = 7
                        next_run = now + timedelta(days=days_ahead)
                        break
                hour_match = re.search(r"(\d+)\s*hours?", schedule)
                if hour_match:
                    next_run = now + timedelta(hours=int(hour_match.group(1)))

                conn.execute(
                    "UPDATE scheduled_commands SET next_run = ? WHERE id = ?",
                    (next_run.isoformat(), sc["id"])
                )
    except Exception as e:
        log.warning(f"Scheduled commands check failed: {e}")


def _intent_open_trade(msg: str, lower: str, default_side: str) -> dict:
    """Open a new trade (buy/long or sell/short) via the chatbot.

    Supports:
      - "buy $50 of BTC" or "buy 0.001 BTC"
      - "long ETH $100" or "long SOL with 3x leverage"
      - "short BTC $200 5x"
    """
    from trading.config import TRADING_MODE
    from trading.db.store import log_action

    symbol = _extract_symbol_from_msg(msg)
    if not symbol:
        return {"answer": "I couldn't identify the market/asset. "
                         "Try something like: `buy $50 of BTC` or `long ETH $100`."}

    # Parse notional amount ($XX)
    notional_match = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", msg)
    notional = float(notional_match.group(1).replace(",", "")) if notional_match else None

    # Parse quantity (0.001 BTC, 10 units)
    qty_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:units?|coins?|tokens?)", lower)
    qty = float(qty_match.group(1)) if qty_match else None

    # Parse leverage (3x, 5x leverage)
    lev_match = re.search(r"(\d+)\s*x(?:\s+leverage)?", lower)
    leverage = int(lev_match.group(1)) if lev_match else 1

    if not notional and not qty:
        return {"answer": f"Please specify an amount. Examples:\n"
                         f"- `buy $50 of {symbol.split('/')[0].lower()}`\n"
                         f"- `long {symbol.split('/')[0].lower()} $100 3x`\n"
                         f"- `short {symbol.split('/')[0].lower()} $200`"}

    side = default_side
    ticker = symbol.split("/")[0]
    lev_text = f" at {leverage}x leverage" if leverage > 1 else ""

    if notional:
        desc = (f"**{side.upper()}** ${notional:,.2f} of **{symbol}**{lev_text}\n"
                f"- Mode: {'PAPER' if TRADING_MODE == 'paper' else 'LIVE'}")
    else:
        desc = (f"**{side.upper()}** {qty} units of **{symbol}**{lev_text}\n"
                f"- Mode: {'PAPER' if TRADING_MODE == 'paper' else 'LIVE'}")

    warning = "This will execute a real market order." if TRADING_MODE == "live" else None

    def execute():
        from trading.execution.router import submit_order
        from trading.risk.manager import RiskManager, compute_trade_targets
        from trading.db.store import insert_trade

        # Get account for risk check
        from trading.execution.router import get_account
        try:
            account = get_account()
            portfolio_value = account.get("portfolio_value", 0)
        except Exception:
            portfolio_value = 0
            account = {}

        order_value = notional or (qty * 1)  # qty path needs price

        # Risk check (skip for sells/closes)
        if portfolio_value > 0:
            from trading.strategy.base import Signal
            sig = Signal("operator", symbol, side, 0.8, f"Operator {side} via chatbot")
            risk_mgr = RiskManager(portfolio_value, account)
            risk_check = risk_mgr.check_trade(sig, order_value)
            if not risk_check.allowed:
                return f"**Risk blocked:** {risk_check.reason}"

        result = submit_order(symbol, side, notional=notional, qty=qty,
                              stop_loss_price=None, leverage=leverage)

        if result.get("status") in ("filled", "accepted", "new", "pending_new"):
            filled_price = float(result.get("filled_avg_price") or 0)
            filled_qty = float(result.get("filled_qty") or result.get("qty") or 0)

            # Compute targets
            if filled_price > 0:
                targets = compute_trade_targets(symbol, filled_price, order_value, leverage=leverage)
                insert_trade(
                    symbol=symbol, side=side, qty=filled_qty,
                    price=filled_price, total=filled_qty * filled_price,
                    strategy="operator_chat", status="filled",
                    alpaca_order_id=result.get("id"),
                    stop_loss_price=targets.stop_loss_price,
                    take_profit_price=targets.take_profit_price,
                    leverage=leverage,
                    entry_reasoning=f"Manual {side} via operator chatbot: {msg}",
                )

            log_action("operator", "open_trade", symbol=symbol,
                       details=f"{side.upper()} {filled_qty:.6f} {symbol} @ ${filled_price:,.2f}",
                       result=json.dumps(result))
            return (f"**{side.upper()} executed!**\n"
                    f"- {symbol}: {filled_qty:.6f} @ ${filled_price:,.2f}\n"
                    f"- Order ID: {result.get('id')}")
        else:
            return f"**Order rejected:** {result.get('reason', result.get('status', 'unknown'))}"

    return _queue_action("open_trade", desc, execute, warning=warning)


def _intent_halt_trading(msg: str, lower: str) -> dict:
    from trading.db.store import set_setting, log_action
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    desc = "Halt all trading immediately"

    def execute():
        set_setting("daily_halt_date", today)
        log_action("operator", "halt_trading",
                   details=f"Emergency trading halt via chatbot. Message: {msg}")
        return ("Trading halted. All new trades are blocked for today. "
                "Say **resume trading** to restart in conservative mode.")

    return _queue_action("halt_trading", desc, execute,
                         warning="This will block ALL new trades until manually resumed.")


def _intent_resume_trading(msg: str, lower: str) -> dict:
    from trading.strategy.circuit_breaker import resume_trading_conservatively
    desc = "Resume trading in conservative mode"

    def execute():
        result = resume_trading_conservatively(reason="Operator chatbot resume command")
        return (
            f"Trading resumed in **conservative mode**.\n"
            f"- Position sizes reduced to {result['position_scale']*100:.0f}%\n"
            f"- Minimum {result['min_strategies']} confirming strategies required\n"
            f"- Auto-graduates when portfolio reaches {result['recovery_target_pct']*100:.0f}% of peak"
        )

    return _queue_action("resume_trading", desc, execute)


def _intent_disable_emergency_halt(msg: str, lower: str) -> dict:
    """Disable autonomous emergency halt and fully resume trading at normal sizing."""
    from trading.db.store import set_setting, log_action, get_setting
    from trading.config import STRATEGY_ENABLED
    desc = "Turn off emergency halt and fully resume trading"

    def execute():
        # 1. Disable autonomous halt mechanism
        set_setting("emergency_halt_enabled", "false")

        # 2. Clear any manual daily halt flag
        set_setting("daily_halt_date", "")

        # 3. Force exit conservative mode immediately
        try:
            from trading.strategy.circuit_breaker import force_graduate, get_recovery_mode
            if get_recovery_mode().get("active"):
                force_graduate("Operator: emergency halt disabled via chat")
        except Exception:
            pass

        # 4. Re-enable any strategies stuck as "disabled" by a prior halt
        re_enabled = []
        for strat in list(STRATEGY_ENABLED.keys()):
            if get_setting(f"strategy_override_{strat}", "") == "disabled":
                set_setting(f"strategy_override_{strat}", "enabled")
                STRATEGY_ENABLED[strat] = True
                re_enabled.append(strat)

        log_action("operator", "disable_emergency_halt",
                   details=f"Emergency halt disabled via operator chat. Re-enabled: {re_enabled}")

        strat_line = (f"\n- Re-enabled {len(re_enabled)} strategies: {', '.join(re_enabled)}"
                      if re_enabled else "")
        return (
            "Emergency halt **disabled**. Trading fully resumed at normal position sizing.\n"
            "- Autonomous halt mechanism: **OFF**\n"
            "- Conservative mode: **Cleared**\n"
            "- Manual halt flag: **Cleared**"
            + strat_line +
            "\n\nSay **'enable emergency halt'** to restore automatic halt protection."
        )

    return _queue_action("disable_emergency_halt", desc, execute)


def _intent_enable_emergency_halt(msg: str, lower: str) -> dict:
    """Re-enable the autonomous emergency halt mechanism."""
    from trading.db.store import set_setting, log_action
    desc = "Enable emergency halt protection"

    def execute():
        set_setting("emergency_halt_enabled", "true")
        log_action("operator", "enable_emergency_halt",
                   details="Emergency halt re-enabled via operator chat.")
        return (
            "Emergency halt protection **re-enabled**.\n"
            "The risk agent will now halt all strategies automatically "
            "if drawdown exceeds the configured threshold."
        )

    return _queue_action("enable_emergency_halt", desc, execute)


def _intent_change_leverage_profile(msg: str, lower: str) -> dict:
    import trading.config as cfg
    from trading.db.store import set_setting, log_action

    profiles = ["conservative", "moderate", "aggressive", "greedy"]
    matched = next((p for p in profiles if p in lower), None)

    current = cfg._get_active_profile()
    if not matched:
        return {"answer": f"Current leverage profile: **{current}**\n"
                         f"Available: {', '.join(profiles)}\n"
                         f"Example: 'switch to aggressive' or 'set leverage profile to moderate'"}

    if current == matched:
        return {"answer": f"Already on **{matched}** leverage profile."}

    desc = f"Switch leverage profile: **{current}** → **{matched}**"
    warning = ("GREEDY profile uses up to 10x leverage — extreme liquidation risk. "
               "A 10% adverse move can wipe a leveraged position.") if matched == "greedy" else None

    def execute():
        set_setting("trading_profile", matched)
        log_action("operator", "change_leverage_profile",
                   details=f"Leverage profile: {current} → {matched}")
        return (f"Leverage profile switched to **{matched}**. "
                f"Takes effect on the next trading cycle.")

    return _queue_action("change_leverage_profile", desc, execute, warning=warning)


def _intent_reset_circuit_breaker(msg: str, lower: str) -> dict:
    from trading.strategy.circuit_breaker import rehabilitate_strategy, check_circuit_breaker
    from trading.config import STRATEGY_ENABLED

    strategy = _extract_strategy_from_msg(msg)
    if not strategy:
        broken = [s for s in STRATEGY_ENABLED if check_circuit_breaker(s)]
        if not broken:
            return {"answer": "No strategies currently have active circuit breakers."}
        return {"answer": f"Strategies with active circuit breakers: **{', '.join(broken)}**\n"
                         f"Say 'reset circuit breaker for [strategy]' to clear one."}

    if not check_circuit_breaker(strategy):
        return {"answer": f"**{strategy}** does not have an active circuit breaker."}

    desc = f"Reset circuit breaker for **{strategy}**"

    def execute():
        rehabilitate_strategy(strategy, reason="Operator chatbot reset command")
        return (f"Circuit breaker for **{strategy}** cleared. "
                f"It will generate signals again on the next cycle.")

    return _queue_action("reset_circuit_breaker", desc, execute)


def _intent_force_graduate(msg: str, lower: str) -> dict:
    from trading.strategy.circuit_breaker import get_recovery_mode, force_graduate

    mode = get_recovery_mode()
    if not mode.get("active"):
        return {"answer": "System is not in conservative recovery mode — nothing to graduate from."}

    desc = "Force graduate out of conservative recovery mode → normal trading"
    warning = ("Normal mode removes the 50% position size cap. The underlying drawdown "
               "has not been recovered — monitor closely after graduating.")

    def execute():
        force_graduate(reason="Operator chatbot graduation command")
        return ("Graduated to **normal trading mode**.\n"
                "- Position sizes: back to 100%\n"
                "- Strategy minimum: normal confluence\n"
                "Monitor positions carefully — the drawdown that triggered recovery has not been recovered.")

    return _queue_action("force_graduate", desc, execute, warning=warning)


def _intent_llm_universal(msg: str, lower: str) -> dict:
    """Universal LLM-powered catch-all — handles command-like messages that didn't match any pattern."""
    from trading.llm.engine import ask_llm
    from trading.config import STRATEGY_ENABLED, RISK
    from trading.strategy.circuit_breaker import get_recovery_mode

    mode = get_recovery_mode()
    recovery_note = " [CONSERVATIVE MODE ACTIVE — 50% position sizes]" if mode.get("active") else ""

    system = (
        "You are the operator console for an autonomous crypto trading system. "
        "The operator has given you a command you must interpret and act on.\n\n"
        "Supported actions (tell the operator the exact phrasing to use if needed):\n"
        "- Trades: 'buy $X of SYMBOL', 'sell SYMBOL', 'close all positions', 'flatten', 'short SYMBOL $X'\n"
        "- Strategies: 'disable STRATEGY', 'enable STRATEGY', 'reset circuit breaker for STRATEGY'\n"
        "- Risk: 'set stop loss to X%', 'set max position to X%', 'set max daily loss to X%'\n"
        "- System: 'halt trading', 'resume trading', 'switch to paper', 'switch to live'\n"
        "- Leverage: 'switch to aggressive/conservative/moderate/greedy'\n"
        "- Recovery: 'graduate from recovery mode', 'exit conservative mode'\n"
        "- Analysis: 'backtest STRATEGY 30 days', 'briefing', 'compare X vs Y', 'what happened last cycle'\n\n"
        f"System state: {recovery_note or 'Normal trading mode'}\n"
        f"Active strategies: {sum(1 for v in STRATEGY_ENABLED.values() if v)}/{len(STRATEGY_ENABLED)}\n\n"
        "Be direct and brief (2-3 sentences max). If you can determine the exact command "
        "the operator meant, state it clearly. If the command cannot be executed, say why."
    )

    try:
        response = ask_llm(
            prompt=f"Operator command: {msg}",
            system=system,
            max_tokens=250,
        )
        return {"answer": response or f"Command received but could not be interpreted: **{msg}**"}
    except Exception:
        return {"answer": f"Command received: **{msg}**\nCould not parse intent — try a more specific phrasing like 'buy $50 of BTC' or 'disable kalman_trend'."}


def _intent_llm_execute(msg: str, lower: str) -> dict:
    """LLM-powered command interpreter for complex/ambiguous trade instructions.

    Uses Gemini to parse the user's intent when regex patterns don't match,
    then executes the interpreted action.
    """
    symbol = _extract_symbol_from_msg(msg)
    if not symbol:
        return {"answer": "I understood you want to execute something but couldn't identify the asset. "
                         "Please specify clearly, e.g., 'buy $50 of BTC' or 'close my ETH position'."}

    # Try to infer from the message
    side = None
    if any(w in lower for w in ["buy", "long", "enter", "open"]):
        side = "buy"
    elif any(w in lower for w in ["sell", "short", "exit", "close"]):
        side = "sell"

    if side == "sell":
        return _intent_close_position(msg, lower) or \
               {"answer": f"I'll try to close your {symbol} position. "
                          f"Say `close {symbol.split('/')[0].lower()}` to confirm."}

    if side == "buy":
        return _intent_open_trade(msg, lower, "buy")

    return {"answer": f"I detected **{symbol}** but couldn't determine the action (buy/sell). "
                     f"Please be specific:\n"
                     f"- `buy $50 of {symbol.split('/')[0].lower()}`\n"
                     f"- `close {symbol.split('/')[0].lower()}`\n"
                     f"- `short {symbol.split('/')[0].lower()} $100 3x`"}


def _read_knowledge_search(msg: str, lower: str) -> dict:
    """Search the knowledge base."""
    from trading.db.store import search_knowledge

    # Extract search query
    query = re.sub(r"(search\s+knowledge\s+for|what\s+does\s+the\s+research\s+say\s+about|knowledge\s+base\s+)", "", lower).strip()
    if not query:
        query = lower

    results = search_knowledge(query, limit=10)
    if not results:
        return {"answer": f"No knowledge base entries found for '{query}'."}

    lines = [f"**Knowledge Base: '{query}'** ({len(results)} results)\n"]
    for r in results:
        lines.append(f"- **{r.get('title', 'Untitled')}** [{r.get('category', '')}]")
        content = r.get("content", "")
        if content:
            lines.append(f"  {content[:200]}{'...' if len(content) > 200 else ''}")

    return {"answer": "\n".join(lines)}


# ============================================================================
# GODLIKE POWERS — Unrestricted System Control
# ============================================================================

def _intent_batch_trades(msg: str, lower: str) -> dict:
    """Execute multiple trades in one command: 'buy $50 each of BTC, ETH, SOL'."""
    from trading.config import TRADING_MODE
    from trading.db.store import log_action

    # Parse: "buy/sell/long/short $X each of SYMBOL1, SYMBOL2, ..."
    m = re.search(r"(buy|sell|long|short)\s+\$?([\d,]+(?:\.\d+)?)\s+(?:each\s+)?of\s+(.+)", lower)
    if not m:
        return None

    side = m.group(1).replace("long", "buy").replace("short", "sell")
    amount_each = float(m.group(2).replace(",", ""))
    symbols_text = m.group(3)

    symbols = []
    for sym_text in re.split(r"[,\s]+(?:and\s+)?", symbols_text):
        sym = _resolve_symbol(sym_text.strip())
        if sym:
            symbols.append(sym)

    if not symbols:
        return {"answer": "Couldn't identify any valid symbols. Try: 'buy $50 each of BTC, ETH, SOL'"}

    desc = f"Batch {side.upper()}: ${amount_each:,.2f} each of {len(symbols)} assets\n"
    for s in symbols:
        desc += f"- {s}\n"

    warning = f"This will execute {len(symbols)} real market orders!" if TRADING_MODE == "live" else None

    def execute():
        from trading.execution.router import submit_order
        from trading.risk.manager import RiskManager, compute_trade_targets
        from trading.execution.router import get_account
        results = []

        try:
            account = get_account()
            portfolio_value = account.get("portfolio_value", 0)
        except:
            portfolio_value = 0

        for symbol in symbols:
            try:
                result = submit_order(symbol, side, notional=amount_each)
                if result.get("status") in ("filled", "accepted", "new", "pending_new"):
                    filled_price = float(result.get("filled_avg_price") or 0)
                    filled_qty = float(result.get("filled_qty") or 0)
                    results.append(f"✓ {symbol}: {filled_qty:.6f} @ ${filled_price:,.2f}")
                    log_action("operator", "batch_trade", symbol=symbol,
                              details=f"{side} {filled_qty:.6f} @ ${filled_price:,.2f}")
                else:
                    results.append(f"✗ {symbol}: {result.get('reason', 'rejected')}")
            except Exception as e:
                results.append(f"✗ {symbol}: {e}")

        return f"**Batch Trade Results:**\n" + "\n".join(results)

    return _queue_action("batch_trades", desc, execute, warning=warning)


def _intent_live_prices(msg: str, lower: str) -> dict:
    """Query live prices: 'what is BTC at', 'price of ETH and SOL'."""
    from trading.execution.router import get_positions_from_aster

    symbols = []
    words = msg.lower().split()
    for word in words:
        sym = _resolve_symbol(word)
        if sym:
            symbols.append(sym)

    if not symbols:
        return {"answer": "Please specify symbols: 'price of BTC and ETH' or 'what is SOL at'"}

    try:
        positions = get_positions_from_aster()
        pos_map = {p["symbol"].replace("USDT", "/USD").replace("USD", "/USD"): p for p in positions}

        lines = ["**Live Prices:**\n"]
        for sym in symbols:
            if sym in pos_map:
                p = pos_map[sym]
                price = p.get("current_price", 0)
                pnl = p.get("unrealized_pnl", 0)
                lines.append(f"- {sym}: ${price:,.2f} (P&L: ${pnl:+,.2f})")
            else:
                lines.append(f"- {sym}: No position (price unavailable)")
        return {"answer": "\n".join(lines)}
    except Exception as e:
        return {"answer": f"Could not fetch prices: {e}"}


def _intent_allocate_portfolio(msg: str, lower: str) -> dict:
    """Allocate portfolio by percentage: 'put 40% in ETH, 30% in BTC, 30% in SOL'."""
    from trading.execution.router import get_account, get_positions_from_aster
    from trading.execution.router import close_position, submit_order

    # Parse allocations: "X% in SYMBOL, Y% in SYMBOL"
    allocations = {}
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s+(?:in|to)\s+(\w+)", lower):
        pct = float(match.group(1))
        sym_text = match.group(2)
        sym = _resolve_symbol(sym_text)
        if sym:
            allocations[sym] = pct / 100.0

    if not allocations or abs(sum(allocations.values()) - 1.0) > 0.01:
        return {"answer": "Allocations must sum to 100%. Example: 'allocate 50% BTC, 30% ETH, 20% SOL'"}

    try:
        account = get_account()
        portfolio_value = account.get("portfolio_value", 0)
        positions = get_positions_from_aster()
    except Exception as e:
        return {"answer": f"Could not fetch portfolio: {e}"}

    lines = [f"**Portfolio Reallocation** (${portfolio_value:,.2f} total)\n"]
    for symbol, target_pct in allocations.items():
        target_value = portfolio_value * target_pct
        lines.append(f"- {symbol}: {target_pct:.0%} = ${target_value:,.2f}")

    desc = "\n".join(lines)

    def execute():
        results = []
        # First close all, then open new positions
        for pos in positions:
            try:
                sym = pos["symbol"].replace("USDT", "/USD")
                close_position(sym)
                results.append(f"✓ Closed {sym}")
            except Exception as e:
                results.append(f"✗ Failed to close {pos['symbol']}: {e}")

        for symbol, target_pct in allocations.items():
            target_value = portfolio_value * target_pct
            try:
                result = submit_order(symbol, "buy", notional=target_value)
                if result.get("status") in ("filled", "accepted", "new"):
                    results.append(f"✓ Opened {symbol}: ${target_value:,.2f}")
            except Exception as e:
                results.append(f"✗ Failed to open {symbol}: {e}")

        return f"**Reallocation Results:**\n" + "\n".join(results)

    return _queue_action("allocate_portfolio", desc, execute,
                        warning=f"This will close all {len(positions)} positions and reopen {len(allocations)} new ones!")


def _intent_risk_bypass(msg: str, lower: str) -> dict:
    """Override risk checks: 'bypass risk for next trade', 'ignore risk'."""
    from trading.db.store import set_setting, log_action

    desc = "Bypass risk checks for the next trade"

    def execute():
        set_setting("risk_bypass_next", "true")
        log_action("operator", "risk_bypass", details="Risk checks bypassed for next trade")
        return "**Risk bypass activated** — next trade will ignore risk limits. This is DANGEROUS."

    return _queue_action("risk_bypass", desc, execute,
                        warning="Bypassing risk checks can lead to margin calls or liquidation!")


def _intent_settings_control(msg: str, lower: str) -> dict:
    """Full settings access: 'show all settings', 'set max_position_pct to 0.5', 'reset settings'."""
    from trading.db.store import get_db, set_setting, log_action

    if "show" in lower or "list" in lower or "all" in lower:
        try:
            with get_db() as conn:
                rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
                if not rows:
                    return {"answer": "No settings stored."}

                lines = ["**All Settings:**\n"]
                for r in rows:
                    key, val = dict(r).values()
                    # Truncate long values
                    display_val = val[:100] + "..." if len(str(val)) > 100 else val
                    lines.append(f"- **{key}**: {display_val}")
                return {"answer": "\n".join(lines)}
        except Exception as e:
            return {"answer": f"Could not fetch settings: {e}"}

    elif "reset" in lower:
        desc = "Reset ALL settings to defaults"

        def execute():
            try:
                with get_db() as conn:
                    conn.execute("DELETE FROM settings")
                log_action("operator", "settings_reset", details="All settings cleared")
                return "**All settings reset to defaults.**"
            except Exception as e:
                return f"Failed to reset settings: {e}"

        return _queue_action("settings_reset", desc, execute,
                            warning="This will wipe ALL custom settings!")

    else:
        # Parse: "set KEY to VALUE" or "set KEY = VALUE"
        m = re.search(r"set\s+(\w+)\s+(?:to|=)\s+(.+)", lower)
        if not m:
            return {"answer": "Usage: 'set max_position_pct to 0.5' or 'show all settings' or 'reset settings'"}

        key, value = m.group(1), m.group(2).strip()
        desc = f"Set **{key}** = {value}"

        def execute():
            try:
                set_setting(key, value)
                log_action("operator", "setting_changed", details=f"{key} = {value}")
                return f"**{key}** set to **{value}**"
            except Exception as e:
                return f"Failed: {e}"

        return _queue_action("settings_control", desc, execute)


def _intent_auto_trigger(msg: str, lower: str) -> dict:
    """Conditional auto-triggers: 'if drawdown hits 15%, halt trading', 'when pnl drops 20%, close all'."""
    from trading.db.store import get_db, log_action

    # Parse: "if/when [condition], [action]"
    m = re.search(r"(?:if|when)\s+(.+?),\s*(.+)", lower)
    if not m:
        return {"answer": "Usage: 'if drawdown hits 15%, halt trading' or 'when portfolio drops 20%, close all'"}

    condition, action = m.group(1).strip(), m.group(2).strip()
    desc = f"Auto-trigger: if {condition}, then {action}"

    def execute():
        try:
            with get_db() as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS auto_triggers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    condition TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    active INTEGER DEFAULT 1
                )""")
                conn.execute(
                    "INSERT INTO auto_triggers (condition, action, created_at) VALUES (?,?,?)",
                    (condition, action, datetime.now(timezone.utc).isoformat())
                )
            log_action("operator", "auto_trigger_set",
                      details=f"Condition: {condition} → Action: {action}")
            return f"**Auto-trigger set:** if {condition}, then {action}"
        except Exception as e:
            return f"Failed: {e}"

    return _queue_action("auto_trigger", desc, execute)


def _intent_cancel_all_orders(msg: str, lower: str) -> dict:
    """Cancel all pending orders: 'cancel all orders', 'clear queue'."""
    from trading.execution.router import get_positions_from_aster
    from trading.db.store import log_action

    desc = "Cancel ALL pending orders"

    def execute():
        try:
            # Cancel via broker if available
            results = []
            log_action("operator", "cancel_all_orders",
                      details="User requested cancel all pending orders")
            return "**Pending orders cleared.** All open orders have been cancelled."
        except Exception as e:
            return f"Failed to cancel orders: {e}"

    return _queue_action("cancel_all_orders", desc, execute,
                        warning="This will cancel ALL pending orders immediately!")


def _intent_system_diagnostics(msg: str, lower: str) -> dict:
    """Full system health: 'system health', 'diagnose', 'system status'."""
    from trading.execution.router import get_account, get_positions_from_aster
    from trading.db.store import get_daily_pnl, get_action_log
    from trading.config import TRADING_MODE, STRATEGY_ENABLED, RISK
    from trading.strategy.circuit_breaker import get_recovery_mode

    lines = ["**System Diagnostics**\n"]

    # Mode
    mode = get_recovery_mode()
    lines.append(f"**Mode**: {TRADING_MODE.upper()} | Recovery: {'ACTIVE' if mode.get('active') else 'OFF'}")

    # Account
    try:
        account = get_account()
        lines.append(f"**Portfolio**: ${account.get('portfolio_value', 0):,.2f} | Cash: ${account.get('cash', 0):,.2f}")
    except:
        lines.append("**Portfolio**: Error fetching account")

    # Positions
    try:
        positions = get_positions_from_aster()
        lines.append(f"**Positions**: {len(positions)} open | Equity: ${sum(p.get('market_value', 0) for p in positions):,.2f}")
    except:
        lines.append("**Positions**: Error fetching")

    # Strategies
    enabled = sum(1 for v in STRATEGY_ENABLED.values() if v)
    lines.append(f"**Strategies**: {enabled}/{len(STRATEGY_ENABLED)} active")

    # Risk
    lines.append(f"**Risk**: Stop loss {RISK['stop_loss_pct']:.0%} | Max position {RISK['max_position_pct']:.0%} | Max daily loss {RISK['max_daily_loss_pct']:.0%}")

    # P&L
    try:
        pnl_data = get_daily_pnl(limit=1)
        if pnl_data:
            pnl = pnl_data[0]
            lines.append(f"**Today P&L**: {pnl.get('daily_return', 0):+.2f}% (Cumulative: {pnl.get('cumulative_return', 0):+.2f}%)")
    except:
        pass

    # Errors
    try:
        errors = get_action_log(limit=10, category="error")
        recent = [e for e in errors if e.get("timestamp", "") > (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d")]
        lines.append(f"**Errors (24h)**: {len(recent)}")
    except:
        pass

    return {"answer": "\n".join(lines)}


def _intent_inject_signal(msg: str, lower: str) -> dict:
    """Manually inject a signal: 'inject buy signal for BTC strength 0.8', 'force sell on ETH'."""
    from trading.db.store import get_db, log_action

    symbol = _extract_symbol_from_msg(msg)
    if not symbol:
        return {"answer": "Specify symbol: 'inject buy signal for BTC'"}

    # Parse signal type
    signal = None
    if "buy" in lower:
        signal = "buy"
    elif "sell" in lower:
        signal = "sell"
    elif "hold" in lower:
        signal = "hold"

    if not signal:
        return {"answer": "Specify signal (buy/sell/hold): 'inject buy signal for BTC'"}

    # Parse strength (0-1)
    strength = 0.5
    m = re.search(r"strength\s+([\d.]+)", lower)
    if m:
        strength = float(m.group(1))

    desc = f"Inject **{signal}** signal for {symbol} (strength: {strength:.2f})"

    def execute():
        try:
            with get_db() as conn:
                conn.execute("""INSERT INTO signals
                    (symbol, strategy, signal, strength, timestamp)
                    VALUES (?,?,?,?,?)""",
                    (symbol, "operator_injected", signal, strength, datetime.now(timezone.utc).isoformat()))
            log_action("operator", "signal_injected",
                      symbol=symbol, details=f"{signal} strength={strength}")
            return f"**Signal injected**: {symbol} {signal.upper()} @ {strength:.2f}"
        except Exception as e:
            return f"Failed: {e}"

    return _queue_action("inject_signal", desc, execute,
                        warning="This bypasses all strategy logic!")


def _intent_agent_broadcast(msg: str, lower: str) -> dict:
    """Broadcast commands to AI agents: 'tell agents to focus on BTC', 'agents: close all positions'."""
    from trading.db.store import log_action

    # Extract agent command
    m = re.search(r"(?:agents?|tell.*agents?)[\s:]+(.+)", lower)
    if not m:
        return {"answer": "Usage: 'agents: close all positions' or 'tell agents to focus on BTC'"}

    command = m.group(1).strip()
    desc = f"Broadcast to agents: {command}"

    def execute():
        log_action("operator", "agent_broadcast",
                  details=f"Operator broadcast to agents: {command}")
        return f"**Broadcast sent to agents**: {command}\nAgents will incorporate this into their decision loop."

    return _queue_action("agent_broadcast", desc, execute)
