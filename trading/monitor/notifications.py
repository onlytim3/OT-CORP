"""
Notification system for trading alerts.

Sends alerts via Discord webhook, Telegram bot, or console fallback.
Completely self-contained — never raises exceptions to callers.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

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
        logger.debug("Discord returned %s: %s", resp.status_code, resp.text[:200])
    except Exception:
        logger.debug("Discord send failed", exc_info=True)

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
        logger.debug("Telegram send failed", exc_info=True)

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
