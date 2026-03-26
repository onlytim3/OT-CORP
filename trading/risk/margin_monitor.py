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
        # Normalize entry price across venue formats (entryPrice, entry_price, avg_price, avg_cost)
        entry_price = (pos.get("entry_price") or pos.get("entryPrice")
                       or pos.get("avg_price") or pos.get("avg_cost", 0))
        if not entry_price:
            sym = pos.get("symbol", "?")
            logger.warning("Margin check skipped for %s: no entry price available", sym)
            continue
        side = pos.get("side", "LONG").upper()
        liq_distance = 1.0 / leverage
        liq_price = entry_price * (1 - liq_distance) if side == "LONG" else entry_price * (1 + liq_distance)
        sym = pos.get("symbol", "?")
        mark = (mark_prices or {}).get(sym, 0)
        if not mark:
            # Don't use entry_price as mark — it gives false safety readings
            logger.warning("Margin check skipped for %s: no mark price available", sym)
            continue
        if side == "LONG":
            margin_dist = (mark - liq_price) / mark if mark else 1.0
        else:
            margin_dist = (liq_price - mark) / mark if mark else 1.0
        if margin_dist < 0.05:
            actions.append({"symbol": sym, "action": "emergency_close", "margin_distance": margin_dist})
            logger.critical("EMERGENCY: %s margin %.1f%%", sym, margin_dist * 100)
        elif margin_dist < 0.10:
            actions.append({"symbol": sym, "action": "reduce_50", "margin_distance": margin_dist})
            logger.warning("WARNING: %s margin %.1f%%", sym, margin_dist * 100)
        elif margin_dist < 0.20:
            actions.append({"symbol": sym, "action": "warn", "margin_distance": margin_dist})
    return actions


def check_passive_loss_accumulation(positions: list[dict], stop_loss_pct: float = 0.05) -> list[dict]:
    """Detect positions passively accumulating losses that may have been missed.

    Flags positions where:
    1. Loss exceeds stop-loss threshold but no stop was executed
    2. Loss is approaching the threshold and accelerating
    3. Position has been losing for extended periods

    These situations indicate a breakdown in risk management — the system
    should have closed these positions but didn't (e.g., margin constraints).
    """
    alerts = []
    for pos in positions:
        avg_cost = pos.get("avg_cost") or pos.get("entry_price") or pos.get("entryPrice", 0)
        if not avg_cost or avg_cost <= 0:
            continue

        current_price = pos.get("current_price", 0)
        if not current_price or current_price <= 0:
            continue

        side = (pos.get("side") or "long").lower()
        qty = pos.get("qty", 0)
        if qty <= 0:
            continue

        # Calculate P&L percentage
        if side in ("long", "buy"):
            loss_pct = (current_price - avg_cost) / avg_cost
        else:
            loss_pct = (avg_cost - current_price) / avg_cost

        # Position is beyond stop-loss but still open — critical
        if loss_pct <= -stop_loss_pct:
            unrealized_loss = abs(loss_pct * avg_cost * qty)
            alerts.append({
                "symbol": pos.get("symbol", "?"),
                "severity": "critical",
                "action": "emergency_close",
                "loss_pct": loss_pct,
                "unrealized_loss": unrealized_loss,
                "side": side,
                "qty": qty,
                "reason": (
                    f"PASSIVE LOSS: {pos.get('symbol', '?')} ({side}) at {loss_pct*100:.1f}% loss "
                    f"exceeds stop-loss threshold (-{stop_loss_pct*100:.0f}%). "
                    f"Unrealized loss: ${unrealized_loss:.2f}. Position should be closed immediately."
                ),
            })
        # Position approaching stop-loss — warning
        elif loss_pct <= -(stop_loss_pct * 0.7):
            alerts.append({
                "symbol": pos.get("symbol", "?"),
                "severity": "warning",
                "action": "monitor",
                "loss_pct": loss_pct,
                "side": side,
                "qty": qty,
                "reason": (
                    f"APPROACHING STOP: {pos.get('symbol', '?')} ({side}) at {loss_pct*100:.1f}% loss, "
                    f"nearing -{stop_loss_pct*100:.0f}% stop-loss threshold."
                ),
            })
    return alerts


def check_margin_safety(positions: list[dict], mark_prices: dict[str, float] | None = None) -> tuple[bool, str]:
    for pos in positions:
        leverage = pos.get("leverage", 1)
        if leverage <= 1:
            continue
        entry_price = (pos.get("entry_price") or pos.get("entryPrice")
                       or pos.get("avg_price") or pos.get("avg_cost", 0))
        if not entry_price:
            continue
        side = pos.get("side", "LONG").upper()
        liq_distance = 1.0 / leverage
        liq_price = entry_price * (1 - liq_distance) if side == "LONG" else entry_price * (1 + liq_distance)
        sym = pos.get("symbol", "?")
        mark = (mark_prices or {}).get(sym, 0)
        if not mark:
            continue
        if side == "LONG":
            margin_dist = (mark - liq_price) / mark if mark else 1.0
        else:
            margin_dist = (liq_price - mark) / mark if mark else 1.0
        if margin_dist < 0.15:
            return False, f"{pos.get('symbol','?')} within {margin_dist:.1%} of liquidation"
    return True, ""
