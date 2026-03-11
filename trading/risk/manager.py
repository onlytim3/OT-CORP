"""Risk manager — enforces position limits, stop losses, drawdown protection.

v2: Adds correlation limits, crypto exposure cap, trading_blocked check,
    and buying-power awareness.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from trading.config import RISK, CRYPTO_SYMBOLS
from trading.db.store import get_positions, get_daily_pnl, get_trades
from trading.strategy.base import Signal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Crypto exposure cap — total crypto allocation as % of portfolio
# ---------------------------------------------------------------------------
MAX_CRYPTO_EXPOSURE_PCT: float = 0.70  # Max 70% of portfolio in crypto
MAX_CORRELATED_EXPOSURE_PCT: float = 0.50  # Max 50% in a single correlated group

# Correlation groups — assets that tend to move together
CORRELATION_GROUPS = {
    "btc_ecosystem": {"BTC/USD", "BCH/USD", "LTC/USD"},
    "eth_ecosystem": {"ETH/USD", "UNI/USD", "AAVE/USD", "LINK/USD"},
    "alt_l1": {"SOL/USD", "AVAX/USD", "DOT/USD"},
    "precious_metals": {"UGL", "AGQ"},
}

# Leverage factors for leveraged ETFs — used to calculate effective exposure
LEVERAGE_FACTORS: dict[str, float] = {
    "UGL": 2.0,   # 2x Gold
    "AGQ": 2.0,   # 2x Silver
}

# Reverse lookup: symbol → group name
_SYMBOL_TO_GROUP: dict[str, str] = {}
for group_name, symbols in CORRELATION_GROUPS.items():
    for sym in symbols:
        _SYMBOL_TO_GROUP[sym] = group_name


def _is_crypto(symbol: str) -> bool:
    """Check if a symbol is crypto (contains '/')."""
    return "/" in symbol


@dataclass
class RiskCheck:
    """Result of a risk check on a proposed trade."""
    allowed: bool
    reason: str
    signal: Signal


class RiskManager:
    """Enforces all risk rules before trade execution."""

    def __init__(self, portfolio_value: float, account: dict | None = None):
        self.portfolio_value = portfolio_value
        self.rules = RISK
        self.account = account or {}

    def check_trade(self, signal: Signal, order_value: float) -> RiskCheck:
        """Run all risk checks on a proposed trade. Returns RiskCheck."""
        # Fetch positions ONCE — avoids N+1 queries across sub-checks
        positions = get_positions()

        checks = [
            self._check_account_status(),
            self._check_buying_power(order_value),
            self._check_position_size(signal, order_value, positions),
            self._check_crypto_exposure(signal, order_value, positions),
            self._check_correlation_group(signal, order_value, positions),
            self._check_daily_loss(),
            self._check_max_drawdown(),
            self._check_cash_reserve(order_value, positions),
            self._check_trade_count(),
        ]
        for check in checks:
            if not check.allowed:
                log.warning("Risk blocked %s %s: %s", signal.action, signal.symbol, check.reason)
                return check
        return RiskCheck(allowed=True, reason="All risk checks passed", signal=signal)

    # ------------------------------------------------------------------
    # Account-level safety checks
    # ------------------------------------------------------------------

    def _check_account_status(self) -> RiskCheck:
        """Block all trading if Alpaca says trading_blocked=True."""
        if self.account.get("trading_blocked"):
            return RiskCheck(
                allowed=False,
                reason="Trading blocked by broker — account may be restricted or under review",
                signal=Signal("risk", "", "hold", 0, ""),
            )
        status = self.account.get("status", "ACTIVE")
        if status not in ("ACTIVE", "active"):
            return RiskCheck(
                allowed=False,
                reason=f"Account status is '{status}' — must be ACTIVE to trade",
                signal=Signal("risk", "", "hold", 0, ""),
            )
        return RiskCheck(allowed=True, reason="Account status OK", signal=Signal("risk", "", "hold", 0, ""))

    def _check_buying_power(self, order_value: float) -> RiskCheck:
        """Use broker's actual buying power, not estimated cash."""
        buying_power = self.account.get("buying_power")
        if buying_power is not None and order_value > buying_power:
            return RiskCheck(
                allowed=False,
                reason=f"Order ${order_value:.2f} exceeds buying power ${buying_power:.2f}",
                signal=Signal("risk", "", "hold", 0, ""),
            )
        return RiskCheck(allowed=True, reason="Buying power OK", signal=Signal("risk", "", "hold", 0, ""))

    # ------------------------------------------------------------------
    # Position-level checks
    # ------------------------------------------------------------------

    def _check_position_size(self, signal: Signal, order_value: float,
                             positions: list[dict]) -> RiskCheck:
        max_value = self.portfolio_value * self.rules["max_position_pct"]
        leverage = LEVERAGE_FACTORS.get(signal.symbol, 1.0)
        existing = sum(
            p["qty"] * (p["current_price"] or p["avg_cost"])
            for p in positions if p["symbol"] == signal.symbol
        )
        # Effective exposure accounts for leveraged ETFs
        effective_existing = existing * leverage
        effective_order = order_value * leverage
        total = effective_existing + effective_order if signal.action == "buy" else effective_existing

        if total > max_value:
            return RiskCheck(
                allowed=False,
                reason=f"Position size ${total:.2f} exceeds max ${max_value:.2f} ({self.rules['max_position_pct']*100:.0f}% of portfolio)",
                signal=signal,
            )
        return RiskCheck(allowed=True, reason="Position size OK", signal=signal)

    def _check_crypto_exposure(self, signal: Signal, order_value: float,
                               positions: list[dict]) -> RiskCheck:
        """Cap total crypto exposure at MAX_CRYPTO_EXPOSURE_PCT."""
        if signal.action != "buy" or not _is_crypto(signal.symbol):
            return RiskCheck(allowed=True, reason="Not a crypto buy", signal=signal)

        crypto_value = sum(
            p["qty"] * (p["current_price"] or p["avg_cost"])
            for p in positions if _is_crypto(p["symbol"])
        )
        new_total = crypto_value + order_value
        max_crypto = self.portfolio_value * MAX_CRYPTO_EXPOSURE_PCT

        if new_total > max_crypto:
            return RiskCheck(
                allowed=False,
                reason=f"Crypto exposure ${new_total:.2f} would exceed cap ${max_crypto:.2f} ({MAX_CRYPTO_EXPOSURE_PCT*100:.0f}%)",
                signal=signal,
            )
        return RiskCheck(allowed=True, reason="Crypto exposure OK", signal=signal)

    def _check_correlation_group(self, signal: Signal, order_value: float,
                                positions: list[dict]) -> RiskCheck:
        """Limit exposure within correlated asset groups."""
        if signal.action != "buy":
            return RiskCheck(allowed=True, reason="Not a buy", signal=signal)

        group = _SYMBOL_TO_GROUP.get(signal.symbol)
        if not group:
            return RiskCheck(allowed=True, reason="No correlation group", signal=signal)

        group_symbols = CORRELATION_GROUPS[group]
        # Apply leverage factors so 2x ETFs count at effective exposure
        group_value = sum(
            p["qty"] * (p["current_price"] or p["avg_cost"]) * LEVERAGE_FACTORS.get(p["symbol"], 1.0)
            for p in positions if p["symbol"] in group_symbols
        )
        new_total = group_value + order_value * LEVERAGE_FACTORS.get(signal.symbol, 1.0)
        max_group = self.portfolio_value * MAX_CORRELATED_EXPOSURE_PCT

        if new_total > max_group:
            return RiskCheck(
                allowed=False,
                reason=f"Correlated group '{group}' exposure ${new_total:.2f} exceeds cap ${max_group:.2f} ({MAX_CORRELATED_EXPOSURE_PCT*100:.0f}%)",
                signal=signal,
            )
        return RiskCheck(allowed=True, reason=f"Group '{group}' exposure OK", signal=signal)

    # ------------------------------------------------------------------
    # Portfolio-level checks
    # ------------------------------------------------------------------

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

    def _check_cash_reserve(self, order_value: float,
                            positions: list[dict]) -> RiskCheck:
        min_cash = self.portfolio_value * self.rules["min_cash_reserve_pct"]
        # Use broker's actual cash if available
        cash = self.account.get("cash", None)
        if cash is None:
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

    # ------------------------------------------------------------------
    # Stop-loss check (unchanged API, uses RISK config)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Portfolio summary
    # ------------------------------------------------------------------

    def get_portfolio_summary(self) -> dict:
        positions = get_positions()
        positions_value = sum(p["qty"] * (p["current_price"] or p["avg_cost"]) for p in positions)
        cash = self.account.get("cash", self.portfolio_value - positions_value)

        # Break down by asset class
        crypto_value = sum(
            p["qty"] * (p["current_price"] or p["avg_cost"])
            for p in positions if _is_crypto(p["symbol"])
        )
        etf_value = positions_value - crypto_value

        return {
            "portfolio_value": self.portfolio_value,
            "cash": cash,
            "positions_value": positions_value,
            "crypto_value": crypto_value,
            "etf_value": etf_value,
            "crypto_pct": crypto_value / self.portfolio_value * 100 if self.portfolio_value > 0 else 0,
            "cash_pct": cash / self.portfolio_value * 100 if self.portfolio_value > 0 else 0,
            "num_positions": len(positions),
            "positions": positions,
        }
