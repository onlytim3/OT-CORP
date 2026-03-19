"""Portfolio allocation — dynamic sizing based on confluence, regime, and performance.

v3: Replaces fixed strategy budgets with dynamic allocation that scales
    with strategy confirmations, confluence strength, regime alignment,
    and recent win rate. Capital flows toward high-conviction, multi-strategy
    confluences and away from isolated, unconfirmed signals.

Allocation factors (applied multiplicatively):
  1. Base budget       — per-strategy floor allocation
  2. Confluence boost  — more strategies agreeing = bigger position
  3. Regime alignment  — intelligence briefing confirms direction = boost
  4. Performance tilt  — recent win rate shifts budget toward winners
  5. Volatility scale  — ATR-based inverse vol sizing
  6. Signal strength   — raw confidence from aggregator
"""

import logging

import numpy as np

from trading.config import RISK
from trading.db.store import get_positions, get_trades
from trading.strategy.base import Signal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base per-strategy risk budget — floor allocation (dynamic factors scale UP)
# ---------------------------------------------------------------------------
STRATEGY_BUDGETS: dict[str, float] = {
    # Core crypto strategies (proven — higher base)
    "rsi_divergence": 0.05,
    "hmm_regime": 0.06,
    "pairs_trading": 0.05,
    "kalman_trend": 0.06,
    "regime_mean_reversion": 0.05,
    "factor_crypto": 0.04,
    # Perps-specific strategies (AsterDex alpha)
    "funding_arb": 0.05,
    "microstructure_composite": 0.04,
    "basis_zscore": 0.04,
    "funding_term_structure": 0.04,
    "taker_divergence": 0.04,
    "cross_basis_rv": 0.04,
    "oi_price_divergence": 0.04,
    "whale_flow": 0.03,
    # Cross-asset strategies
    "cross_asset_momentum": 0.03,
    "gold_crypto_hedge": 0.03,
    "equity_crypto_correlation": 0.03,
    # Advanced strategies
    "multi_factor_rank": 0.03,
    "volatility_regime": 0.03,
    "meme_momentum": 0.02,
}

DEFAULT_BUDGET = 0.03  # Fallback for unknown strategies

# ---------------------------------------------------------------------------
# Confluence configuration
# ---------------------------------------------------------------------------

# How much to boost per additional confirming strategy
CONFLUENCE_MULTIPLIERS = {
    1: 0.4,    # Single strategy — reduced conviction
    2: 1.0,    # Two strategies agree — base allocation
    3: 1.8,    # Three agree — strong confluence
    4: 2.5,    # Four agree — very strong confluence
}
CONFLUENCE_MAX_MULT = 3.0  # Cap for 5+ strategies confirming

# Regime alignment boost
REGIME_ALIGNMENT_BOOST = 1.25   # Intelligence briefing confirms direction
REGIME_MISALIGN_PENALTY = 0.7   # Intelligence briefing contradicts direction
EVENT_RISK_DAMPENER = 0.6       # Reduce sizing during event risk

# Performance tilt
PERF_LOOKBACK_TRADES = 30       # Recent trades to evaluate
PERF_MIN_TRADES = 5             # Minimum trades before tilting
PERF_HIGH_WR_BOOST = 1.3       # Win rate > 60%
PERF_LOW_WR_PENALTY = 0.6      # Win rate < 30%

# ---------------------------------------------------------------------------
# Internal caches (cleared each cycle by data.cache.clear_cache)
# ---------------------------------------------------------------------------
_perf_cache: dict[str, float] = {}
_regime_cache: dict | None = None


def _get_strategy_budget(strategy_name: str) -> float:
    """Return the base risk budget fraction for a strategy."""
    return STRATEGY_BUDGETS.get(strategy_name, DEFAULT_BUDGET)


# ---------------------------------------------------------------------------
# Confluence scoring
# ---------------------------------------------------------------------------

def _confluence_multiplier(signal: Signal) -> float:
    """Compute allocation multiplier based on how many strategies confirmed this signal.

    More strategies agreeing on the same symbol+direction = higher conviction.
    Single-strategy signals get reduced allocation (0.6x).
    """
    data = signal.data or {}
    signal_count = data.get("signal_count", 1)

    # If this came from the aggregator, it has contributing_strategies
    contributing = data.get("contributing_strategies", [])
    count = max(len(contributing), signal_count, 1)

    mult = CONFLUENCE_MULTIPLIERS.get(count, CONFLUENCE_MAX_MULT)

    if count > 1:
        log.debug(
            "Confluence for %s: %d strategies agree → %.1fx",
            signal.symbol, count, mult,
        )

    return mult


