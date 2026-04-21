"""Risk manager — enforces position limits, stop losses, drawdown protection.

v2: Adds correlation limits, crypto exposure cap, trading_blocked check,
    and buying-power awareness.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from trading.config import RISK, CRYPTO_SYMBOLS
from trading.risk.profit_manager import TAKE_PROFIT_PCT, TRAILING_STOP_ACTIVATE
from trading.risk.volume_gate import compute_volume_ratio, compute_volume_trend, check_spread, check_market_impact
from trading.data.aster import alpaca_to_aster
from trading.db.store import get_positions, get_daily_pnl, get_trades, get_setting, set_setting
from trading.strategy.base import Signal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Crypto exposure cap — total crypto allocation as % of portfolio
# ---------------------------------------------------------------------------
MAX_CRYPTO_EXPOSURE_PCT: float = 0.70  # Max 70% of portfolio in crypto
MAX_CORRELATED_EXPOSURE_PCT: float = 0.50  # Max 50% in a single correlated group
MAX_NET_LONG_EXPOSURE_PCT = 0.80  # Max 80% net long
MAX_NET_SHORT_EXPOSURE_PCT = 0.30  # Max 30% net short

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

# Asset classification for sector exposure limits
CRYPTO_L1 = {"BTC", "ETH", "BTCUSDT", "ETHUSDT"}
CRYPTO_ALTS = {"SOL", "AVAX", "DOT", "LINK", "SOLUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"}
CRYPTO_MEME = {"DOGE", "SHIB", "PEPE", "DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "BONK", "BONKUSDT", "WIF", "WIFUSDT"}
STOCK_PERPS = {"AAPL", "TSLA", "NVDA", "GOOGL", "MSFT", "AMZN", "META"}
COMMODITY_PERPS = {"GOLD", "SILVER", "OIL", "XAUUSDT"}


def _get_atr_stop_pct(symbol: str, leverage: int = 1) -> float | None:
    """Calculate ATR-based stop distance for a symbol, adjusted for leverage.

    Used by portfolio.py to size positions based on risk-per-trade.
    Returns the effective stop distance as a fraction (e.g., 0.035 = 3.5%).
    """
    try:
        from trading.strategy.indicators import atr
        from trading.data.aster import get_aster_ohlcv
        from trading.config import (ASTER_SYMBOLS, CRYPTO_SYMBOLS,
                                     CRYPTO_L1 as CFG_L1, CRYPTO_MEME as CFG_MEME,
                                     STOCK_PERPS as CFG_STOCK, COMMODITY_PERPS as CFG_COMM,
                                     INDEX_PERPS as CFG_IDX)

        # Resolve symbol to AsterDex
        aster_sym = None
        matched_coin_id = None
        for coin_id, alpaca_sym in CRYPTO_SYMBOLS.items():
            if alpaca_sym == symbol or symbol.replace("/", "") in alpaca_sym.replace("/", ""):
                aster_sym = ASTER_SYMBOLS.get(coin_id)
                matched_coin_id = coin_id
                break
        if not aster_sym and symbol.endswith("USDT"):
            aster_sym = symbol

        if not aster_sym:
            return None

        # Per-asset-class ATR multiplier
        if matched_coin_id and matched_coin_id in CFG_MEME:
            atr_mult = 4.0
        elif matched_coin_id and matched_coin_id in CFG_L1:
            atr_mult = 2.5
        elif matched_coin_id and matched_coin_id in (CFG_STOCK + CFG_COMM + CFG_IDX):
            atr_mult = 2.0
        elif matched_coin_id:
            atr_mult = 3.0
        else:
            atr_mult = 2.0

        df = get_aster_ohlcv(aster_sym, interval="1h", limit=50)
        if df is None or df.empty or len(df) < 14:
            return None

        atr_series = atr(df["high"], df["low"], df["close"], period=14)
        atr_value = float(atr_series.iloc[-1])
        entry_price = float(df["close"].iloc[-1])

        if atr_value <= 0 or entry_price <= 0:
            return None

        base_sl_pct = (atr_mult * atr_value) / entry_price

        # Tighten for leverage (sqrt scaling)
        if leverage > 1:
            effective_sl_pct = base_sl_pct / math.sqrt(leverage)
        else:
            effective_sl_pct = base_sl_pct

        # Clamp between 1.5% and 15%
        effective_sl_pct = max(0.015, min(0.15, effective_sl_pct))

        return effective_sl_pct
    except Exception:
        return None

SECTOR_LIMITS = {"l1": 0.50, "alts": 0.20, "defi": 0.25, "meme": 0.10, "stocks": 0.20, "commodities": 0.15}

def _get_asset_sector(symbol: str) -> str:
    sym = symbol.upper().replace("USDT","").replace("USD","").replace("/","")
    if sym in {"BTC","ETH"}: return "l1"
    if sym in {"SOL","AVAX","DOT","LINK","ADA","NEAR","SUI","APT"}: return "alts"
    if sym in {"DOGE","SHIB","PEPE","BONK","WIF","TRUMP"}: return "meme"
    if sym in {"AAPL","TSLA","NVDA","GOOGL","MSFT","AMZN","META"}: return "stocks"
    if sym in {"GOLD","SILVER","OIL","XAU","XAG"}: return "commodities"
    return "other"

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
        """Run all risk checks on a proposed trade. Returns RiskCheck.

        Checks are evaluated lazily — the first failure short-circuits so
        later checks (which may need DB access) are never called.
        """
        # Fetch positions ONCE — avoids N+1 queries across sub-checks
        try:
            positions = get_positions()
        except Exception:
            positions = []

        checks = [
            lambda: self._check_account_status(),
            lambda: self._check_buying_power(order_value),
            lambda: self._check_volume(signal),
            lambda: self._check_liquidity(signal, order_value),
            lambda: self._check_open_positions_cap(positions),
            lambda: self._check_same_strategy_cap(signal, positions),
            lambda: self._check_position_size(signal, order_value, positions),
            lambda: self._check_crypto_exposure(signal, order_value, positions),
            lambda: self._check_correlation_group(signal, order_value, positions),
            lambda: self._check_directional_exposure(signal, order_value, positions),
            lambda: self._check_daily_loss(),
            lambda: self._check_max_drawdown(),
            lambda: self._check_cash_reserve(order_value, positions),
            lambda: self._check_trade_count(),
            lambda: self._check_total_leverage_risk(signal, order_value),
            lambda: self._check_sector_exposure(signal, order_value),
        ]
        for check_fn in checks:
            check = check_fn()
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
    # Volume gate — only trade assets with adequate volume
    # ------------------------------------------------------------------

    def _check_volume(self, signal: Signal) -> RiskCheck:
        """Block buy entries when volume is too low or fading.

        Checks both absolute volume level AND volume trend (building vs fading).
        Only applies to buy signals — sells/exits are never blocked.
        Fails open: if data is unavailable, the gate passes.
        """
        if signal.action != "buy":
            return RiskCheck(allowed=True, reason="Not a buy — volume gate skipped", signal=signal)

        min_ratio = self.rules.get("min_volume_ratio", 0.30)
        aster_sym = alpaca_to_aster(signal.symbol)
        if not aster_sym:
            return RiskCheck(allowed=True, reason="No AsterDex symbol — volume gate skipped", signal=signal)

        ratio = compute_volume_ratio(aster_sym)
        if ratio is None:
            return RiskCheck(allowed=True, reason="Volume data unavailable — gate passed", signal=signal)

        if ratio < min_ratio:
            return RiskCheck(
                allowed=False,
                reason=f"Volume too low: {signal.symbol} at {ratio:.0%} of average (minimum {min_ratio:.0%})",
                signal=signal,
            )

        # Check volume trend — block if volume is rapidly fading even if still above threshold
        trend = compute_volume_trend(aster_sym)
        if trend is not None and trend < -0.5 and ratio < 0.6:
            return RiskCheck(
                allowed=False,
                reason=(
                    f"Volume fading fast: {signal.symbol} at {ratio:.0%} of average "
                    f"and declining {abs(trend):.0%} — move is dying"
                ),
                signal=signal,
            )

        return RiskCheck(allowed=True, reason=f"Volume OK: {ratio:.0%} of average, trend {trend or 0:+.0%}", signal=signal)

    def _check_liquidity(self, signal: Signal, order_value: float) -> RiskCheck:
        """Block entries when spread is too wide or order would move the market.

        Wide spread = bad fill quality. Large order relative to volume = market impact.
        Only applies to buys. Fails open on data errors.
        """
        if signal.action != "buy":
            return RiskCheck(allowed=True, reason="Not a buy — liquidity check skipped", signal=signal)

        aster_sym = alpaca_to_aster(signal.symbol)
        if not aster_sym:
            return RiskCheck(allowed=True, reason="No AsterDex symbol — liquidity check skipped", signal=signal)

        # Check spread — block if spread is too wide (> 50 bps = 0.5%)
        max_spread_bps = self.rules.get("max_spread_bps", 50)
        spread = check_spread(aster_sym)
        if spread is not None and spread > max_spread_bps:
            return RiskCheck(
                allowed=False,
                reason=f"Spread too wide: {signal.symbol} at {spread:.0f} bps (max {max_spread_bps} bps)",
                signal=signal,
            )

        # Check market impact — block if order is > 1% of recent 4h quote volume
        max_impact = self.rules.get("max_market_impact_pct", 0.01)
        impact_ok = check_market_impact(aster_sym, order_value, max_impact)
        if impact_ok is not None and not impact_ok:
            return RiskCheck(
                allowed=False,
                reason=f"Market impact too high: ${order_value:.0f} order is > {max_impact:.0%} of recent volume for {signal.symbol}",
                signal=signal,
            )

        return RiskCheck(allowed=True, reason="Liquidity OK", signal=signal)

    # ------------------------------------------------------------------
    # Hard caps (NEW — prevent pile-on)
    # ------------------------------------------------------------------

    def _check_open_positions_cap(self, positions: list[dict]) -> RiskCheck:
        """Block new entries if total open positions >= max_open_positions.

        This prevents the scenario (March 25) where 6+ simultaneous altcoin
        shorts all moved against us in the same cycle.
        """
        max_positions = self.rules.get("max_open_positions", 6)
        if signal := None:  # noqa: confusing but we need a dummy signal
            pass
        dummy = Signal("risk", "", "hold", 0, "")
        open_count = len([p for p in positions if p.get("qty", 0) != 0])
        if open_count >= max_positions:
            return RiskCheck(
                allowed=False,
                reason=f"Open position cap reached: {open_count}/{max_positions} positions open. "
                       "Close existing positions before opening new ones.",
                signal=dummy,
            )
        return RiskCheck(allowed=True, reason=f"Open positions: {open_count}/{max_positions} OK", signal=dummy)

    def _check_same_strategy_cap(self, signal: Signal, positions: list[dict]) -> RiskCheck:
        """Block a strategy from opening more than max_same_strategy_positions trades.

        Prevents a single strategy (e.g. multi_factor_rank) from opening
        many correlated positions that fail together.
        """
        max_same = self.rules.get("max_same_strategy_positions", 3)
        strategy = signal.strategy or ""
        if not strategy or strategy in ("risk", "aggregator"):
            return RiskCheck(allowed=True, reason="Strategy cap: no limit for aggregator", signal=signal)
        try:
            from trading.db.store import get_trades
            open_trades = get_trades(limit=200)
            same_strategy_open = [
                t for t in open_trades
                if t.get("strategy") == strategy
                and t.get("status") in ("filled", "pending")
                and not t.get("closed_at")
            ]
            if len(same_strategy_open) >= max_same:
                return RiskCheck(
                    allowed=False,
                    reason=f"Strategy '{strategy}' already has {len(same_strategy_open)} open positions "
                           f"(max {max_same}). Prevents correlated pile-on.",
                    signal=signal,
                )
        except Exception:
            pass
        return RiskCheck(allowed=True, reason=f"Strategy cap OK for {strategy}", signal=signal)

    # ------------------------------------------------------------------
    # Position-level checks
    # ------------------------------------------------------------------

    def _check_position_size(self, signal: Signal, order_value: float,
                             positions: list[dict]) -> RiskCheck:
        max_pct = self.rules["max_position_pct"]
        # Stage 1 risk: reduce max position by 20%
        try:
            stage = int(get_setting("risk_stage", "0") or "0")
            if stage >= 1:
                max_pct *= 0.80
        except Exception:
            pass
        max_value = self.portfolio_value * max_pct
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

    def _check_directional_exposure(self, signal: Signal, order_value: float,
                                     positions: list[dict]) -> RiskCheck:
        """Check aggregate net directional exposure doesn't exceed limits."""
        # Normalize positions: compute market_value and side if missing (paper mode compat)
        def _pos_market_value(p):
            mv = p.get("market_value")
            if mv:
                return mv
            qty = p.get("qty", 0)
            price = p.get("current_price") or p.get("avg_cost", 0)
            return qty * price

        def _pos_side(p):
            side = p.get("side")
            if side:
                return side.lower()
            return "long" if p.get("qty", 0) >= 0 else "short"

        long_value = sum(
            abs(_pos_market_value(p)) for p in positions
            if _pos_side(p) == "long"
        )
        short_value = sum(
            abs(_pos_market_value(p)) for p in positions
            if _pos_side(p) == "short"
        )

        if signal.action == "buy":
            long_value += order_value
        elif signal.action == "sell":
            short_value += order_value

        net_exposure = long_value - short_value
        net_pct = net_exposure / self.portfolio_value if self.portfolio_value > 0 else 0

        if net_pct > MAX_NET_LONG_EXPOSURE_PCT:
            return RiskCheck(
                allowed=False,
                reason=f"Net long exposure {net_pct:.0%} exceeds {MAX_NET_LONG_EXPOSURE_PCT:.0%} cap",
                signal=signal,
            )
        if net_pct < -MAX_NET_SHORT_EXPOSURE_PCT:
            return RiskCheck(
                allowed=False,
                reason=f"Net short exposure {abs(net_pct):.0%} exceeds {MAX_NET_SHORT_EXPOSURE_PCT:.0%} cap",
                signal=signal,
            )

        return RiskCheck(allowed=True, reason="Directional exposure OK", signal=signal)

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
        # Recovery mode is the explicit acknowledgment of the drawdown — bypass the check
        # so conservative trading can proceed after the operator resumes.
        try:
            from trading.strategy.circuit_breaker import get_recovery_mode
            if get_recovery_mode().get("active"):
                return RiskCheck(allowed=True, reason="Drawdown check bypassed — recovery mode active", signal=Signal("risk", "", "hold", 0, ""))
        except Exception:
            pass
        pnl_records = get_daily_pnl(limit=90)
        if len(pnl_records) < 2:
            return RiskCheck(allowed=True, reason="Insufficient data for drawdown check", signal=Signal("risk", "", "hold", 0, ""))
        peak = max(r["portfolio_value"] for r in pnl_records)
        current = pnl_records[0]["portfolio_value"]
        drawdown = (peak - current) / peak if peak > 0 else 0

        # --- Graduated risk response ---
        stage_2_threshold = self.rules.get("drawdown_stage2_pct", 0.08)   # 8%
        stage_1_threshold = self.rules.get("drawdown_stage1_pct", 0.05)   # 5%

        if drawdown > self.rules["max_drawdown_pct"]:
            set_setting("risk_stage", "3")
            return RiskCheck(
                allowed=False,
                reason=f"Drawdown {drawdown*100:.1f}% exceeds max {self.rules['max_drawdown_pct']*100:.0f}% — ALL trading halted",
                signal=Signal("risk", "", "hold", 0, ""),
            )
        elif drawdown > stage_2_threshold:
            # Stage 2: conservative mode behaviour (existing circuit breaker handles full logic)
            set_setting("risk_stage", "2")
        elif drawdown > stage_1_threshold:
            # Stage 1: tighten confluence, reduce max position by 20%
            set_setting("risk_stage", "1")
        else:
            set_setting("risk_stage", "0")

        return RiskCheck(allowed=True, reason=f"Drawdown {drawdown*100:.1f}% OK (stage {get_setting('risk_stage','0')})", signal=Signal("risk", "", "hold", 0, ""))

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
    # Leverage & sector checks
    # ------------------------------------------------------------------

    def _check_total_leverage_risk(self, signal: Signal, order_value: float) -> RiskCheck:
        """Cap aggregate portfolio leverage at 5x."""
        new_leverage = 1
        if signal.data and isinstance(signal.data, dict):
            new_leverage = signal.data.get("leverage", 1)
        total_notional = order_value * new_leverage
        # Add existing positions' leveraged exposure — fail CLOSED on error
        try:
            from trading.execution.router import get_positions_from_aster
            for pos in get_positions_from_aster():
                pos_lev = pos.get("leverage", 1)
                total_notional += abs(pos.get("market_value", 0)) * pos_lev
        except Exception as e:
            log.error("Leverage check: failed to fetch positions, blocking trade: %s", e)
            return RiskCheck(
                allowed=False,
                reason=f"Cannot verify total leverage — position fetch failed: {e}",
                signal=signal,
            )
        if self.portfolio_value > 0 and total_notional / self.portfolio_value > 5.0:
            return RiskCheck(
                allowed=False,
                reason=f"Total leverage {total_notional/self.portfolio_value:.1f}x exceeds 5x cap",
                signal=signal,
            )
        return RiskCheck(allowed=True, reason="Total leverage OK", signal=signal)

    def _check_sector_exposure(self, signal: Signal, order_value: float) -> RiskCheck:
        """Check sector exposure limits."""
        sector = _get_asset_sector(signal.symbol)
        if sector == "other":
            return RiskCheck(allowed=True, reason="No sector limit", signal=signal)
        limit = SECTOR_LIMITS.get(sector, 1.0)
        # Sum existing exposure in same sector
        sector_exposure = order_value
        try:
            from trading.execution.router import get_positions_from_aster
            for pos in get_positions_from_aster():
                if _get_asset_sector(pos.get("symbol", "")) == sector:
                    sector_exposure += abs(pos.get("market_value", 0))
        except Exception:
            pass
        if self.portfolio_value > 0 and sector_exposure / self.portfolio_value > limit:
            return RiskCheck(
                allowed=False,
                reason=f"Sector '{sector}' exposure {sector_exposure/self.portfolio_value:.1%} exceeds {limit:.0%} limit",
                signal=signal,
            )
        return RiskCheck(allowed=True, reason=f"Sector '{sector}' exposure OK", signal=signal)

    # ------------------------------------------------------------------
    # Stop-loss check (unchanged API, uses RISK config)
    # ------------------------------------------------------------------

    def check_stop_loss(self, symbol: str, current_price: float) -> RiskCheck | None:
        """Check if a position should be stopped out."""
        positions = get_positions()
        for pos in positions:
            if pos["symbol"] == symbol:
                avg_cost = pos["avg_cost"]
                if avg_cost <= 0:
                    continue
                qty = pos.get("qty", 0)
                is_short = qty < 0 or pos.get("side", "").lower() == "short"
                if is_short:
                    # Short: loss when price goes UP
                    loss_pct = (avg_cost - current_price) / avg_cost
                else:
                    # Long: loss when price goes DOWN
                    loss_pct = (current_price - avg_cost) / avg_cost
                if loss_pct <= -self.rules["stop_loss_pct"]:
                    close_action = "buy" if is_short else "sell"
                    return RiskCheck(
                        allowed=False,
                        reason=f"Stop loss triggered: {symbol} ({'short' if is_short else 'long'}) at {loss_pct*100:.1f}% loss (threshold: -{self.rules['stop_loss_pct']*100:.0f}%)",
                        signal=Signal("risk", symbol, close_action, 1.0, f"Stop loss at {loss_pct*100:.1f}%"),
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


# ---------------------------------------------------------------------------
# Trade target computation — SL/TP must be set before any trade
# ---------------------------------------------------------------------------

@dataclass
class TradeTargets:
    """Pre-computed stop loss and take profit levels for a trade."""
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    trailing_stop_activate_price: float
    risk_reward_ratio: float
    max_loss_pct: float
    max_gain_pct: float
    max_loss_value: float      # Dollar loss at SL for the given order size
    max_gain_value: float      # Dollar gain at TP for the given order size


def compute_trade_targets(
    symbol: str,
    entry_price: float,
    order_value: float,
    signal_strength: float = 0.5,
    leverage: int = 1,
) -> TradeTargets:
    """Compute stop loss and take profit targets before entering a trade.

    The SL is ATR-based when data is available, with fallback to RISK['stop_loss_pct'].
    Adjustments for:
      - ATR volatility (2x ATR as base stop distance)
      - Leveraged ETFs (tighter SL to account for amplified moves)
      - Signal strength (stronger signals get slightly wider stops)
      - Leverage (tighter stops to avoid liquidation)

    The TP uses profit_manager.TAKE_PROFIT_PCT with a minimum 2:1 R:R ratio.
    """
    base_sl_pct = RISK["stop_loss_pct"]
    etf_leverage = LEVERAGE_FACTORS.get(symbol, 1.0)

    # Try ATR-based stop distance with per-asset-class multiplier
    try:
        from trading.strategy.indicators import atr
        from trading.data.aster import get_aster_ohlcv
        from trading.config import (ASTER_SYMBOLS, CRYPTO_SYMBOLS,
                                     CRYPTO_L1, CRYPTO_MEME,
                                     STOCK_PERPS, COMMODITY_PERPS, INDEX_PERPS)
        # Resolve to AsterDex symbol and determine coin_id
        aster_sym = None
        matched_coin_id = None
        for coin_id, alpaca_sym in CRYPTO_SYMBOLS.items():
            if alpaca_sym == symbol or symbol.replace("/", "") in alpaca_sym.replace("/", ""):
                aster_sym = ASTER_SYMBOLS.get(coin_id)
                matched_coin_id = coin_id
                break
        if not aster_sym and symbol.endswith("USDT"):
            aster_sym = symbol

        # Per-asset-class ATR multiplier:
        #   2.5x — crypto majors (BTC, ETH, SOL, etc.)
        #   3.0x — crypto alts (L2, DeFi, AI)
        #   4.0x — meme coins (high volatility)
        #   2.0x — stocks, commodities, indices
        if matched_coin_id and matched_coin_id in CRYPTO_MEME:
            atr_mult = 4.0
        elif matched_coin_id and matched_coin_id in CRYPTO_L1:
            atr_mult = 2.5
        elif matched_coin_id and matched_coin_id in (STOCK_PERPS + COMMODITY_PERPS + INDEX_PERPS):
            atr_mult = 2.0
        elif matched_coin_id:
            atr_mult = 3.0  # alts (L2, DeFi, AI)
        else:
            atr_mult = 2.0  # default for unknown

        if aster_sym:
            df = get_aster_ohlcv(aster_sym, interval="1h", limit=50)
            if df is not None and not df.empty and len(df) >= 14:
                atr_series = atr(df["high"], df["low"], df["close"], period=14)
                atr_value = float(atr_series.iloc[-1])
                if atr_value > 0 and entry_price > 0:
                    base_sl_pct = (atr_mult * atr_value) / entry_price
    except Exception:
        pass  # Fall back to config-based stop

    # Tighter stops for leveraged instruments (their moves are amplified)
    effective_sl_pct = base_sl_pct / etf_leverage

    # Adjust for trade leverage — tighter stops to preserve margin
    if leverage > 1:
        effective_sl_pct /= math.sqrt(leverage)

    # Strong signals get slightly wider stops (up to +20% wider)
    strength_adj = 1.0 + (signal_strength - 0.5) * 0.4  # 0.8x to 1.2x
    effective_sl_pct *= strength_adj

    # Clamp SL between 1.5% and 15%
    effective_sl_pct = max(0.015, min(effective_sl_pct, 0.15))

    # Take profit — at least 3:1 reward-to-risk ratio
    # At 55% win rate, 3:1 R:R gives positive expectancy:
    # EV = 0.55 * 3R - 0.45 * 1R = +1.2R per trade
    base_tp_pct = TAKE_PROFIT_PCT
    min_tp_from_rr = effective_sl_pct * 3.0  # 3:1 R:R minimum (was 2:1)
    effective_tp_pct = max(base_tp_pct, min_tp_from_rr)

    # Trailing stop activation
    trailing_activate_pct = TRAILING_STOP_ACTIVATE

    # Compute price levels
    stop_loss_price = entry_price * (1 - effective_sl_pct)
    take_profit_price = entry_price * (1 + effective_tp_pct)
    trailing_stop_activate_price = entry_price * (1 + trailing_activate_pct)

    # Risk/reward ratio
    risk = effective_sl_pct
    reward = effective_tp_pct
    rr_ratio = reward / risk if risk > 0 else 0

    # Dollar values
    qty_estimate = order_value / entry_price if entry_price > 0 else 0
    max_loss_value = qty_estimate * (entry_price - stop_loss_price)
    max_gain_value = qty_estimate * (take_profit_price - entry_price)

    return TradeTargets(
        entry_price=round(entry_price, 2),
        stop_loss_price=round(stop_loss_price, 2),
        take_profit_price=round(take_profit_price, 2),
        trailing_stop_activate_price=round(trailing_stop_activate_price, 2),
        risk_reward_ratio=round(rr_ratio, 2),
        max_loss_pct=round(effective_sl_pct * 100, 2),
        max_gain_pct=round(effective_tp_pct * 100, 2),
        max_loss_value=round(max_loss_value, 2),
        max_gain_value=round(max_gain_value, 2),
    )
