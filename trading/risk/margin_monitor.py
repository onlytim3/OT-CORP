"""Margin health & liquidation monitoring."""
import logging
from typing import Any

logger = logging.getLogger(__name__)


def check_margin_health(positions: list[dict[str, Any]], mark_prices: dict[str, float] | None = None) -> list[dict]:
    actions = []
    for pos in positions:
        leverage = pos.get("leverage", 1)
        if leverage <= 1:
            continue
        entry_price = pos.get("entry_price") or pos.get("avg_price") or pos.get("avg_cost", 0)
        if not entry_price:
            continue
        side = pos.get("side", "LONG").upper()
        liq_distance = 1.0 / leverage
        liq_price = entry_price * (1 - liq_distance) if side == "LONG" else entry_price * (1 + liq_distance)
        mark = (mark_prices or {}).get(pos.get("symbol", ""), entry_price)
        if side == "LONG":
            margin_dist = (mark - liq_price) / mark if mark else 1.0
        else:
            margin_dist = (liq_price - mark) / mark if mark else 1.0
        sym = pos.get("symbol", "?")
        if margin_dist < 0.05:
            actions.append({"symbol": sym, "action": "emergency_close", "margin_distance": margin_dist})
            logger.critical("EMERGENCY: %s margin %.1f%%", sym, margin_dist * 100)
        elif margin_dist < 0.10:
            actions.append({"symbol": sym, "action": "reduce_50", "margin_distance": margin_dist})
            logger.warning("WARNING: %s margin %.1f%%", sym, margin_dist * 100)
        elif margin_dist < 0.20:
            actions.append({"symbol": sym, "action": "warn", "margin_distance": margin_dist})
    return actions


def check_margin_safety(positions: list[dict], mark_prices: dict[str, float] | None = None) -> tuple[bool, str]:
    for pos in positions:
        leverage = pos.get("leverage", 1)
        if leverage <= 1:
            continue
        entry_price = pos.get("entry_price") or pos.get("avg_price") or pos.get("avg_cost", 0)
        if not entry_price:
            continue
        side = pos.get("side", "LONG").upper()
        liq_distance = 1.0 / leverage
        liq_price = entry_price * (1 - liq_distance) if side == "LONG" else entry_price * (1 + liq_distance)
        mark = (mark_prices or {}).get(pos.get("symbol", ""), entry_price)
        if side == "LONG":
            margin_dist = (mark - liq_price) / mark if mark else 1.0
        else:
            margin_dist = (liq_price - mark) / mark if mark else 1.0
        if margin_dist < 0.15:
            return False, f"{pos.get('symbol','?')} within {margin_dist:.1%} of liquidation"
    return True, ""