# ---------------------------------------------------------------------------
# Regime alignment scoring
# ---------------------------------------------------------------------------

def _regime_alignment_multiplier(signal: Signal) -> float:
    """Check if the market intelligence briefing confirms or contradicts this signal.

    A buy signal in a bullish regime gets boosted.
    A buy signal in a bearish regime gets penalized.
    Event risk dampens all sizing.
    """
    global _regime_cache

    # Lazy-load the latest briefing
    if _regime_cache is None:
        try:
            from trading.intelligence.engine import generate_briefing
            briefing = generate_briefing()
            _regime_cache = {
                "categories": briefing.categories,
                "overall_score": briefing.overall_score,
                "event_risk": briefing.has_event_risk(),
            }
        except Exception:
            _regime_cache = {"categories": {}, "overall_score": 0.0, "event_risk": False}

    # Event risk dampens everything
    if _regime_cache.get("event_risk"):
        log.debug("Event risk active → dampening sizing to %.1fx", EVENT_RISK_DAMPENER)
        return EVENT_RISK_DAMPENER

    # Determine which category this signal falls into
    symbol = signal.symbol or ""
    if any(tok in symbol.upper() for tok in ["BTC", "ETH", "SOL", "DOGE", "XRP",
                                              "AVAX", "DOT", "LINK", "UNI", "AAVE",
                                              "PEPE", "BONK", "SHIB", "TRUMP"]):
        category = "crypto"
    elif any(tok in symbol.upper() for tok in ["XAU", "XAG", "XCU", "XPT", "NATGAS", "PAXG"]):
        category = "commodities"
    elif any(tok in symbol.upper() for tok in ["SPX", "QQQ", "AAPL", "MSFT", "NVDA",
                                                "TSLA", "GOOG", "META", "AMZN"]):
        # Use macro for equities
        category = "macro"
    else:
        category = "crypto"  # Default for crypto-majority portfolio

    cat_data = _regime_cache.get("categories", {}).get(category, {})
    regime_score = cat_data.get("score", 0.0)
    confidence = cat_data.get("confidence", 0.0)

    # Only apply regime alignment when confidence is meaningful
    if confidence < 0.3:
        return 1.0  # Low confidence — no adjustment

    # Check alignment: buy in bullish regime or sell in bearish regime
    if signal.action == "buy":
        if regime_score > 0.2:
            log.debug(
                "Regime aligned: buy %s in bullish %s (%.2f) → %.1fx",
                signal.symbol, category, regime_score, REGIME_ALIGNMENT_BOOST,
            )
            return REGIME_ALIGNMENT_BOOST
        elif regime_score < -0.2:
            log.debug(
                "Regime misaligned: buy %s in bearish %s (%.2f) → %.1fx",
                signal.symbol, category, regime_score, REGIME_MISALIGN_PENALTY,
            )
            return REGIME_MISALIGN_PENALTY
    elif signal.action == "sell":
        if regime_score < -0.2:
            return REGIME_ALIGNMENT_BOOST  # Sell in bearish = aligned
        elif regime_score > 0.2:
            return REGIME_MISALIGN_PENALTY  # Sell in bullish = misaligned

    return 1.0


# ---------------------------------------------------------------------------
# Performance tilt
# ---------------------------------------------------------------------------

def _performance_multiplier(strategy_name: str) -> float:
    """Tilt allocation toward strategies with recent positive performance.

    Strategies with >60% win rate get boosted.
    Strategies with <30% win rate get penalized.
    New strategies (< 5 trades) get no adjustment.
    """
    global _perf_cache

    if strategy_name in _perf_cache:
        return _perf_cache[strategy_name]

    try:
        trades = get_trades(limit=500)
        strat_trades = [
            t for t in trades
            if (t.get("strategy") or "").startswith(strategy_name)
            and t.get("pnl") is not None
        ]

        # Only the most recent N trades
        recent = strat_trades[:PERF_LOOKBACK_TRADES]

        if len(recent) < PERF_MIN_TRADES:
            _perf_cache[strategy_name] = 1.0
            return 1.0

        wins = sum(1 for t in recent if t["pnl"] > 0)
        win_rate = wins / len(recent)

        if win_rate >= 0.60:
            mult = PERF_HIGH_WR_BOOST
        elif win_rate <= 0.30:
            mult = PERF_LOW_WR_PENALTY
        else:
            # Linear interpolation between penalty and boost
            # 0.30 → 0.6x, 0.45 → 1.0x, 0.60 → 1.3x
            if win_rate < 0.45:
                mult = PERF_LOW_WR_PENALTY + (win_rate - 0.30) / 0.15 * (1.0 - PERF_LOW_WR_PENALTY)
            else:
                mult = 1.0 + (win_rate - 0.45) / 0.15 * (PERF_HIGH_WR_BOOST - 1.0)

        log.debug(
            "Performance tilt for %s: %d/%d wins (%.0f%%) → %.2fx",
            strategy_name, wins, len(recent), win_rate * 100, mult,
        )
        _perf_cache[strategy_name] = round(mult, 3)
        return _perf_cache[strategy_name]

    except Exception as e:
        log.debug("Performance tilt failed for %s: %s", strategy_name, e)
        _perf_cache[strategy_name] = 1.0
        return 1.0


