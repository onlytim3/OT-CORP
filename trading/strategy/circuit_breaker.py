"""Circuit breaker: auto-disable strategies after consecutive losses.

v2: Adds portfolio-level emergency halt + structured recovery protocol.
    After a drawdown halt, the system enters CONSERVATIVE mode (not full stop):
    - Position sizes reduced by 50%
    - Requires 2+ strategies to confirm a trade
    - Only top historically profitable strategies can fire
    Auto-exits conservative mode when portfolio recovers to 80% of peak.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

CONSECUTIVE_LOSS_THRESHOLD = 5
COOLDOWN_HOURS = 48

# Recovery protocol settings
CONSERVATIVE_POSITION_SCALE = 0.50   # 50% of normal position size
CONSERVATIVE_MIN_STRATEGIES = 2       # Require at least 2 confirming strategies
RECOVERY_TARGET_PCT = 0.80           # Exit conservative mode at 80% of peak portfolio

# Strategies NOT allowed in conservative mode (historically weakest)
CONSERVATIVE_BLOCKLIST = {"aggregator", "meme_momentum", "onchain_flow", "multi_factor_rank"}


# ---------------------------------------------------------------------------
# Strategy-level circuit breaker (unchanged API)
# ---------------------------------------------------------------------------

def check_circuit_breaker(strategy_name: str) -> bool:
    """Returns True if strategy is circuit-broken (should be skipped)."""
    from trading.db.store import get_setting, set_setting
    key = f"cb_{strategy_name}"
    raw = get_setting(key)
    if not raw:
        return False
    try:
        state = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    consecutive_losses = state.get("consecutive_losses", 0)
    last_updated = state.get("last_updated", "")
    if consecutive_losses >= CONSECUTIVE_LOSS_THRESHOLD:
        if last_updated:
            try:
                updated_at = datetime.fromisoformat(last_updated)
                if datetime.now(timezone.utc) - updated_at > timedelta(hours=COOLDOWN_HOURS):
                    reset_circuit_breaker(strategy_name)
                    logger.info(f"Circuit breaker reset for {strategy_name} after {COOLDOWN_HOURS}h cooldown")
                    return False
            except Exception:
                pass
        logger.warning(f"Circuit breaker ACTIVE for {strategy_name}: {consecutive_losses} consecutive losses")
        return True
    return False


def record_trade_result(strategy_name: str, is_loss: bool):
    """Record a trade result for circuit breaker tracking."""
    from trading.db.store import get_setting, set_setting
    key = f"cb_{strategy_name}"
    raw = get_setting(key)
    state = {"consecutive_losses": 0}
    if raw:
        try:
            state = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    if is_loss:
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
    else:
        state["consecutive_losses"] = 0
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    set_setting(key, json.dumps(state))


def reset_circuit_breaker(strategy_name: str):
    """Reset circuit breaker for a strategy."""
    from trading.db.store import set_setting
    set_setting(f"cb_{strategy_name}", json.dumps({
        "consecutive_losses": 0,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }))


# ---------------------------------------------------------------------------
# Portfolio-level recovery protocol (NEW)
# ---------------------------------------------------------------------------

def get_recovery_mode() -> dict:
    """Get the current recovery mode state.

    Returns a dict with:
        active (bool): Whether conservative recovery mode is active
        reason (str): Why it was activated
        activated_at (str): ISO timestamp
        position_scale (float): Scale factor for position sizes (0.5 = 50%)
        min_strategies (int): Minimum confirming strategies required
    """
    from trading.db.store import get_setting
    raw = get_setting("recovery_mode")
    if not raw:
        return {"active": False}
    try:
        state = json.loads(raw)
        return state
    except (json.JSONDecodeError, TypeError):
        return {"active": False}


def activate_recovery_mode(reason: str = "Drawdown halt cleared"):
    """Enter conservative recovery mode after a halt is lifted.

    Reduces position sizes and increases minimum confluence requirements
    until the portfolio recovers to RECOVERY_TARGET_PCT of its peak.
    """
    from trading.db.store import set_setting, log_action
    state = {
        "active": True,
        "reason": reason,
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "position_scale": CONSERVATIVE_POSITION_SCALE,
        "min_strategies": CONSERVATIVE_MIN_STRATEGIES,
        "blocklist": list(CONSERVATIVE_BLOCKLIST),
    }
    set_setting("recovery_mode", json.dumps(state))
    log_action(
        "system", "recovery_mode_activated",
        details=f"Conservative mode ON: {reason}. "
                f"Position size: {CONSERVATIVE_POSITION_SCALE*100:.0f}%, "
                f"Min strategies: {CONSERVATIVE_MIN_STRATEGIES}",
        result="active",
    )
    logger.warning(f"RECOVERY MODE ACTIVATED: {reason}")
    try:
        from trading.monitor.notifications import notify_circuit_breaker
        # Extract drawdown pct from reason string if present
        import re
        dd_match = re.search(r"(\d+\.?\d*)%", reason)
        dd_pct = float(dd_match.group(1)) if dd_match else 0.0
        notify_circuit_breaker(reason, dd_pct)
    except Exception:
        pass


def deactivate_recovery_mode():
    """Exit conservative recovery mode."""
    from trading.db.store import set_setting, log_action
    set_setting("recovery_mode", json.dumps({"active": False}))
    log_action(
        "system", "recovery_mode_deactivated",
        details=f"Conservative mode OFF: portfolio reached {RECOVERY_TARGET_PCT*100:.0f}% of peak",
        result="inactive",
    )
    logger.info("Recovery mode deactivated — normal trading resumed")


def force_graduate(reason: str = "Autonomous graduation"):
    """Forcefully exit conservative recovery mode.

    Called by the Autonomous Agent when market conditions are favorable
    and new guardrails (Wave 5) are active.
    """
    from trading.db.store import log_action
    mode = get_recovery_mode()
    if not mode.get("active"):
        return

    deactivate_recovery_mode()
    log_action(
        "autonomous_agent", "force_graduate",
        details=f"Forced graduation from recovery mode. Reason: {reason}",
        result="success",
    )
    logger.info(f"FORCE GRADUATE: {reason}")


def rehabilitate_strategy(strategy_name: str, reason: str = "Autonomous rehabilitation"):
    """Reset the circuit breaker for a strategy and log the event.

    Used when a strategy has been improved or filtered by new logic.
    """
    from trading.db.store import log_action
    reset_circuit_breaker(strategy_name)
    log_action(
        "autonomous_agent", "strategy_rehabilitated",
        details=f"Strategy '{strategy_name}' circuit breaker reset. Reason: {reason}",
        result="success",
    )
    logger.info(f"REHABILITATED STRATEGY: {strategy_name} ({reason})")


def check_recovery_graduation():
    """Auto-exit conservative mode when portfolio recovers to 80% of peak.

    Called by the daemon on each cycle. Safe to call even if recovery mode
    is not active (no-op).
    """
    mode = get_recovery_mode()
    if not mode.get("active"):
        return

    try:
        from trading.db.store import get_daily_pnl
        pnl_records = get_daily_pnl(limit=90)
        if len(pnl_records) < 2:
            return
        peak = max(r["portfolio_value"] for r in pnl_records)
        current = pnl_records[0]["portfolio_value"]
        recovery_pct = current / peak if peak > 0 else 0

        if recovery_pct >= RECOVERY_TARGET_PCT:
            deactivate_recovery_mode()
            logger.info(
                f"Recovery graduation: portfolio at {recovery_pct*100:.1f}% of peak "
                f"(target {RECOVERY_TARGET_PCT*100:.0f}%)"
            )
    except Exception as e:
        logger.error(f"Recovery graduation check failed: {e}")


def is_strategy_allowed_in_recovery(strategy_name: str) -> bool:
    """Returns False if this strategy is blocked during conservative recovery mode."""
    mode = get_recovery_mode()
    if not mode.get("active"):
        return True
    blocklist = set(mode.get("blocklist", list(CONSERVATIVE_BLOCKLIST)))
    return strategy_name not in blocklist


def get_position_scale() -> float:
    """Returns position size multiplier. 1.0 in normal mode, 0.5 in recovery mode."""
    mode = get_recovery_mode()
    if mode.get("active"):
        return mode.get("position_scale", CONSERVATIVE_POSITION_SCALE)
    return 1.0


def full_reset_trading(reason: str = "Manual full reset") -> dict:
    """Full reset — clears ALL halt state and re-enables ALL strategies immediately.

    Use this when the halt rules were too aggressive and you want to restart
    trading from the current balance without the gradual recovery protocol.
    """
    from trading.config import STRATEGY_ENABLED
    from trading.db.store import set_setting, log_action

    # Clear all halt flags
    set_setting("daily_halt_date", "")
    set_setting("recovery_mode", json.dumps({"active": False}))
    set_setting("risk_stage", "0")

    # Re-enable ALL strategies from config defaults
    for strat in STRATEGY_ENABLED:
        STRATEGY_ENABLED[strat] = True

    # Persist the re-enabled state to DB so it survives the next restart
    from trading.intelligence.autonomous import _DB_KEY_STRATEGY_ENABLED
    set_setting(_DB_KEY_STRATEGY_ENABLED, json.dumps(
        {k: True for k in STRATEGY_ENABLED}
    ))

    enabled_count = sum(1 for v in STRATEGY_ENABLED.values() if v)

    log_action(
        "system", "full_reset",
        details=f"Full trading reset: all halt state cleared, {enabled_count} strategies re-enabled. Reason: {reason}",
        result="normal",
    )
    logger.warning("FULL RESET: halt cleared, %d strategies re-enabled. %s", enabled_count, reason)

    return {
        "status": "normal",
        "message": f"Full reset complete. All halt state cleared. {enabled_count} strategies re-enabled. Trading resumes at normal position sizing.",
        "strategies_enabled": enabled_count,
    }


def resume_trading_conservatively(reason: str = "Manual resume") -> dict:
    """Public API: called by the /api/resume_trading endpoint.

    Clears the emergency halt and enters conservative recovery mode.
    Returns status dict for the API response.
    """
    from trading.db.store import set_setting, get_setting, log_action

    # Clear any daily halt flag
    set_setting("daily_halt_date", "")

    activate_recovery_mode(reason=f"Trading resumed in conservative mode ({reason})")

    log_action(
        "system", "trading_resumed",
        details=f"Emergency halt cleared. Recovery mode active. Reason: {reason}",
        result="conservative",
    )

    return {
        "status": "conservative",
        "message": "Trading resumed in conservative mode. "
                   f"Position sizes reduced to {CONSERVATIVE_POSITION_SCALE*100:.0f}%, "
                   f"requires {CONSERVATIVE_MIN_STRATEGIES}+ confirming strategies. "
                   f"Will auto-graduate to normal when portfolio reaches "
                   f"{RECOVERY_TARGET_PCT*100:.0f}% of peak.",
        "position_scale": CONSERVATIVE_POSITION_SCALE,
        "min_strategies": CONSERVATIVE_MIN_STRATEGIES,
        "recovery_target_pct": RECOVERY_TARGET_PCT,
    }
