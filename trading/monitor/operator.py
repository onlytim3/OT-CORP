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
    """Resolve user text to an internal symbol like 'BTC/USD'."""
    from trading.config import CRYPTO_SYMBOLS
    t = text.strip().lower()
    if t in _SYMBOL_ALIASES:
        return _SYMBOL_ALIASES[t]
    # Check CRYPTO_SYMBOLS values (already in BTC/USD format)
    for _coin_id, sym in CRYPTO_SYMBOLS.items():
        if t == sym.lower() or t == sym.replace("/", "").lower():
            return sym
    # Check by ticker (e.g., "BTCUSD" or "BTC")
    for _coin_id, sym in CRYPTO_SYMBOLS.items():
        ticker = sym.split("/")[0].lower()
        if t == ticker:
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
    """Queue an action for confirmation and return the response."""
    _cleanup_expired()
    action_id = str(uuid.uuid4())[:8]
    _pending_actions[action_id] = {
        "type": action_type,
        "description": description,
        "execute_fn": execute_fn,
        "warning": warning,
        "created_at": datetime.now(timezone.utc),
    }
    result = {
        "answer": description,
        "confirm": {
            "action_id": action_id,
            "description": description,
            "warning": warning,
        },
    }
    return result


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

    # 2. Strategy control (disable, enable)
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