# ---------------------------------------------------------------------------
# Volatility estimation (unchanged from v2)
# ---------------------------------------------------------------------------

def _estimate_volatility(symbol: str) -> float:
    """Estimate annualized volatility using recent price data.

    Returns a volatility multiplier:
      - Low vol (< 30% annual) → 1.2 (size up)
      - Normal vol (30-60%)    → 1.0 (normal)
      - High vol (60-100%)     → 0.7 (size down)
      - Very high vol (>100%)  → 0.4 (minimal size)
    """
    try:
        if "/" in symbol:
            from trading.data.crypto import get_ohlc
            from trading.config import CRYPTO_SYMBOLS
            reverse_map = {v: k for k, v in CRYPTO_SYMBOLS.items()}
            coin_id = reverse_map.get(symbol)
            if not coin_id:
                return 1.0
            df = get_ohlc(coin_id, days=14)
            if df.empty or len(df) < 5:
                return 1.0
            closes = df["close"].values
        else:
            from trading.data.commodities import get_etf_history
            df = get_etf_history(symbol, period="1mo")
            if df.empty or len(df) < 5:
                return 1.0
            if "Close" in df.columns:
                closes = df["Close"].values
            elif "close" in df.columns:
                closes = df["close"].values
            else:
                return 1.0

        returns = np.diff(closes) / closes[:-1]
        daily_vol = np.std(returns)

        ann_factor = np.sqrt(365) if "/" in symbol else np.sqrt(252)
        annual_vol = daily_vol * ann_factor

        if annual_vol < 0.30:
            return 1.2
        elif annual_vol < 0.60:
            return 1.0
        elif annual_vol < 1.00:
            return 0.7
        else:
            return 0.4

    except Exception as e:
        log.debug("Volatility estimation failed for %s: %s", symbol, e)
        return 1.0


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def clear_allocation_cache():
    """Clear per-cycle caches. Called at the start of each trading cycle."""
    global _perf_cache, _regime_cache
    _perf_cache = {}
    _regime_cache = None


# ---------------------------------------------------------------------------
# Correlation group penalty — reduce sizing when group is already heavy
# ---------------------------------------------------------------------------

def _correlation_group_multiplier(signal, positions, portfolio_value):
    try:
        from trading.risk.manager import CORRELATION_GROUPS
    except ImportError:
        return 1.0
    strategy = signal.strategy.split("+")[0] if hasattr(signal, 'strategy') else ""
    my_group = None
    for group, strats in CORRELATION_GROUPS.items():
        if strategy in strats:
            my_group = group
            break
    if not my_group:
        return 1.0
    group_strats = CORRELATION_GROUPS[my_group]
    group_exposure = 0
    for pos in (positions or []):
        if any(s in (pos.get("strategy","") or "") for s in group_strats):
            group_exposure += abs(pos.get("market_value", 0))
    if portfolio_value <= 0:
        return 1.0
    return max(0.5, 1.0 - (group_exposure / portfolio_value) / 0.50 * 0.5)


# ---------------------------------------------------------------------------
# Drawdown multiplier — reduce sizing during drawdowns
# ---------------------------------------------------------------------------

def _drawdown_multiplier(portfolio_value):
    try:
        from trading.db.store import get_daily_pnl
        pnl_data = get_daily_pnl(limit=90)
        if not pnl_data:
            return 1.0
        peak = max(d.get("portfolio_value", 0) for d in pnl_data)
        if peak <= 0:
            return 1.0
        drawdown = (peak - portfolio_value) / peak
        if drawdown <= 0.05: return 1.0
        elif drawdown <= 0.10: return 0.75
        elif drawdown <= 0.15: return 0.5
        elif drawdown <= 0.20: return 0.25
        else: return 0.0  # halt
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# Main sizing function
# ---------------------------------------------------------------------------

