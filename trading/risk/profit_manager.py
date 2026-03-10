"""Profit management — take-profit targets and trailing stop logic.

Complements the stop-loss system in scheduler.py by handling the upside:
closing positions that hit a take-profit target or that retrace too far
from their high watermark after a significant gain.
"""

from __future__ import annotations

from rich.console import Console

from trading.db.store import log_action

# ---------------------------------------------------------------------------
# Profit management parameters (self-contained, not in config.py)
# ---------------------------------------------------------------------------

TAKE_PROFIT_PCT = 0.15          # Take full profit at 15% gain
TRAILING_STOP_ACTIVATE = 0.08   # Activate trailing stop after 8% gain
TRAILING_STOP_PCT = 0.04        # Trail 4% below the high watermark

console = Console()


# ---------------------------------------------------------------------------
# ProfitTracker — maintains per-symbol high watermarks between calls
# ---------------------------------------------------------------------------

class ProfitTracker:
    """Tracks the highest observed price for each held position.

    A single instance is created once (typically in the scheduler) and
    passed to ``check_profit_targets`` on every check cycle.  The tracker
    handles first-time symbols by initialising from ``avg_cost`` when the
    caller supplies it.
    """

    def __init__(self) -> None:
        self._high_watermarks: dict[str, float] = {}

    # -- public API --------------------------------------------------------

    def update(self, symbol: str, current_price: float) -> None:
        """Record *current_price* if it exceeds the stored high watermark."""
        prev = self._high_watermarks.get(symbol)
        if prev is None or current_price > prev:
            self._high_watermarks[symbol] = current_price

    def get_high(self, symbol: str) -> float | None:
        """Return the high watermark for *symbol*, or ``None`` if unseen."""
        return self._high_watermarks.get(symbol)

    def remove(self, symbol: str) -> None:
        """Drop tracking data when a position is fully closed."""
        self._high_watermarks.pop(symbol, None)

    # -- helpers -----------------------------------------------------------

    def initialise_from_cost(self, symbol: str, avg_cost: float) -> None:
        """Seed the watermark for a symbol we have never seen before.

        Uses the average cost basis so the tracker starts from a known
        floor rather than ``None``.
        """
        if symbol not in self._high_watermarks:
            self._high_watermarks[symbol] = avg_cost

    @property
    def symbols(self) -> list[str]:
        """Return all tracked symbols."""
        return list(self._high_watermarks)

    def __repr__(self) -> str:
        entries = ", ".join(
            f"{s}: ${p:.2f}" for s, p in self._high_watermarks.items()
        )
        return f"ProfitTracker({{{entries}}})"


# ---------------------------------------------------------------------------
# Core check — returns sell actions for positions hitting profit targets
# ---------------------------------------------------------------------------

def check_profit_targets(
    positions: list[dict],
    tracker: ProfitTracker,
) -> list[dict]:
    """Evaluate every open position against take-profit and trailing-stop rules.

    Parameters
    ----------
    positions:
        Each dict must contain at minimum:
        ``symbol``, ``qty``, ``avg_cost``, ``current_price``,
        ``unrealized_pnl_pct``.
    tracker:
        A long-lived ``ProfitTracker`` instance that persists between
        successive calls.

    Returns
    -------
    list[dict]
        One entry per position that should be closed, each with keys:
        ``symbol``, ``qty``, ``action`` (``"take_profit"`` or
        ``"trailing_stop"``), ``reason``, ``pnl_pct``.
    """
    actions: list[dict] = []

    for pos in positions:
        symbol: str = pos["symbol"]
        qty: float = pos["qty"]
        avg_cost: float = pos["avg_cost"]
        current_price: float = pos["current_price"]

        # Ensure the tracker has a baseline for this symbol
        tracker.initialise_from_cost(symbol, avg_cost)

        # Update watermark with the latest price
        tracker.update(symbol, current_price)

        # Percentage gain from cost basis
        gain_pct = (current_price - avg_cost) / avg_cost if avg_cost > 0 else 0.0

        # -- 1. Take-profit check ------------------------------------------
        if gain_pct >= TAKE_PROFIT_PCT:
            reason = (
                f"Take-profit target hit: {gain_pct * 100:.1f}% gain "
                f"(threshold {TAKE_PROFIT_PCT * 100:.0f}%)"
            )
            actions.append({
                "symbol": symbol,
                "qty": qty,
                "action": "take_profit",
                "reason": reason,
                "pnl_pct": gain_pct,
            })
            log_action(
                "profit_mgmt", "take_profit_triggered",
                symbol=symbol,
                details=reason,
                data={"gain_pct": gain_pct, "avg_cost": avg_cost, "price": current_price},
            )
            continue

        # -- 2. Trailing stop check ----------------------------------------
        if gain_pct >= TRAILING_STOP_ACTIVATE:
            high = tracker.get_high(symbol)
            # high is guaranteed non-None after update() above
            if high is not None and high > 0:
                drawdown_from_high = (high - current_price) / high

                if drawdown_from_high >= TRAILING_STOP_PCT:
                    reason = (
                        f"Trailing stop triggered: price ${current_price:.2f} is "
                        f"{drawdown_from_high * 100:.1f}% below high ${high:.2f} "
                        f"(trail {TRAILING_STOP_PCT * 100:.0f}%, gain {gain_pct * 100:.1f}%)"
                    )
                    actions.append({
                        "symbol": symbol,
                        "qty": qty,
                        "action": "trailing_stop",
                        "reason": reason,
                        "pnl_pct": gain_pct,
                    })
                    log_action(
                        "profit_mgmt", "trailing_stop_triggered",
                        symbol=symbol,
                        details=reason,
                        data={
                            "gain_pct": gain_pct,
                            "high_watermark": high,
                            "drawdown_from_high": drawdown_from_high,
                            "price": current_price,
                        },
                    )
                    continue

        # -- 3. No action needed -------------------------------------------
        # Position is either below trailing-stop activation or still
        # within the trail cushion.  Nothing to do.

    return actions


