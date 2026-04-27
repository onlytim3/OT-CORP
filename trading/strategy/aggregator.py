"""Signal Aggregator — collects, deduplicates, and consolidates trading signals.

Receives raw signals from all enabled strategies, groups them by symbol,
resolves conflicts when strategies disagree on direction, and returns a
single consolidated signal per symbol. This prevents over-allocation when
many strategies pile into the same asset and ensures conflicting signals
produce a clear net direction (or a hold if the margin is too thin).

Usage:
    from trading.strategy.aggregator import aggregate_signals

    raw_signals = []
    for strategy in get_enabled_strategies():
        raw_signals.extend(strategy.generate_signals())
    consolidated = aggregate_signals(raw_signals)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from trading.db.store import log_action
from trading.strategy.base import Signal

_log = logging.getLogger(__name__)


def _log_counterfactual(sig: "Signal", block_reason: str) -> None:
    """Persist a blocked signal to counterfactual_signals for later PnL analysis."""
    try:
        from trading.db.store import insert_counterfactual
        insert_counterfactual(
            symbol=sig.symbol,
            action=sig.action,
            strength=sig.strength,
            strategy=sig.strategy,
            block_reason=block_reason,
            entry_price=None,  # filled by fill_counterfactual_exits() via price feed
            data=sig.data,
        )
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Exposure / piling-in guardrails
# ---------------------------------------------------------------------------

MAX_CRYPTO_EXPOSURE_PCT: float = 0.70
"""Maximum fraction of the portfolio that should be in crypto. Downstream
risk checks should enforce this — the aggregator exposes the constant so
position-sizers can import it."""

MAX_SINGLE_ASSET_SIGNALS: int = 4
"""If more than this many strategies agree on a single symbol, cap the count
used for strength averaging. This prevents diminishing-returns scenarios
where 8 strategies all say 'buy BTC' and the system over-concentrates."""

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_CONFLICT_MARGIN_THRESHOLD: float = 0.15
"""Minimum difference between total buy strength and total sell strength
required to declare a winner when signals conflict. Below this the
aggregator emits a 'hold' signal for the symbol."""

MIN_CONFLUENCE_STRENGTH: float = 0.40
"""Minimum strength required when only ONE strategy votes for a trade.

This is the PRIMARY gate that prevents the March 25 scenario:
  - multi_factor_rank alone voted 'sell' on 6 altcoins at strength 0.34-0.64
  - All 6 short positions opened simultaneously
  - Violent altcoin rally wiped -30.45% in one cycle

With this gate:
  - 1 strategy + strength < 0.40 → hold (weak signal, insufficient conviction)
  - 1 strategy + strength ≥ 0.40 → proceed (high-conviction single-strategy)
  - 2+ strategies → proceed as normal
  - Recovery mode → requires 2+ strategies regardless of strength
