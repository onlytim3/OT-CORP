"""
Notification system for trading alerts.

Sends alerts via Discord webhook, Telegram bot, or console fallback.
Completely self-contained — never raises exceptions to callers.
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def _redact_url(url: str) -> str:
    """Replace secret tokens in webhook URLs before logging."""
    return re.sub(r"(https?://[^/]+/)([A-Za-z0-9/_\-]{20,})", r"\1[REDACTED]", url)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_discord_webhook_url() -> str:
    """Read Discord webhook URL from env (deferred so config can load first)."""
    try:
        from trading.config import DISCORD_WEBHOOK_URL
        return DISCORD_WEBHOOK_URL
    except ImportError:
        return os.getenv("DISCORD_WEBHOOK_URL", "")


def _get_telegram_config() -> tuple[str, str]:
    """Read Telegram bot token and chat ID from env."""
    try:
        from trading.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        return TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    except ImportError:
        return (
            os.getenv("TELEGRAM_BOT_TOKEN", ""),
            os.getenv("TELEGRAM_CHAT_ID", ""),
        )


# Level -> (emoji, Discord embed color as decimal int)
_LEVEL_META: dict[str, tuple[str, int]] = {
    "info":      ("ℹ️",  0x3498DB),   # blue
    "warning":   ("⚠️",  0xF39C12),   # orange
    "error":     ("🔴", 0xE74C3C),   # red
    "trade":     ("📈", 0x2ECC71),   # green (overridden per side)
    "stop_loss": ("🛑", 0xE74C3C),   # red
}

_TRADE_SIDE_COLOR: dict[str, int] = {
    "buy":  0x2ECC71,  # green
    "sell": 0xE74C3C,  # red
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def _send_discord(
    title: str,
    message: str,
    level: str = "info",
    color_override: Optional[int] = None,
    fields: Optional[list[dict]] = None,
) -> bool:
    """Post a rich embed to a Discord webhook. Returns True on success."""
    url = _get_discord_webhook_url()
    if not url:
        return False

    try:
        import requests  # local import — keeps module importable without requests
    except ImportError:
        logger.debug("requests not installed; skipping Discord notification")
        return False

    emoji, color = _LEVEL_META.get(level, ("ℹ️", 0x3498DB))
    if color_override is not None:
        color = color_override

    embed: dict = {
        "title": f"{emoji}  {title}",
        "description": message,
        "color": color,
        "footer": {"text": _timestamp()},
    }

    if fields:
        embed["fields"] = fields

    payload = {"embeds": [embed]}

    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code in (200, 204):
            return True
        logger.debug("Discord webhook %s returned %s: %s", _redact_url(url), resp.status_code, resp.text[:200])
    except Exception:
        logger.debug("Discord send failed (webhook: %s)", _redact_url(url), exc_info=True)

    return False


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _send_telegram(
    title: str,
    message: str,
    level: str = "info",
) -> bool:
    """Send a formatted message via Telegram Bot API. Returns True on success."""
    bot_token, chat_id = _get_telegram_config()
    if not bot_token or not chat_id:
        return False

    try:
        import requests
    except ImportError:
        logger.debug("requests not installed; skipping Telegram notification")
        return False

    emoji, _ = _LEVEL_META.get(level, ("ℹ️", 0))

    text = (
        f"{emoji} <b>{title}</b>\n\n"
        f"{message}\n\n"
        f"<i>{_timestamp()}</i>"
    )

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(api_url, json=payload, timeout=5)
        if resp.status_code == 200:
            return True
        logger.debug("Telegram returned %s: %s", resp.status_code, resp.text[:200])
    except Exception:
        logger.debug("Telegram send failed (url: %s)", _redact_url(api_url), exc_info=True)

    return False


# ---------------------------------------------------------------------------
# Console fallback
# ---------------------------------------------------------------------------

def _log_to_console(title: str, message: str, level: str = "info") -> None:
    """Always log to the Python logger regardless of webhook delivery."""
    log_level = {
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "trade": logging.INFO,
        "stop_loss": logging.WARNING,
    }.get(level, logging.INFO)

    logger.log(log_level, "[%s] %s — %s", level.upper(), title, message)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify(title: str, message: str, level: str = "info") -> None:
    """
    Send a notification through available channels.

    Tries Discord first, then Telegram, then falls back to console-only.
    Never raises — notifications must not crash the trading system.

    Args:
        title:   Short heading for the alert.
        message: Body text with details.
        level:   One of "info", "warning", "error", "trade", "stop_loss".
    """
    try:
        # Always log to console/logger
        _log_to_console(title, message, level)

        # Try Discord, then Telegram
        sent = _send_discord(title, message, level)
        if not sent:
            _send_telegram(title, message, level)
    except Exception:
        # Absolute last resort — swallow everything
        try:
            logger.debug("notify() itself failed", exc_info=True)
        except Exception:
            pass


def notify_trade(
    symbol: str,
    side: str,
    amount: float,
    price: float,
    strategy: str,
) -> None:
    """Send a trade execution alert."""
    try:
        side_upper = side.upper()
        title = f"Trade Executed: {side_upper} {symbol}"
        message = (
            f"Symbol: {symbol}\n"
            f"Side: {side_upper}\n"
            f"Amount: ${amount:,.2f}\n"
            f"Price: ${price:,.4f}\n"
            f"Strategy: {strategy}"
        )

        color = _TRADE_SIDE_COLOR.get(side.lower(), 0x3498DB)
        fields = [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "Side", "value": side_upper, "inline": True},
            {"name": "Amount", "value": f"${amount:,.2f}", "inline": True},
            {"name": "Price", "value": f"${price:,.4f}", "inline": True},
            {"name": "Strategy", "value": strategy, "inline": True},
        ]

        _log_to_console(title, message, "trade")

        sent = _send_discord(title, message, "trade", color_override=color, fields=fields)
        if not sent:
            _send_telegram(title, message, "trade")
    except Exception:
        try:
            logger.debug("notify_trade() failed", exc_info=True)
        except Exception:
            pass


def notify_stop_loss(symbol: str, pnl_pct: float, qty: float) -> None:
    """Send a stop-loss trigger alert."""
    try:
        title = f"Stop-Loss Triggered: {symbol}"
        message = (
            f"Symbol: {symbol}\n"
            f"P&L: {pnl_pct:+.2f}%\n"
            f"Quantity sold: {qty:.6f}"
        )

        fields = [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "P&L", "value": f"{pnl_pct:+.2f}%", "inline": True},
            {"name": "Qty Sold", "value": f"{qty:.6f}", "inline": True},
        ]

        _log_to_console(title, message, "stop_loss")

        sent = _send_discord(title, message, "stop_loss", fields=fields)
        if not sent:
            _send_telegram(title, message, "stop_loss")
    except Exception:
        try:
            logger.debug("notify_stop_loss() failed", exc_info=True)
        except Exception:
            pass


def notify_error(error: str, context: str = "") -> None:
    """Send an error alert."""
    try:
        title = "Trading System Error"
        parts = [f"Error: {error}"]
        if context:
            parts.append(f"Context: {context}")
        message = "\n".join(parts)

        _log_to_console(title, message, "error")

        sent = _send_discord(title, message, "error")
        if not sent:
            _send_telegram(title, message, "error")
    except Exception:
        try:
            logger.debug("notify_error() failed", exc_info=True)
        except Exception:
            pass


def notify_cycle_summary(signals: int, executed: int, blocked: int) -> None:
    """Send an end-of-cycle summary."""
    try:
        title = "Cycle Summary"
        message = (
            f"Signals generated: {signals}\n"
            f"Trades executed: {executed}\n"
            f"Trades blocked: {blocked}"
        )

        fields = [
            {"name": "Signals", "value": str(signals), "inline": True},
            {"name": "Executed", "value": str(executed), "inline": True},
            {"name": "Blocked", "value": str(blocked), "inline": True},
        ]

        _log_to_console(title, message, "info")

        sent = _send_discord(title, message, "info", fields=fields)
        if not sent:
            _send_telegram(title, message, "info")
    except Exception:
        try:
            logger.debug("notify_cycle_summary() failed", exc_info=True)
        except Exception:
            pass


def notify_circuit_breaker(reason: str, drawdown_pct: float) -> None:
    """Send alert when circuit breaker activates."""
    try:
        title = "⚡ Circuit Breaker Activated"
        message = (
            f"Trading halted — circuit breaker triggered.\n"
            f"Reason: {reason}\n"
            f"Drawdown: {drawdown_pct:+.2f}%\n"
            f"System entering conservative recovery mode."
        )
        fields = [
            {"name": "Reason", "value": reason, "inline": False},
            {"name": "Drawdown", "value": f"{drawdown_pct:+.2f}%", "inline": True},
            {"name": "Status", "value": "Conservative Mode", "inline": True},
        ]
        _log_to_console(title, message, "error")
        sent = _send_discord(title, message, "error", fields=fields)
        if not sent:
            _send_telegram(title, message, "error")
    except Exception:
        pass


def notify_regime_shift(old_regime: str, new_regime: str, score: float) -> None:
    """Send alert when market regime changes."""
    try:
        direction = "↑" if score > 0 else "↓"
        title = f"Regime Shift: {old_regime} → {new_regime}"
        message = (
            f"Market regime changed.\n"
            f"Previous: {old_regime}\n"
            f"Current: {new_regime} (score {score:+.3f})\n"
            f"Routing multipliers and cycle frequency will adjust."
        )
        level = "info" if "bullish" in new_regime else "warning"
        _log_to_console(title, message, level)
        sent = _send_discord(title, message, level)
        if not sent:
            _send_telegram(title, message, level)
    except Exception:
        pass


def notify_passive_loss(symbol: str, pnl_pct: float) -> None:
    """Send alert when a position slips past stop-loss threshold passively."""
    try:
        title = f"Passive Loss Detected: {symbol}"
        message = (
            f"Position {symbol} crossed loss threshold without stop-loss trigger.\n"
            f"Current P&L: {pnl_pct:+.2f}%\n"
            f"Immediate review recommended."
        )
        _log_to_console(title, message, "warning")
        sent = _send_discord(title, message, "warning")
        if not sent:
            _send_telegram(title, message, "warning")
    except Exception:
        pass


def notify_volume_exit(symbol: str, pnl_pct: float) -> None:
    """Send alert when a position is closed due to volume dry-up."""
    try:
        title = f"Volume Exit: {symbol}"
        message = (
            f"Position {symbol} closed — market volume dried up.\n"
            f"P&L at exit: {pnl_pct:+.2f}%\n"
            f"Liquidity risk avoided."
        )
        _log_to_console(title, message, "info")
        sent = _send_discord(title, message, "info")
        if not sent:
            _send_telegram(title, message, "info")
    except Exception:
        pass


def notify_adaptation_applied(strategy: str, param: str, old_val: str, new_val: str) -> None:
    """Send alert when an autonomous agent changes a strategy parameter."""
    try:
        title = f"Adaptation Applied: {strategy}"
        message = (
            f"Strategy parameter updated autonomously.\n"
            f"Strategy: {strategy}\n"
            f"Parameter: {param}\n"
            f"Change: {old_val} → {new_val}"
        )
        _log_to_console(title, message, "info")
        sent = _send_discord(title, message, "info")
        if not sent:
            _send_telegram(title, message, "info")
    except Exception:
        pass


def notify_scale_in(symbol: str, add_pct: float, total_pnl_pct: float) -> None:
    """Send alert when a winning position is scaled into."""
    try:
        title = f"Scaled Into Winner: {symbol}"
        message = (
            f"Added {add_pct*100:.1f}% to {symbol}.\n"
            f"Current unrealized gain: {total_pnl_pct*100:+.1f}%\n"
            f"Position momentum confirmed."
        )
        _log_to_console(title, message, "trade")
        sent = _send_discord(title, message, "trade", color_override=0x2ECC71)
        if not sent:
            _send_telegram(title, message, "trade")
    except Exception:
        pass


def notify_risk_block(symbol: str, reason: str, strategy: str) -> None:
    """Send alert when risk manager blocks a trade with specific reason."""
    try:
        title = f"Trade Blocked: {symbol}"
        message = (
            f"Risk manager blocked a {strategy} signal on {symbol}.\n"
            f"Reason: {reason}"
        )
        _log_to_console(title, message, "warning")
        sent = _send_discord(title, message, "warning")
        if not sent:
            _send_telegram(title, message, "warning")
    except Exception:
        pass


def notify_sl_failure(symbol: str, qty: float, error: str) -> None:
    """Send CRITICAL alert when a stop-loss order fails and retry also failed."""
    try:
        title = f"🚨 CRITICAL: Stop-Loss Failed — {symbol}"
        message = (
            f"Stop-loss execution FAILED for {symbol}.\n"
            f"Qty: {qty:.6f}\n"
            f"Error: {error}\n\n"
            f"MANUAL INTERVENTION MAY BE REQUIRED."
        )
        _log_to_console(title, message, "error")
        # Send to both channels simultaneously (critical escalation)
        _send_discord(title, message, "error")
        _send_telegram(title, message, "error")
    except Exception:
        pass


def notify_macro_event_risk(event_name: str, hours_until: float) -> None:
    """Send alert when a major macro event is approaching."""
    try:
        title = f"Macro Event Risk: {event_name}"
        message = (
            f"High-impact macro event in {hours_until:.1f} hours.\n"
            f"Event: {event_name}\n"
            f"Action: Position sizing reduced, new entries restricted near event."
        )
        _log_to_console(title, message, "warning")
        sent = _send_discord(title, message, "warning")
        if not sent:
            _send_telegram(title, message, "warning")
    except Exception:
        pass


def notify_deployment_failure(missing: list[str], failed: dict[str, str]) -> None:
    """Send critical deployment failure alert to ALL channels (escalation level 1).

    Unlike normal alerts (try Discord then fallback to Telegram), this sends to
    both channels simultaneously to ensure operations team visibility.
    """
    try:
        title = "CRITICAL: Strategy Deployment Failure"
        parts = ["Strategy pre-flight check FAILED. Immediate action required.", ""]
        if missing:
            parts.append(f"Missing strategy files ({len(missing)}):")
            for name in missing:
                parts.append(f"  - {name}")
        if failed:
            parts.append(f"Broken strategy imports ({len(failed)}):")
            for name, err in failed.items():
                parts.append(f"  - {name}: {err}")
        parts.extend([
            "",
            "Escalation: Level 1 — Operations team action required",
            "System running in DEGRADED mode with available strategies.",
        ])
        message = "\n".join(parts)

        _log_to_console(title, message, "error")
        _send_discord(title, message, "error")
        _send_telegram(title, message, "error")
    except Exception:
        try:
            logger.debug("notify_deployment_failure() failed", exc_info=True)
        except Exception:
            pass