# ---------------------------------------------------------------------------
# Status formatter — human-readable profit dashboard
# ---------------------------------------------------------------------------

def format_profit_status(
    positions: list[dict],
    tracker: ProfitTracker,
) -> str:
    """Build a rich-formatted string summarising profit status for each position.

    Intended for console output or dashboard display.
    """
    if not positions:
        return "[dim]No open positions.[/dim]"

    lines: list[str] = []
    lines.append("[bold cyan]Profit Management Status[/bold cyan]")
    lines.append(f"[dim]{'=' * 72}[/dim]")
    lines.append(
        f"  {'Symbol':<8} {'Gain':>8} {'TP Dist':>8} "
        f"{'Trail':>10} {'High':>10} {'Price':>10}"
    )
    lines.append(f"[dim]  {'-' * 66}[/dim]")

    for pos in positions:
        symbol = pos["symbol"]
        avg_cost = pos["avg_cost"]
        current_price = pos["current_price"]

        gain_pct = (current_price - avg_cost) / avg_cost if avg_cost > 0 else 0.0
        distance_to_tp = TAKE_PROFIT_PCT - gain_pct

        high = tracker.get_high(symbol)
        high_str = f"${high:.2f}" if high is not None else "n/a"

        # Trailing stop status
        if gain_pct >= TRAILING_STOP_ACTIVATE:
            trail_status = "[yellow]ACTIVE[/yellow]"
            if high is not None and high > 0:
                dd = (high - current_price) / high
                cushion = TRAILING_STOP_PCT - dd
                if cushion > 0:
                    trail_status = f"[yellow]ACTIVE ({cushion * 100:.1f}% cushion)[/yellow]"
                else:
                    trail_status = "[red]TRIGGER[/red]"
        else:
            remaining = TRAILING_STOP_ACTIVATE - gain_pct
            trail_status = f"[dim]{remaining * 100:.1f}% to activate[/dim]"

        # Colour the gain
        if gain_pct >= TAKE_PROFIT_PCT:
            gain_str = f"[bold green]+{gain_pct * 100:.1f}%[/bold green]"
        elif gain_pct >= TRAILING_STOP_ACTIVATE:
            gain_str = f"[green]+{gain_pct * 100:.1f}%[/green]"
        elif gain_pct > 0:
            gain_str = f"[green]+{gain_pct * 100:.1f}%[/green]"
        else:
            gain_str = f"[red]{gain_pct * 100:.1f}%[/red]"

        tp_dist_str = f"+{distance_to_tp * 100:.1f}%" if distance_to_tp > 0 else "[green]HIT[/green]"

        lines.append(
            f"  {symbol:<8} {gain_str:>18} {tp_dist_str:>8} "
            f"{trail_status:>26} {high_str:>10} ${current_price:>9.2f}"
        )

    lines.append(f"[dim]{'=' * 72}[/dim]")
    lines.append(
        f"[dim]  TP = {TAKE_PROFIT_PCT * 100:.0f}%  |  "
        f"Trail activates at {TRAILING_STOP_ACTIVATE * 100:.0f}%  |  "
        f"Trail width {TRAILING_STOP_PCT * 100:.0f}%[/dim]"
    )

    return "\n".join(lines)
