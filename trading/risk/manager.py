"""Risk manager — enforces position limits, stop losses, drawdown protection."""

from dataclasses import dataclass
from datetime import datetime, timezone

from trading.config import RISK
from trading.db.store import get_positions, get_daily_pnl, get_trades
from trading.strategy.base import Signal


@dataclass
class RiskCheck:
    """Result of a risk check on a proposed trade."""
    allowed: bool
    reason: str
    signal: Signal


class RiskManager:
    """Enforces all risk rules before trade execution."""

    def __init__(self, portfolio_value: float):
        self.portfolio_value = portfolio_value
        self.rules = RISK

    def check_trade(self, signal: Signal, order_value: float) -> RiskCheck:
        """Run all risk checks on a proposed trade. Returns RiskCheck."""
        checks = [
            self._check_position_size(signal, order_value),
            self._check_daily_loss(),
            self._check_max_drawdown(),
            self._check_cash_reserve(order_value),
            self._check_trade_count(),
        ]
        for check in checks:
            if not check.allowed:
                return check
        return RiskCheck(allowed=True, reason="All risk checks passed", signal=signal)

    def _check_position_size(self, signal: Signal, order_value: float) -> RiskCheck:
        max_value = self.portfolio_value * self.rules["max_position_pct"]
        # Check existing position + new order
        positions = get_positions()
        existing = sum(p["qty"] * (p["current_price"] or p["avg_cost"]) for p in positions if p["symbol"] == signal.symbol)
        total = existing + order_value if signal.action == "buy" else existing

        if total > max_value:
            return RiskCheck(
                allowed=False,
                reason=f"Position size ${total:.2f} exceeds max ${max_value:.2f} ({self.rules['max_position_pct']*100:.0f}% of portfolio)",
                signal=signal,
            )
        return RiskCheck(allowed=True, reason="Position size OK", signal=signal)

    def _check_daily_loss(self) -> RiskCheck:
        pnl_records = get_daily_pnl(limit=1)
        if pnl_records:
            today = pnl_records[0]
            if today["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                daily_return = today.get("daily_return", 0) or 0
                if daily_return < -self.rules["max_daily_loss_pct"]:
                    return RiskCheck(
                        allowed=False,
                        reason=f"Daily loss {daily_return*100:.1f}% exceeds max {self.rules['max_daily_loss_pct']*100:.0f}% — trading halted for today",
                        signal=Signal("risk", "", "hold", 0, ""),
                    )
        return RiskCheck(allowed=True, reason="Daily loss OK", signal=Signal("risk", "", "hold", 0, ""))

    def _check_max_drawdown(self) -> RiskCheck:
        pnl_records = get_daily_pnl(limit=90)
        if len(pnl_records) < 2:
            return RiskCheck(allowed=True, reason="Insufficient data for drawdown check", signal=Signal("risk", "", "hold", 0, ""))
        peak = max(r["portfolio_value"] for r in pnl_records)
        current = pnl_records[0]["portfolio_value"]
        drawdown = (peak - current) / peak if peak > 0 else 0
        if drawdown > self.rules["max_drawdown_pct"]:
            return RiskCheck(
                allowed=False,
                reason=f"Drawdown {drawdown*100:.1f}% exceeds max {self.rules['max_drawdown_pct']*100:.0f}% — ALL trading halted",
                signal=Signal("risk", "", "hold", 0, ""),
            )
        return RiskCheck(allowed=True, reason="Drawdown OK", signal=Signal("risk", "", "hold", 0, ""))

    def _check_cash_reserve(self, order_value: float) -> RiskCheck:
        min_cash = self.portfolio_value * self.rules["min_cash_reserve_pct"]
        positions = get_positions()
        positions_value = sum(p["qty"] * (p["current_price"] or p["avg_cost"]) for p in positions)
        cash = self.portfolio_value - positions_value
        remaining = cash - order_value
        if remaining < min_cash:
            return RiskCheck(
                allowed=False,
                reason=f"Order would leave ${remaining:.2f} cash, below minimum ${min_cash:.2f}",
                signal=Signal("risk", "", "hold", 0, ""),
            )
        return RiskCheck(allowed=True, reason="Cash reserve OK", signal=Signal("risk", "", "hold", 0, ""))

    def _check_trade_count(self) -> RiskCheck:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades = get_trades(limit=100)
        today_trades = [t for t in trades if t["timestamp"].startswith(today)]
        if len(today_trades) >= self.rules["max_trades_per_day"]:
            return RiskCheck(
                allowed=False,
                reason=f"Already {len(today_trades)} trades today (max {self.rules['max_trades_per_day']})",
                signal=Signal("risk", "", "hold", 0, ""),
            )
        return RiskCheck(allowed=True, reason="Trade count OK", signal=Signal("risk", "", "hold", 0, ""))

    def check_stop_loss(self, symbol: str, current_price: float) -> RiskCheck | None:
        """Check if a position should be stopped out."""
        positions = get_positions()
        for pos in positions:
            if pos["symbol"] == symbol:
                loss_pct = (current_price - pos["avg_cost"]) / pos["avg_cost"]
                if loss_pct <= -self.rules["stop_loss_pct"]:
                    return RiskCheck(
                        allowed=False,
                        reason=f"Stop loss triggered: {symbol} at {loss_pct*100:.1f}% loss (threshold: -{self.rules['stop_loss_pct']*100:.0f}%)",
                        signal=Signal("risk", symbol, "sell", 1.0, f"Stop loss at {loss_pct*100:.1f}%"),
                    )
        return None

    def get_portfolio_summary(self) -> dict:
        positions = get_positions()
        positions_value = sum(p["qty"] * (p["current_price"] or p["avg_cost"]) for p in positions)
        cash = self.portfolio_value - positions_value
        return {
            "portfolio_value": self.portfolio_value,
            "cash": cash,
            "positions_value": positions_value,
            "cash_pct": cash / self.portfolio_value * 100 if self.portfolio_value > 0 else 0,
            "num_positions": len(positions),
            "positions": positions,
        }
