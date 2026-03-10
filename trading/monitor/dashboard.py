"""Terminal dashboard — positions, P&L, signals, portfolio summary."""

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text

from trading.db.store import get_positions, get_trades, get_daily_pnl, get_signals


console = Console()


def show_portfolio(account: dict):
    """Display portfolio summary."""
    mode = "[bold yellow]PAPER[/]" if account.get("paper") else "[bold green]LIVE[/]"
    table = Table(title=f"Portfolio Summary {mode}", show_header=False, border_style="blue")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Portfolio Value", f"${account['portfolio_value']:.2f}")
    table.add_row("Cash", f"${account['cash']:.2f}")
    table.add_row("Buying Power", f"${account['buying_power']:.2f}")
    table.add_row("Equity", f"${account['equity']:.2f}")
    table.add_row("Status", account.get("status", "N/A"))

    console.print(table)


def show_positions(positions: list[dict]):
    """Display open positions."""
    if not positions:
        console.print("[dim]No open positions.[/dim]")
        return

    table = Table(title="Open Positions", border_style="green")
    table.add_column("Symbol", style="cyan")
    table.add_column("Qty", justify="right")
    table.add_column("Avg Cost", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("P&L %", justify="right")

    for pos in positions:
        pnl = pos.get("unrealized_pnl", 0)
        pnl_pct = pos.get("unrealized_pnl_pct", 0)
        pnl_style = "green" if pnl >= 0 else "red"

        table.add_row(
            pos["symbol"],
            f"{pos['qty']:.6f}",
            f"${pos['avg_cost']:.2f}",
            f"${pos['current_price']:.2f}",
            f"${pos.get('market_value', pos['qty'] * pos['current_price']):.2f}",
            f"[{pnl_style}]${pnl:+.2f}[/]",
            f"[{pnl_style}]{pnl_pct:+.1f}%[/]",
        )

    console.print(table)


def show_signals(signals: list[dict]):
    """Display trading signals."""
    if not signals:
        console.print("[dim]No signals generated.[/dim]")
        return

    table = Table(title="Trading Signals", border_style="yellow")
    table.add_column("Strategy", style="cyan")
    table.add_column("Symbol", style="white")
    table.add_column("Signal", style="bold")
    table.add_column("Strength", justify="right")
    table.add_column("Reason")

    for sig in signals:
        action = sig.get("action", sig.get("signal", ""))
        if action == "buy":
            style = "green"
        elif action == "sell":
            style = "red"
        else:
            style = "dim"

        table.add_row(
            sig.get("strategy", ""),
            sig.get("symbol", ""),
            f"[{style}]{action.upper()}[/]",
            f"{sig.get('strength', 0):.2f}",
            sig.get("reason", ""),
        )

    console.print(table)


def show_trades(limit: int = 20):
    """Display recent trade history."""
    trades = get_trades(limit=limit)
    if not trades:
        console.print("[dim]No trades yet.[/dim]")
        return

    table = Table(title=f"Recent Trades (last {limit})", border_style="magenta")
    table.add_column("Time", style="dim")
    table.add_column("Symbol", style="cyan")
    table.add_column("Side")
    table.add_column("Qty", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Strategy")
    table.add_column("Status")
    table.add_column("P&L", justify="right")

    for trade in trades:
        side_style = "green" if trade["side"] == "buy" else "red"
        pnl = trade.get("pnl")
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "-"
        pnl_style = "green" if pnl and pnl > 0 else "red" if pnl and pnl < 0 else "dim"

        table.add_row(
            trade["timestamp"][:19],
            trade["symbol"],
            f"[{side_style}]{trade['side'].upper()}[/]",
            f"{trade['qty']:.6f}",
            f"${trade['price']:.2f}" if trade.get("price") else "-",
            f"${trade['total']:.2f}" if trade.get("total") else "-",
            trade.get("strategy", ""),
            trade["status"],
            f"[{pnl_style}]{pnl_str}[/]",
        )

    console.print(table)


def show_daily_pnl(limit: int = 14):
    """Display daily P&L history."""
    records = get_daily_pnl(limit=limit)
    if not records:
        console.print("[dim]No P&L history yet.[/dim]")
        return

    table = Table(title=f"Daily P&L (last {limit} days)", border_style="blue")
    table.add_column("Date", style="cyan")
    table.add_column("Portfolio", justify="right")
    table.add_column("Cash", justify="right")
    table.add_column("Positions", justify="right")
    table.add_column("Daily Return", justify="right")
    table.add_column("Cumulative", justify="right")

    for record in records:
        daily = record.get("daily_return", 0) or 0
        cumul = record.get("cumulative_return", 0) or 0
        daily_style = "green" if daily >= 0 else "red"
        cumul_style = "green" if cumul >= 0 else "red"

        table.add_row(
            record["date"],
            f"${record['portfolio_value']:.2f}",
            f"${record['cash']:.2f}",
            f"${record['positions_value']:.2f}",
            f"[{daily_style}]{daily*100:+.2f}%[/]",
            f"[{cumul_style}]{cumul*100:+.2f}%[/]",
        )

    console.print(table)
