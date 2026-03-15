"""Operator hooks — applied at the start of each trading cycle.

Reads strategy and risk overrides from the DB (set via operator console)
and patches the in-memory config dicts so the scheduler respects them.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def apply_strategy_overrides():
    """Read strategy_override_* settings from DB and patch STRATEGY_ENABLED."""
    from trading.config import STRATEGY_ENABLED
    from trading.db.store import get_db, set_setting, log_action

    now = datetime.now(timezone.utc)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'strategy_override_%'"
        ).fetchall()

    for row in rows:
        key = row["key"]
        value = row["value"]
        strategy = key.replace("strategy_override_", "")

        if strategy not in STRATEGY_ENABLED:
            continue

        # Handle JSON format (with expiry) or plain string
        try:
            data = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            data = None

        if isinstance(data, dict):
            status = data.get("status", "disabled")
            expires_at = data.get("expires_at")

            # Check expiry
            if expires_at:
                try:
                    exp_dt = datetime.fromisoformat(expires_at)
                    if now >= exp_dt:
                        # Expired — revert to default (enabled)
                        log.info("Strategy override expired for %s, reverting to enabled", strategy)
                        STRATEGY_ENABLED[strategy] = True
                        set_setting(key, "")  # Clear the override
                        log_action("system", "override_expired",
                                   details=f"Strategy override for {strategy} expired, reverted to enabled")
                        continue
                except (ValueError, TypeError):
                    pass

            STRATEGY_ENABLED[strategy] = (status == "enabled")
        elif value == "disabled":
            STRATEGY_ENABLED[strategy] = False
        elif value == "enabled":
            STRATEGY_ENABLED[strategy] = True
        # Empty string = cleared override, skip


def apply_risk_overrides():
    """Read risk_override_* settings from DB and patch RISK dict."""
    from trading.config import RISK
    from trading.db.store import get_db, set_setting, log_action

    now = datetime.now(timezone.utc)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'risk_override_%'"
        ).fetchall()

    for row in rows:
        key = row["key"]
        value = row["value"]
        param = key.replace("risk_override_", "")

        if not value or param not in RISK:
            continue

        try:
            data = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(data, dict) or "value" not in data:
            continue

        # Check expiry
        expires_at = data.get("expires_at")
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at)
                if now >= exp_dt:
                    # Expired — revert to original
                    old_value = data.get("old_value")
                    if old_value is not None:
                        RISK[param] = old_value
                        log.info("Risk override expired for %s, reverted to %s", param, old_value)
                    set_setting(key, "")  # Clear
                    log_action("system", "risk_override_expired",
                               details=f"Risk override for {param} expired, reverted to {old_value}")
                    continue
            except (ValueError, TypeError):
                pass

        # Apply the override
        RISK[param] = data["value"]


def check_alerts():
    """Check alert conditions (called at end of each cycle).

    TODO: Full implementation — for now, checks price alerts against current quotes.
    """
    from trading.db.store import get_db, set_setting, log_action

    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'alert_%'"
        ).fetchall()

    if not rows:
        return

    for row in rows:
        key = row["key"]
        value = row["value"]
        if not value:
            continue

        try:
            data = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue

        if "symbol" in data and "threshold" in data and "direction" in data:
            # Price alert
            try:
                from trading.execution.router import get_crypto_quote
                quote = get_crypto_quote(data["symbol"])
                mid = quote.get("mid", 0)
                if not mid:
                    continue

                triggered = False
                if data["direction"] == "below" and mid < data["threshold"]:
                    triggered = True
                elif data["direction"] == "above" and mid > data["threshold"]:
                    triggered = True

                if triggered:
                    ticker = data["symbol"].split("/")[0]
                    msg = (f"ALERT: {ticker} is at ${mid:,.2f} "
                           f"({data['direction']} ${data['threshold']:,.2f})")
                    log_action("alert", "price_alert", symbol=data["symbol"], details=msg)
                    log.warning(msg)
                    # Clear the alert after triggering
                    set_setting(key, "")
                    # Try to notify
                    try:
                        from trading.monitor.notifications import notify_error
                        notify_error(msg, "alert")
                    except Exception:
                        pass
            except Exception:
                pass
