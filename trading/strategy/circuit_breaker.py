"""Circuit breaker: auto-disable strategies after consecutive losses."""
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

CONSECUTIVE_LOSS_THRESHOLD = 5
COOLDOWN_HOURS = 48


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