def calculate_order_size(
    signal: Signal,
    portfolio_value: float,
    num_signals: int = 1,
    use_vol_sizing: bool = True,
    use_strategy_budgets: bool = True,
) -> float:
    """Calculate how much to allocate to a trade — dynamic based on confluence.

    Allocation factors (multiplicative):
      1. Base budget         — per-strategy floor from STRATEGY_BUDGETS
      2. Confluence boost    — 1 strategy=0.6x, 2=1.0x, 3=1.4x, 4=1.7x, 5+=2.0x
      3. Regime alignment    — intelligence confirms direction = 1.25x, contradicts = 0.7x
      4. Performance tilt    — recent win rate > 60% = 1.3x, < 30% = 0.6x
      5. Volatility scale    — ATR-based inverse vol (0.4x to 1.2x)
      6. Signal strength     — raw aggregated confidence (0.0 to 1.0)
      7. Hard caps           — max_position_pct, free capital, strategy budget

    Returns dollar amount to trade, or 0.0 if too small.
    """
    max_per_position = portfolio_value * RISK["max_position_pct"]
    cash_reserve = portfolio_value * RISK["min_cash_reserve_pct"]
    available = portfolio_value - cash_reserve

    # Check existing positions
    positions = get_positions()
    positions_value = sum(
        p["qty"] * (p["current_price"] or p["avg_cost"]) for p in positions
    )
    free_capital = available - positions_value

    if free_capital <= 0:
        return 0.0

    # Extract base strategy name for budget lookup
    base_strategy = signal.strategy.split("+")[0] if signal.strategy else ""
    # For aggregator signals, use the first contributing strategy
    if base_strategy == "aggregator":
        contributing = (signal.data or {}).get("contributing_strategies", [])
        base_strategy = contributing[0] if contributing else "aggregator"

    # --- Factor 1: Base budget ---
    if use_strategy_budgets:
        budget_pct = _get_strategy_budget(base_strategy)
        strategy_cap = portfolio_value * budget_pct

        # How much has this strategy already deployed?
        strategy_deployed = sum(
            p["qty"] * (p["current_price"] or p["avg_cost"])
            for p in positions
            if (p.get("strategy") or "").startswith(base_strategy)
        )
        strategy_remaining = max(strategy_cap - strategy_deployed, 0)
    else:
        strategy_remaining = free_capital

    # Effective ceiling = min(free capital, strategy budget remaining, max position)
    ceiling = min(free_capital, strategy_remaining, max_per_position)
    if ceiling <= 0:
        log.debug("No budget remaining for %s", signal.strategy)
        return 0.0

    # Per-signal allocation: the ceiling already incorporates the strategy's
    # budget cap, so we only split free_capital (not strategy budget) among
    # concurrent buy signals to prevent over-deployment.
    # This ensures small portfolios don't produce sub-minimum orders.
    if use_strategy_budgets and strategy_remaining < free_capital:
        # Strategy budget is the binding constraint — no need to split further
        # since each strategy's budget is independent
        per_signal = ceiling
    else:
        # Free capital is the constraint — split among all buy signals
        per_signal = ceiling / max(num_signals, 1)

    # =====================================================================
    # RISK-BASED POSITION SIZING (v4)
    # Core logic: size the position so that hitting the stop loss = 1% loss
    #
    # Formula:
    #   risk_amount = portfolio × RISK_PER_TRADE_PCT (default 1%)
    #   stop_distance_pct = ATR-based stop distance (from compute_trade_targets)
    #   position_notional = risk_amount / stop_distance_pct
    #   (This is the leveraged notional — the actual margin used is notional/leverage)
    #
    # The old budget-based system produced bite-sized orders that didn't
    # take advantage of leverage. This approach sizes to the stop, meaning
    # leverage amplifies gains while risk stays fixed at 1% of portfolio.
    # =====================================================================

    # Get leverage for this strategy
    leverage = 1
    if hasattr(signal, 'data') and isinstance(signal.data, dict):
        leverage = signal.data.get("leverage", 1)
    if leverage < 1:
        leverage = 1

    # Risk per trade: 1% of portfolio (configurable)
    risk_pct = RISK.get("risk_per_trade_pct", 0.01)
    risk_amount = portfolio_value * risk_pct

    # Get ATR-based stop distance
    stop_distance_pct = RISK.get("stop_loss_pct", 0.05)  # fallback 5%
    try:
        from trading.risk.manager import _get_atr_stop_pct
        atr_stop = _get_atr_stop_pct(signal.symbol, leverage)
        if atr_stop and atr_stop > 0:
            stop_distance_pct = atr_stop
    except Exception:
        pass

    # Core formula: size so that stop_loss hit = risk_amount loss
    # With leverage, our margin = notional / leverage
    # Loss at stop = margin × stop_distance_pct × leverage = notional × stop_distance_pct
    # So: notional = risk_amount / stop_distance_pct
    if stop_distance_pct > 0:
        risk_based_size = risk_amount / stop_distance_pct
    else:
        risk_based_size = per_signal  # fallback to old method

    # Use the LARGER of risk-based sizing and the old budget method
    # (risk-based is usually larger since it properly accounts for leverage)
    order_value = max(risk_based_size * signal.strength, per_signal * signal.strength)

    # --- Factor 2: Confluence boost ---
    confluence_mult = _confluence_multiplier(signal)
    order_value *= confluence_mult

    # --- Factor 3: Regime alignment ---
    regime_mult = _regime_alignment_multiplier(signal)
    order_value *= regime_mult

    # --- Factor 4: Performance tilt ---
    perf_mult = _performance_multiplier(base_strategy)
    order_value *= perf_mult

    # --- Factor 7: Volume-based sizing ---
    if signal.action == "buy":
        try:
            from trading.risk.volume_gate import compute_volume_sizing_multiplier
            from trading.data.aster import alpaca_to_aster
            aster_sym = alpaca_to_aster(signal.symbol)
            volume_mult = compute_volume_sizing_multiplier(aster_sym) if aster_sym else 1.0
        except Exception:
            volume_mult = 1.0
        order_value *= volume_mult
    else:
        volume_mult = 1.0

    # --- Correlation group penalty ---
    corr_mult = _correlation_group_multiplier(signal, positions, portfolio_value)
    dd_mult = _drawdown_multiplier(portfolio_value)
    order_value *= corr_mult * dd_mult

    # --- Liquidation safety check ---
    if leverage > 1:
        liq_distance = 1.0 / leverage
        # Ensure stop is within 80% of liquidation distance
        if stop_distance_pct >= liq_distance * 0.8:
            # Stop is too close to liquidation — reduce size
            order_value *= 0.7

    # Hard cap
    order_value = min(order_value, max_per_position)

    # Also cap at free capital
    order_value = min(order_value, free_capital * 0.95)

    # Minimum order size
    if order_value < 5.0:
        return 0.0

    log.info(
        "Risk-based sizing %s %s: risk=$%.0f stop=%.2f%% → risk_size=$%.0f "
        "× confluence=%.1f × regime=%.2f × perf=%.2f × volume=%.2f "
        "× corr=%.2f × dd=%.2f × strength=%.2f → $%.2f (lev=%dx)",
        signal.action, signal.symbol, risk_amount, stop_distance_pct * 100,
        risk_based_size, confluence_mult, regime_mult, perf_mult, volume_mult,
        corr_mult, dd_mult, signal.strength, order_value, leverage,
    )

    return round(order_value, 2)


def get_rebalance_targets(signals: list[Signal], portfolio_value: float) -> list[dict]:
    """Given signals, compute target orders for rebalancing.

    Returns list of {symbol, action, value, reason}.
    """
    buy_signals = [s for s in signals if s.action == "buy"]
    sell_signals = [s for s in signals if s.action == "sell"]
    orders = []

    # Process sells first to free up capital
    for signal in sell_signals:
        positions = get_positions()
        pos = next((p for p in positions if p["symbol"] == signal.symbol), None)
        if pos and pos["qty"] > 0:
            sell_value = pos["qty"] * (pos["current_price"] or pos["avg_cost"])
            orders.append({
                "symbol": signal.symbol,
                "action": "sell",
                "value": sell_value,
                "qty": pos["qty"],
                "reason": signal.reason,
            })

    # Then process buys
    for signal in buy_signals:
        order_value = calculate_order_size(signal, portfolio_value, len(buy_signals))
        if order_value > 0:
            orders.append({
                "symbol": signal.symbol,
                "action": "buy",
                "value": order_value,
                "qty": None,
                "reason": signal.reason,
            })

    return orders
