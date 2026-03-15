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

from collections import defaultdict

from rich.console import Console
from rich.table import Table

from trading.db.store import log_action
from trading.strategy.base import Signal

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

def aggregate_signals(all_signals: list[Signal]) -> list[Signal]:
    """Aggregate raw signals from every strategy into one signal per symbol.

    Steps:
        1. Filter to actionable signals (buy / sell only).
        2. Group by symbol.
        3. For each symbol, determine whether strategies agree or conflict.
        4. Resolve conflicts via strength comparison or merge agreements.
        5. Apply the MAX_SINGLE_ASSET_SIGNALS cap.
        6. Log the aggregation event and print a summary table.

    Args:
        all_signals: Flat list of Signal objects from all strategies.

    Returns:
        List of deduplicated, consolidated Signal objects — at most one per
        symbol, ready for downstream position-sizing and risk checks.
    """
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