"""

# Strategy correlation groups — strategies measuring the same underlying signal.
# Each group counts as at most 1 vote in confluence scoring.
# The strongest signal from the group represents the group.
STRATEGY_CORRELATION_GROUPS = {
    "funding": {"funding_arb", "funding_term_structure"},
    "basis": {"basis_zscore", "cross_basis_rv"},
    "microstructure": {"taker_divergence", "microstructure_composite", "whale_flow"},
}

# Reverse lookup: strategy_name -> group_name
_STRATEGY_TO_GROUP = {}
for _group_name, _members in STRATEGY_CORRELATION_GROUPS.items():
    for _member in _members:
        _STRATEGY_TO_GROUP[_member] = _group_name

# ---------------------------------------------------------------------------
# Regime-conditional routing — strategy type classification
# ---------------------------------------------------------------------------

STRATEGY_TYPES: dict[str, set[str]] = {
    "momentum": {
        "dual_momentum_antonacci", "cross_asset_momentum", "meme_momentum",
        "kalman_trend", "breakout_detection",
    },
    "mean_reversion": {
        "basis_zscore", "funding_arb", "regime_mean_reversion", "rsi_divergence",
        "pairs_trading", "cross_basis_rv", "funding_forecast",
    },
    "trend": {"garch_volatility", "volatility_regime", "hmm_regime"},
    "microstructure": {
        "microstructure_composite", "taker_divergence", "whale_flow", "oi_price_divergence",
    },
    "fundamental": {
        "factor_crypto", "multi_factor_rank", "equity_crypto_correlation",
        "gold_crypto_hedge", "dxy_dollar", "onchain_flow", "news_sentiment",
    },
    "funding": {"funding_term_structure"},
}
_STRATEGY_TO_TYPE: dict[str, str] = {s: t for t, ss in STRATEGY_TYPES.items() for s in ss}

# Regime → per-type strength multiplier + directional guards.
# Regimes match engine.py _score_label output: strongly bullish / bullish / neutral / bearish / strongly bearish
_REGIME_ROUTING: dict[str, dict] = {
    "strongly bullish": {
        "type_mult": {
            "momentum": 1.30, "trend": 1.20, "mean_reversion": 0.65,
            "microstructure": 1.10, "fundamental": 1.00, "funding": 0.85,
        },
        "buy_mult": 1.10, "sell_mult": 0.40, "block_sells_above": 0.80,
    },
    "bullish": {
        "type_mult": {
            "momentum": 1.15, "trend": 1.10, "mean_reversion": 0.80,
            "microstructure": 1.05, "fundamental": 1.00, "funding": 0.90,
        },
        "buy_mult": 1.05, "sell_mult": 0.60, "block_sells_above": None,
    },
    "neutral": {
        "type_mult": {
            "momentum": 1.00, "trend": 1.00, "mean_reversion": 1.00,
            "microstructure": 1.00, "fundamental": 1.00, "funding": 1.00,
        },
        "buy_mult": 1.00, "sell_mult": 1.00, "block_sells_above": None,
    },
    "bearish": {
        "type_mult": {
            "momentum": 0.70, "trend": 0.85, "mean_reversion": 1.25,
            "microstructure": 0.90, "fundamental": 0.90, "funding": 1.15,
        },
        "buy_mult": 0.65, "sell_mult": 1.10, "block_sells_above": None,
    },
    "strongly bearish": {
        "type_mult": {
            "momentum": 0.40, "trend": 0.60, "mean_reversion": 1.30,
            "microstructure": 0.80, "fundamental": 0.75, "funding": 1.20,
        },
        "buy_mult": 0.30, "sell_mult": 1.20, "block_sells_above": None,
    },
}

# ---------------------------------------------------------------------------
# Asset correlation groups for portfolio deconcentration
# ---------------------------------------------------------------------------

_ASSET_CORR_GROUPS: dict[str, list[str]] = {
    "btc": ["BTC/USDT", "BTCUSDT", "BTC/USD", "BCH/USD", "LTC/USD"],
    "eth": ["ETH/USDT", "ETHUSDT", "ETH/USD", "LINK/USD", "UNI/USD", "AAVE/USD"],
    "alt_l1": [
        "SOL/USDT", "SOLUSDT", "SOL/USD", "AVAX/USDT", "AVAXUSDT",
        "AVAX/USD", "DOT/USDT", "DOTUSDT",
    ],
    "meme": [
        "DOGE/USDT", "DOGEUSDT", "SHIB/USDT", "SHIBUSDT", "PEPE/USDT", "PEPEUSDT",
        "WIF/USDT", "WIFUSDT", "BONK/USDT", "BONKUSDT",
    ],
}
_SYMBOL_TO_CORR_GROUP: dict[str, str] = {
    sym: grp for grp, syms in _ASSET_CORR_GROUPS.items() for sym in syms
}

_console = Console()


# ---------------------------------------------------------------------------
# Correlation deduplication
# ---------------------------------------------------------------------------

def _deduplicate_correlated(signals: list) -> list:
    """Deduplicate signals from correlated strategy groups.

    For each symbol, if multiple strategies from the same correlation group
    emit signals, keep only the strongest one. This prevents correlated
    strategies from inflating confluence scores.
    """
    # Group signals by (symbol, correlation_group)
    group_signals = defaultdict(list)  # (symbol, group) -> [signals]
    ungrouped = []

    for sig in signals:
        group = _STRATEGY_TO_GROUP.get(sig.strategy)
        if group:
            group_signals[(sig.symbol, group)].append(sig)
        else:
            ungrouped.append(sig)

    # From each group, keep only the strongest signal
    deduplicated = list(ungrouped)
    for (symbol, group), group_sigs in group_signals.items():
        # Sort by strength descending, take the strongest
        group_sigs.sort(key=lambda s: s.strength, reverse=True)
        best = group_sigs[0]
        if len(group_sigs) > 1:
            # Annotate that this signal represents a group
            original_reason = best.reason
            best = Signal(
                strategy=best.strategy,
                symbol=best.symbol,
                action=best.action,
                strength=best.strength,
                reason=f"{original_reason} [representing {group} group: {len(group_sigs)} strategies]",
                data={**(best.data or {}), "correlation_group": group, "group_count": len(group_sigs)},
            )
        deduplicated.append(best)

    return deduplicated


# ---------------------------------------------------------------------------
# Strategy weighting
# ---------------------------------------------------------------------------

def _get_strategy_weight(strategy_name: str) -> float:
    """Get performance-based weight for a strategy.

    Returns a multiplier (0.6 - 1.3) based on historical win rate.
    """
    try:
        from trading.db.store import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins "
                "FROM trades WHERE strategy = ? AND status = 'closed' "
                "ORDER BY timestamp DESC LIMIT 30",
                (strategy_name,),
            ).fetchone()
            if rows and rows["total"] >= 5:
                win_rate = rows["wins"] / rows["total"]
                if win_rate > 0.6:
                    return 1.3
                elif win_rate < 0.3:
                    return 0.6
                else:
                    # Linear interpolation between 0.6x and 1.3x
                    return 0.6 + (win_rate - 0.3) / 0.3 * 0.7
    except Exception:
        pass
    return 1.0  # Default: no adjustment


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _apply_signal_decay(signals: list[Signal], cycle_interval_hours: float = 4.0) -> list[Signal]:
    """Apply time-based decay to signals. Discard signals older than 3 cycle intervals."""
    now = datetime.now(timezone.utc)
    max_age_hours = cycle_interval_hours * 3
    result = []
    for sig in signals:
        age_hours = 0.0
        if hasattr(sig, 'data') and isinstance(sig.data, dict) and sig.data.get('timestamp'):
            try:
                sig_time = datetime.fromisoformat(str(sig.data['timestamp']))
                age_hours = (now - sig_time).total_seconds() / 3600
            except Exception:
                pass
        if age_hours > max_age_hours:
            continue  # Discard stale signals
        if age_hours > 0:
            decay = max(0.0, 1.0 - age_hours / max_age_hours)
            sig = Signal(
                strategy=sig.strategy, symbol=sig.symbol, action=sig.action,
                strength=round(sig.strength * decay, 4),
                reason=sig.reason, data=sig.data,
            )
        result.append(sig)
    return result


def _compute_diversity_bonus(signals: list[Signal]) -> float:
    """Compute diversity bonus based on distinct correlation groups.
    3+ groups -> 1.15x, 2 groups -> 1.05x, 1 group -> 1.0x"""
    groups = set()
    for sig in signals:
        group = _STRATEGY_TO_GROUP.get(sig.strategy, sig.strategy)
        groups.add(group)
    n = len(groups)
    if n >= 3:
        return 1.15
    elif n >= 2:
        return 1.05
    return 1.0


def _apply_multi_timeframe_confirmation(signal: Signal) -> Signal:
    """Adjust signal strength based on daily 20-SMA trend confirmation."""
    try:
        from trading.strategy.indicators import sma
        from trading.execution.bybit_client import get_bybit_klines
        klines = get_bybit_klines(signal.symbol.replace("/", ""), interval="1d", limit=25)
        if klines and len(klines) >= 20:
            closes = [float(k[4]) for k in klines]
            sma_20 = sum(closes[-20:]) / 20
            current = closes[-1]
            if signal.action == "buy":
                mult = 1.10 if current > sma_20 else 0.80
            elif signal.action == "sell":
                mult = 1.10 if current < sma_20 else 0.80
            else:
                return signal
            return Signal(
                strategy=signal.strategy, symbol=signal.symbol, action=signal.action,
                strength=round(min(signal.strength * mult, 1.0), 4),
                reason=signal.reason + f" [MTF: {'aligned' if mult > 1 else 'misaligned'}]",
                data=signal.data,
            )
    except Exception:
        pass
    return signal


def _apply_aggression_scaling(consolidated: list[Signal]) -> list[Signal]:
    """If the market is in extreme panic (Fear & Greed < 25), heavily boost buy signals.
    
    This acts as a contrarian multiplier, and overrides the MIN_CONFLUENCE_STRENGTH
    so we can catch market bottoms aggressively.
    """
    try:
        import requests
        resp = requests.get("https://api.alternative.me/fng/", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            fng = int(data["data"][0]["value"])
            if fng <= 25:
                # Extreme fear -> apply aggression scaling
                for i, sig in enumerate(consolidated):
                    if sig.action == "buy":
                        mult = 1.35 if fng <= 15 else 1.20
                        # Auto-pass the confluence gate by forcing strength to >= 0.60
                        new_strength = max(round(min(sig.strength * mult, 1.0), 4), 0.60)
                        consolidated[i] = Signal(
                            strategy=sig.strategy, symbol=sig.symbol, action=sig.action,
                            strength=new_strength,
                            reason=sig.reason + f" [AGGR: F&G {fng}]",
                            data=sig.data,
                        )
    except Exception as e:
        _log.warning("Failed to fetch Fear & Greed for aggression scaling: %s", e)
    return consolidated


def _apply_regime_routing(signals: list, raw_all: list) -> list:
    """Full regime-conditional routing: boost/dampen strategies by type and market regime.

    Combines HMM regime detection (from raw signals) with the market intelligence
    briefing (engine.py) to determine a composite regime, then applies per-strategy-type
    strength multipliers and directional guards. Supersedes the old _apply_regime_guard
    which only handled the HMM bull → dampen shorts case.
    """
    # --- HMM regime from raw signals ---
    hmm_sigs = [
        s for s in raw_all
        if s.strategy == "hmm_regime" and isinstance(getattr(s, "data", None), dict)
    ]
    hmm_regime = "neutral"
    hmm_confidence = 0.0
    if hmm_sigs:
        best = max(hmm_sigs, key=lambda s: s.strength)
        hmm_regime = (best.data or {}).get("regime", "neutral").lower()
        hmm_confidence = best.strength

    # --- Market intelligence briefing regime ---
    briefing_regime = "neutral"
    briefing_score = 0.0
    try:
        from trading.intelligence.engine import generate_briefing
        _b = generate_briefing()
        briefing_regime = getattr(_b, "overall_regime", "neutral").lower()
        briefing_score = getattr(_b, "overall_score", 0.0)
    except Exception:
        pass

    def _norm(label: str, score: float = 0.0) -> str:
        if "strongly bullish" in label:
            return "strongly bullish"
        if label in ("bullish", "bull", "trending_up", "uptrend"):
            return "bullish"
        if label in ("bearish", "bear", "trending_down", "downtrend"):
            return "bearish"
        if "strongly bearish" in label or label in ("crash", "extreme_bear"):
            return "strongly bearish"
        # Fallback: numeric score
        if score >= 0.5:
            return "strongly bullish"
        if score >= 0.2:
            return "bullish"
        if score <= -0.5:
            return "strongly bearish"
        if score <= -0.2:
            return "bearish"
        return "neutral"

    _score_map = {
        "strongly bearish": -2, "bearish": -1, "neutral": 0,
        "bullish": 1, "strongly bullish": 2,
    }
    norm_hmm = _norm(hmm_regime)
    norm_briefing = _norm(briefing_regime, briefing_score)
    # The more extreme signal drives the composite (higher absolute score wins; tie → briefing)
    composite = (
        norm_hmm
        if abs(_score_map[norm_hmm]) > abs(_score_map[norm_briefing])
        else norm_briefing
    )

    routing = _REGIME_ROUTING.get(composite, _REGIME_ROUTING["neutral"])
    type_mult = routing["type_mult"]
    buy_mult = routing["buy_mult"]
    sell_mult = routing["sell_mult"]
    block_sells_above = routing.get("block_sells_above")

    if composite == "neutral" and not hmm_sigs:
        return signals  # Nothing to adjust

    result = []
    for sig in signals:
        if sig.action not in ("buy", "sell"):
            result.append(sig)
            continue

        strat_type = _STRATEGY_TO_TYPE.get(sig.strategy, "fundamental")
        t_mult = type_mult.get(strat_type, 1.0)
        d_mult = buy_mult if sig.action == "buy" else sell_mult

        # Block shorts at extreme bull confidence (preserves original regime guard behaviour)
        if sig.action == "sell" and block_sells_above and hmm_confidence >= block_sells_above:
            result.append(Signal(
                strategy=sig.strategy, symbol=sig.symbol, action="hold",
                strength=0.0,
                reason=(
                    f"{sig.reason} [REGIME BLOCK: {composite} @{hmm_confidence:.0%} — "
                    f"short blocked to prevent rally wipeout]"
                ),
                data=sig.data,
            ))
            _log.info(
                f"Regime routing BLOCKED short on {sig.symbol} "
                f"({composite} @{hmm_confidence:.0%})"
            )
            _log_counterfactual(sig, "regime_routing")
            continue

        combined = min(t_mult * d_mult, 2.0)
        if abs(combined - 1.0) < 0.02:
            result.append(sig)
            continue

        new_str = round(min(sig.strength * combined, 1.0), 4)
        tag = "BOOST" if combined > 1.0 else "DAMPEN"
        result.append(Signal(
            strategy=sig.strategy, symbol=sig.symbol, action=sig.action,
            strength=new_str,
            reason=(
                f"{sig.reason} [REGIME {tag}: {composite}, "
                f"{strat_type} {combined:.2f}x → {new_str:.2f}]"
            ),
            data=sig.data,
        ))
        log_action("regime", "routing_decision", symbol=sig.symbol, data={
            "strategy": sig.strategy,
            "action": sig.action,
            "original_strength": round(sig.strength, 4),
            "adjusted_strength": new_str,
            "tag": tag,
            "regime": composite,
            "strat_type": strat_type,
            "multiplier": round(combined, 3),
        })

    if composite != "neutral":
        _log.info(
            f"Regime routing applied: composite={composite} "
            f"(HMM={norm_hmm}@{hmm_confidence:.0%}, briefing={norm_briefing}@{briefing_score:+.2f})"
        )
        try:
            from trading.db.store import set_setting, get_setting
            old_regime = get_setting("current_regime") or "neutral"
            set_setting("current_regime", composite)
            set_setting("current_regime_score", str(round(briefing_score, 4)))
            if old_regime != composite:
                try:
                    from trading.monitor.notifications import notify_regime_shift
                    notify_regime_shift(old_regime, composite, briefing_score)
                except Exception:
                    pass
        except Exception:
            pass
    return result


def _apply_correlation_deconcentration(signals: list) -> list:
    """Penalise same-direction signals from correlated asset groups.

    When BTC, ETH, and SOL all generate buy signals simultaneously, the system
    is effectively taking a single concentrated bet on crypto beta. This function
    ranks signals within each asset correlation group by strength and applies
    descending penalties: rank #1 = 1.0x, #2 = 0.85x, #3+ = 0.70x.
    """
    buys = [s for s in signals if s.action == "buy"]
    sells = [s for s in signals if s.action == "sell"]
    others = [s for s in signals if s.action not in ("buy", "sell")]

    def _deconc(directional: list) -> list:
        if len(directional) <= 1:
            return directional

        groups: dict[str, list] = defaultdict(list)
        ungrouped = []
        for sig in directional:
            grp = _SYMBOL_TO_CORR_GROUP.get(sig.symbol)
            if grp:
                groups[grp].append(sig)
            else:
                ungrouped.append(sig)

        result = list(ungrouped)
        penalties = [1.0, 0.85, 0.70]

        for grp, grp_sigs in groups.items():
            if len(grp_sigs) == 1:
                result.extend(grp_sigs)
                continue

            grp_sigs.sort(key=lambda s: s.strength, reverse=True)
            for i, sig in enumerate(grp_sigs):
                p = penalties[min(i, len(penalties) - 1)]
                if p >= 1.0:
                    result.append(sig)
                    continue
                new_str = round(sig.strength * p, 4)
                result.append(Signal(
                    strategy=sig.strategy, symbol=sig.symbol, action=sig.action,
                    strength=new_str,
                    reason=(
                        f"{sig.reason} [CORR DECONC: {grp} rank#{i + 1} "
                        f"{p:.0%} → {new_str:.2f}]"
                    ),
                    data=sig.data,
                ))
                _log.info(
                    f"Corr deconc: {sig.symbol} ({grp}) rank#{i + 1} "
                    f"penalty {p:.0%}, strength {sig.strength:.2f}→{new_str:.2f}"
                )

        return result

    return _deconc(buys) + _deconc(sells) + others


def _apply_confluence_gate(consolidated: list, by_symbol: dict) -> list:
    """Block low-conviction single-strategy signals from becoming trades.

    If only 1 strategy voted for a symbol (after deduplication) and the
    strength is below MIN_CONFLUENCE_STRENGTH, convert to hold.

    Also enforces recovery mode minimum strategy requirement.
    """
    try:
        from trading.strategy.circuit_breaker import get_recovery_mode, CONSERVATIVE_MIN_STRATEGIES
        recovery = get_recovery_mode()
        recovery_active = recovery.get("active", False)
        min_in_recovery = recovery.get("min_strategies", CONSERVATIVE_MIN_STRATEGIES)
    except Exception:
        recovery_active = False
        min_in_recovery = 2

    result = []
    for sig in consolidated:
        if sig.action not in ("buy", "sell"):
            result.append(sig)
            continue

        contributing = by_symbol.get(sig.symbol, [])
        n_strategies = len(contributing)

        # Recovery mode: require minimum number of confirming strategies
        if recovery_active and n_strategies < min_in_recovery:
            result.append(Signal(
                strategy=sig.strategy, symbol=sig.symbol, action="hold",
                strength=0.0,
                reason=(
                    f"{sig.reason} [RECOVERY GATE: need {min_in_recovery}+ strategies, "
                    f"only {n_strategies} voted]"
                ),
                data=sig.data,
            ))
            _log.info(
                f"Confluence gate (recovery): blocked {sig.action} on {sig.symbol} "
                f"({n_strategies} strategies, need {min_in_recovery})"
            )
            _log_counterfactual(sig, "recovery_gate")
            continue

        # Normal mode: single-strategy trades need high-conviction strength
        if n_strategies == 1 and sig.strength < MIN_CONFLUENCE_STRENGTH:
            result.append(Signal(
                strategy=sig.strategy, symbol=sig.symbol, action="hold",
                strength=0.0,
                reason=(
                    f"{sig.reason} [CONFLUENCE GATE: single-strategy strength "
                    f"{sig.strength:.2f} < {MIN_CONFLUENCE_STRENGTH} minimum. "
                    f"Need 2+ strategies or strength ≥ {MIN_CONFLUENCE_STRENGTH}]"
                ),
                data=sig.data,
            ))
            _log.info(
                f"Confluence gate blocked {sig.action} on {sig.symbol}: "
                f"1 strategy, strength={sig.strength:.2f} < {MIN_CONFLUENCE_STRENGTH}"
            )
            _log_counterfactual(sig, "confluence_gate")
        else:
            result.append(sig)

    return result


def aggregate_signals(all_signals: list[Signal]) -> list[Signal]:
    """Aggregate raw signals from every strategy into one signal per symbol.

    Steps:
        1. Filter to actionable signals (buy / sell only).
        2. Group by symbol.
        3. For each symbol, determine whether strategies agree or conflict.
        4. Resolve conflicts via strength comparison or merge agreements.
        5. Apply the MAX_SINGLE_ASSET_SIGNALS cap.
        6. Apply regime guard (dampen/block shorts in bull markets).
        7. Apply confluence gate (require 2+ strategies OR strength >= 0.55).
        8. Log the aggregation event and print a summary table.

    Args:
        all_signals: Flat list of Signal objects from all strategies.

    Returns:
        List of deduplicated, consolidated Signal objects — at most one per
        symbol, ready for downstream position-sizing and risk checks.
    """
    # Phase 3.3: Apply signal decay before filtering
    all_signals = _apply_signal_decay(all_signals)

    actionable = [s for s in all_signals if s.is_actionable]

    if not actionable:
        log_action(
            category="signal",
            action="aggregate",
            details="No actionable signals to aggregate",
            result="empty",
        )
        return []

    # Deduplicate correlated strategies before grouping
    actionable = _deduplicate_correlated(actionable)

    # Full regime routing BEFORE grouping (boosts/dampens by strategy type × market regime)
    actionable = _apply_regime_routing(actionable, all_signals)

    # Group by symbol
    by_symbol: dict[str, list[Signal]] = defaultdict(list)
    for sig in actionable:
        by_symbol[sig.symbol].append(sig)

    consolidated: list[Signal] = []

    for symbol, signals in sorted(by_symbol.items()):
        buys = [s for s in signals if s.action == "buy"]
        sells = [s for s in signals if s.action == "sell"]

        if buys and sells:
            # Conflict: strategies disagree on direction
            result = _resolve_conflict(buys, sells)
        else:
            # Agreement: all signals point the same way
            result = _merge_agreement(signals)

        consolidated.append(result)

    # Phase 3.4: Apply diversity bonus to each consolidated signal
    for i, sig in enumerate(consolidated):
        contributing = by_symbol.get(sig.symbol, [])
        bonus = _compute_diversity_bonus(contributing)
        if bonus > 1.0 and sig.action in ("buy", "sell"):
            consolidated[i] = Signal(
                strategy=sig.strategy, symbol=sig.symbol, action=sig.action,
                strength=round(min(sig.strength * bonus, 1.0), 4),
                reason=sig.reason + f" [diversity: {bonus:.2f}x]",
                data=sig.data,
            )

    # Phase 4.5: Multi-timeframe confirmation
    for i, sig in enumerate(consolidated):
        if sig.action in ("buy", "sell"):
            consolidated[i] = _apply_multi_timeframe_confirmation(sig)

    # Correlation deconcentration: penalise correlated-asset pile-ins before aggression scaling
    consolidated = _apply_correlation_deconcentration(consolidated)

    # Phase 4.8: Contrarian Aggression Scaling (Fear & Greed)
    consolidated = _apply_aggression_scaling(consolidated)

    # P0: Apply confluence gate (blocks single-strategy low-conviction trades)
    consolidated = _apply_confluence_gate(consolidated, by_symbol)

    # Log aggregation summary
    _log_aggregation(all_signals, actionable, consolidated)
    _print_summary(all_signals, actionable, consolidated)

    return consolidated


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------

def _resolve_conflict(
    buy_signals: list[Signal],
    sell_signals: list[Signal],
) -> Signal:
    """Resolve opposing buy and sell signals for the same symbol.

    Compares total buy strength against total sell strength.  If the margin
    exceeds ``_CONFLICT_MARGIN_THRESHOLD`` the stronger side wins and a
    consolidated signal is returned with adjusted strength.  Otherwise a
    ``hold`` signal is emitted because the edge is too uncertain.

    Args:
        buy_signals:  All buy signals for this symbol.
        sell_signals: All sell signals for this symbol.

    Returns:
        A single consolidated Signal for the symbol.
    """
    symbol = buy_signals[0].symbol

    total_buy_strength = sum(
        s.strength * _get_strategy_weight(s.strategy) for s in buy_signals
    )
    total_sell_strength = sum(
        s.strength * _get_strategy_weight(s.strategy) for s in sell_signals
    )

    margin = abs(total_buy_strength - total_sell_strength)

    if margin < _CONFLICT_MARGIN_THRESHOLD:
        # Too close to call — sit this one out
        buy_names = ", ".join(s.strategy for s in buy_signals)
        sell_names = ", ".join(s.strategy for s in sell_signals)
        return Signal(
            strategy="aggregator",
            symbol=symbol,
            action="hold",
            strength=0.0,
            reason=(
                f"Conflicting signals resolved to hold "
                f"(buy={total_buy_strength:.2f} vs sell={total_sell_strength:.2f}, "
                f"margin={margin:.2f} < {_CONFLICT_MARGIN_THRESHOLD}). "
                f"Buy: [{buy_names}], Sell: [{sell_names}]"
            ),
            data={
                "conflict": True,
                "buy_count": len(buy_signals),
                "sell_count": len(sell_signals),
                "buy_strength": round(total_buy_strength, 4),
                "sell_strength": round(total_sell_strength, 4),
                "margin": round(margin, 4),
            },
        )

    # There is a clear winner
    if total_buy_strength > total_sell_strength:
        winner_action = "buy"
        winner_signals = buy_signals
        winner_strength = total_buy_strength
        loser_strength = total_sell_strength
    else:
        winner_action = "sell"
        winner_signals = sell_signals
        winner_strength = total_sell_strength
        loser_strength = total_buy_strength

    winner_count = min(len(winner_signals), MAX_SINGLE_ASSET_SIGNALS)
    net_strength = (winner_strength - loser_strength) / winner_count
    net_strength = min(max(net_strength, 0.0), 1.0)

    winner_names = [s.strategy for s in winner_signals[:MAX_SINGLE_ASSET_SIGNALS]]

    return Signal(
        strategy="aggregator",
        symbol=symbol,
        action=winner_action,
        strength=round(net_strength, 4),
        reason=(
            f"Conflict resolved to {winner_action} "
            f"({len(winner_signals)} vs {len(buy_signals) + len(sell_signals) - len(winner_signals)} signals, "
            f"net strength={net_strength:.2f}). "
            f"Strategies: {', '.join(winner_names)}"
        ),
        data={
            "conflict": True,
            "resolved_action": winner_action,
            "buy_count": len(buy_signals),
            "sell_count": len(sell_signals),
            "buy_strength": round(total_buy_strength, 4),
            "sell_strength": round(total_sell_strength, 4),
            "net_strength": round(net_strength, 4),
            "contributing_strategies": winner_names,
        },
    )


# ---------------------------------------------------------------------------
# Agreement merging
# ---------------------------------------------------------------------------

def _merge_agreement(signals: list[Signal]) -> Signal:
    """Merge signals that all agree on direction into one consolidated signal.

    Averages strength across contributing strategies (capped by
    ``MAX_SINGLE_ASSET_SIGNALS``) and caps the result at 1.0.

    Args:
        signals: Non-empty list of signals for one symbol, all sharing
                 the same action (all buy or all sell).

    Returns:
        A single consolidated Signal for the symbol.
    """
    symbol = signals[0].symbol
    action = signals[0].action

    # Cap how many strategies can contribute to the average
    capped = signals[:MAX_SINGLE_ASSET_SIGNALS]
    weights = [_get_strategy_weight(s.strategy) for s in capped]
    total_weight = sum(weights)
    avg_strength = sum(
        s.strength * w for s, w in zip(capped, weights)
    ) / total_weight if total_weight else 0.0
    avg_strength = min(avg_strength, 1.0)

    strategy_names = [s.strategy for s in capped]
    total_count = len(signals)
    capped_note = ""
    if total_count > MAX_SINGLE_ASSET_SIGNALS:
        capped_note = (
            f" (capped from {total_count} to {MAX_SINGLE_ASSET_SIGNALS} "
            f"strategies — diminishing returns)"
        )

    return Signal(
        strategy="aggregator",
        symbol=symbol,
        action=action,
        strength=round(avg_strength, 4),
        reason=(
            f"{total_count} strategies agree on {action}: "
            f"{', '.join(strategy_names)}{capped_note}"
        ),
        data={
            "conflict": False,
            "action": action,
            "signal_count": total_count,
            "capped_count": len(capped),
            "avg_strength": round(avg_strength, 4),
            "contributing_strategies": strategy_names,
        },
    )


# ---------------------------------------------------------------------------
# Logging & display helpers
# ---------------------------------------------------------------------------

def _log_aggregation(
    all_signals: list[Signal],
    actionable: list[Signal],
    consolidated: list[Signal],
) -> None:
    """Persist an aggregation summary to the action log."""
    actions = {s.action for s in consolidated}
    symbols = [s.symbol for s in consolidated]

    log_action(
        category="signal",
        action="aggregate",
        details=(
            f"Aggregated {len(all_signals)} raw signals "
            f"({len(actionable)} actionable) into "
            f"{len(consolidated)} consolidated signals"
        ),
        result=", ".join(sorted(actions)) if actions else "none",
        data={
            "total_raw": len(all_signals),
            "total_actionable": len(actionable),
            "total_consolidated": len(consolidated),
            "symbols": symbols,
            "actions": {s.symbol: s.action for s in consolidated},
            "strengths": {s.symbol: s.strength for s in consolidated},
        },
    )


def _print_summary(
    all_signals: list[Signal],
    actionable: list[Signal],
    consolidated: list[Signal],
) -> None:
    """Print a rich table summarising the aggregation result."""
    _console.print()
    _console.rule("[bold cyan]Signal Aggregation Summary[/bold cyan]")
    _console.print(
        f"  Raw signals: {len(all_signals)}  |  "
        f"Actionable: {len(actionable)}  |  "
        f"Consolidated: {len(consolidated)}"
    )

    if not consolidated:
        _console.print("  [dim]No consolidated signals to display.[/dim]")
        return

    table = Table(show_header=True, header_style="bold", pad_edge=False)
    table.add_column("Symbol", style="white", min_width=10)
    table.add_column("Action", min_width=6)
    table.add_column("Strength", justify="right", min_width=8)
    table.add_column("Conflict?", justify="center", min_width=9)
    table.add_column("Reason", max_width=72)

    for sig in consolidated:
        action_style = {
            "buy": "bold green",
            "sell": "bold red",
            "hold": "dim yellow",
        }.get(sig.action, "white")

        is_conflict = "yes" if sig.data and sig.data.get("conflict") else "no"

        table.add_row(
            sig.symbol,
            f"[{action_style}]{sig.action.upper()}[/{action_style}]",
            f"{sig.strength:.2f}",
            is_conflict,
            sig.reason,
        )

    _console.print(table)
    _console.print()
