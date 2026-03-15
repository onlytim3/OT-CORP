"""Batch backtest runner — run all strategies over 2 years and compare results.

Usage:
    python -m trading.backtest.run_all [--days 730] [--capital 100000]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from trading.backtest.engine import (
    Backtester,
    BacktestResult,
    _fetch_historical_data,
    print_backtest_report,
)
from trading.strategy.registry import list_registered

logger = logging.getLogger(__name__)
console = Console()

ALL_STRATEGIES = [
    # Core crypto
    "rsi_divergence",
    "hmm_regime",
    "pairs_trading",
    "kalman_trend",
    "regime_mean_reversion",
    "factor_crypto",
    # Perps-specific (AsterDex)
    "funding_arb",
    "microstructure_composite",
    "basis_zscore",
    "funding_term_structure",
    "taker_divergence",
    "cross_basis_rv",
    "oi_price_divergence",
    "whale_flow",
    # Cross-asset
    "cross_asset_momentum",
    "gold_crypto_hedge",
    "equity_crypto_correlation",
    # Advanced
    "multi_factor_rank",
    "volatility_regime",
    "meme_momentum",
]


def run_all_backtests(
    days: int = 730,
    starting_capital: float = 100_000,
    strategies: list[str] | None = None,
    leverage: int = 1,
) -> list[BacktestResult]:
    """Run backtests for all strategies using shared historical data.

    Fetches data ONCE and reuses across all strategies to avoid redundant API calls.
    """
    strategy_names = strategies or ALL_STRATEGIES

    console.print(Panel(
        f"[bold]2-Year Backtest — All Strategies[/]\n"
        f"Period: {days} days | Capital: ${starting_capital:,.0f} | "
        f"Leverage: {leverage}x | Strategies: {len(strategy_names)}",
        style="bold cyan",
    ))

    # Fetch data once for all strategies
    console.print("\n[bold yellow]Phase 1: Fetching historical data (shared across all strategies)...[/]\n")
    t0 = time.time()
    historical_data = _fetch_historical_data("all", days)
    fetch_time = time.time() - t0
    console.print(f"\n[green]Data fetched in {fetch_time:.1f}s[/]\n")

    # Show data summary
    _print_data_summary(historical_data)

    # Run each strategy
    end_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    results: list[BacktestResult] = []

    console.print(f"\n[bold yellow]Phase 2: Running {len(strategy_names)} backtests ({start_date} → {end_date})...[/]\n")

    for i, name in enumerate(strategy_names, 1):
        console.print(f"[bold][{i}/{len(strategy_names)}] Running: {name}...[/]", end=" ")
        t0 = time.time()

        try:
            backtester = Backtester(
                starting_capital=starting_capital,
                commission_pct=0.001,
                leverage=leverage,
            )
            result = backtester.run(
                strategy_name=name,
                historical_data=historical_data,
                start_date=str(start_date),
                end_date=str(end_date),
            )
            elapsed = time.time() - t0
            m = result.metrics
            total_return = 0
            if result.portfolio_values:
                end_val = result.portfolio_values[-1]["value"]
                total_return = (end_val - starting_capital) / starting_capital * 100

            color = "green" if total_return >= 0 else "red"
            console.print(
                f"[{color}]{total_return:+.2f}%[/{color}] "
                f"({m['total_trades']} trades, {m['win_rate']*100:.0f}% WR, "
                f"Sharpe {m['sharpe_ratio']:.2f}) [{elapsed:.1f}s]"
            )
            results.append(result)

        except Exception as exc:
            console.print(f"[red]FAILED: {exc}[/red]")
            logger.exception("Backtest failed for %s", name)
            # Add empty result so we still show it in comparison
            results.append(BacktestResult(
                strategy_name=name,
                metrics={"total_trades": 0, "closed_trades": 0, "win_rate": 0,
                         "total_pnl": 0, "avg_trade_pnl": 0, "sharpe_ratio": 0,
                         "max_drawdown": 0, "best_trade": 0, "worst_trade": 0},
                start_date=str(start_date),
                end_date=str(end_date),
                starting_capital=starting_capital,
            ))

    # Print comparison
    console.print(f"\n[bold yellow]Phase 3: Comparative Analysis[/]\n")
    _print_comparison_table(results, starting_capital)
    _print_ranking(results, starting_capital)
    _print_combined_portfolio(results, starting_capital)

    # Save results to JSON
    _save_results(results, days, starting_capital)

    return results


def _print_data_summary(data: dict) -> None:
    """Print a summary of fetched historical data."""
    table = Table(title="Historical Data Summary", show_lines=True)
    table.add_column("Data Source", style="bold")
    table.add_column("Items", justify="right")
    table.add_column("Date Range")

    ohlc = data.get("ohlc", {})
    if ohlc:
        for coin, df in ohlc.items():
            if not df.empty:
                table.add_row(
                    f"OHLC: {coin}",
                    str(len(df)),
                    f"{df.index[0].date()} → {df.index[-1].date()}" if hasattr(df.index[0], 'date') else "N/A",
                )

    etfs = data.get("etf_history", {})
    if etfs:
        for sym, df in etfs.items():
            if not df.empty:
                table.add_row(
                    f"ETF: {sym}",
                    str(len(df)),
                    f"{df.index[0].date()} → {df.index[-1].date()}" if hasattr(df.index[0], 'date') else "N/A",
                )

    fg = data.get("fear_greed", [])
    if fg:
        table.add_row("Fear & Greed", str(len(fg)), "daily index values")

    fred = data.get("fred_series", {})
    if fred:
        for sid, df in fred.items():
            if not df.empty:
                table.add_row(
                    f"FRED: {sid}",
                    str(len(df)),
                    f"{df.index[0].date()} → {df.index[-1].date()}" if hasattr(df.index[0], 'date') else "N/A",
                )

    console.print(table)


def _print_comparison_table(results: list[BacktestResult], starting_capital: float) -> None:
    """Print a side-by-side comparison of all strategy results."""
    table = Table(title="Strategy Comparison — 2-Year Backtest", show_lines=True)
    table.add_column("Strategy", style="bold", min_width=20)
    table.add_column("Total Return", justify="right", min_width=14)
    table.add_column("Total P&L", justify="right", min_width=12)
    table.add_column("Trades", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("Avg P&L", justify="right")
    table.add_column("Best Trade", justify="right")
    table.add_column("Worst Trade", justify="right")
    table.add_column("Sharpe", justify="right")
    table.add_column("Max DD", justify="right")

    for r in sorted(results, key=lambda x: _total_return(x, starting_capital), reverse=True):
        m = r.metrics
        ret = _total_return(r, starting_capital)
        color = "green" if ret >= 0 else "red"

        table.add_row(
            r.strategy_name,
            f"[{color}]{ret:+.2f}%[/{color}]",
            f"[{color}]${m.get('total_pnl', 0):+,.2f}[/{color}]",
            str(m.get("total_trades", 0)),
            f"{m.get('win_rate', 0)*100:.1f}%",
            f"${m.get('avg_trade_pnl', 0):+,.2f}",
            f"[green]${m.get('best_trade', 0):+,.2f}[/green]",
            f"[red]${m.get('worst_trade', 0):+,.2f}[/red]",
            f"{m.get('sharpe_ratio', 0):.2f}",
            f"[red]{m.get('max_drawdown', 0)*100:.2f}%[/red]",
        )

    console.print(table)


def _print_ranking(results: list[BacktestResult], starting_capital: float) -> None:
    """Print strategies ranked by different metrics."""
    console.print()

    # Rank by total return
    by_return = sorted(results, key=lambda x: _total_return(x, starting_capital), reverse=True)
    console.print("[bold]Rankings by Total Return:[/]")
    for i, r in enumerate(by_return, 1):
        ret = _total_return(r, starting_capital)
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f" {i}.")
        color = "green" if ret >= 0 else "red"
        console.print(f"  {medal} [{color}]{ret:+.2f}%[/{color}] — {r.strategy_name}")

    # Rank by Sharpe ratio
    by_sharpe = sorted(results, key=lambda x: x.metrics.get("sharpe_ratio", 0), reverse=True)
    console.print("\n[bold]Rankings by Sharpe Ratio (risk-adjusted):[/]")
    for i, r in enumerate(by_sharpe, 1):
        sharpe = r.metrics.get("sharpe_ratio", 0)
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f" {i}.")
        color = "green" if sharpe > 0 else "red"
        console.print(f"  {medal} [{color}]{sharpe:.4f}[/{color}] — {r.strategy_name}")

    # Rank by win rate (minimum 5 closed trades)
    qualified = [r for r in results if r.metrics.get("closed_trades", 0) >= 5]
    if qualified:
        by_wr = sorted(qualified, key=lambda x: x.metrics.get("win_rate", 0), reverse=True)
        console.print("\n[bold]Rankings by Win Rate (min 5 closed trades):[/]")
        for i, r in enumerate(by_wr, 1):
            wr = r.metrics.get("win_rate", 0) * 100
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f" {i}.")
            color = "green" if wr >= 50 else "red"
            console.print(f"  {medal} [{color}]{wr:.1f}%[/{color}] — {r.strategy_name}")

    # Rank by max drawdown (least drawdown is best)
    by_dd = sorted(results, key=lambda x: x.metrics.get("max_drawdown", 0), reverse=True)
    console.print("\n[bold]Rankings by Max Drawdown (smallest = best):[/]")
    for i, r in enumerate(reversed(by_dd), 1):
        dd = r.metrics.get("max_drawdown", 0) * 100
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f" {i}.")
        console.print(f"  {medal} [red]{dd:.2f}%[/red] — {r.strategy_name}")


def _print_combined_portfolio(results: list[BacktestResult], starting_capital: float) -> None:
    """Show what an equal-weighted portfolio of all strategies would return."""
    console.print()

    # Collect daily values from all strategies
    all_daily: dict[str, list[float]] = {}
    for r in results:
        if not r.portfolio_values:
            continue
        for dv in r.portfolio_values:
            date = dv["date"]
            all_daily.setdefault(date, []).append(dv["value"])

    if not all_daily:
        return

    # Equal-weight portfolio: average of all strategy portfolio values
    n_strategies = len(results)
    combined = []
    for date in sorted(all_daily.keys()):
        vals = all_daily[date]
        # Normalize: each strategy starts at equal weight
        avg = sum(vals) / len(vals)
        combined.append({"date": date, "value": avg})

    if not combined:
        return

    start_val = combined[0]["value"]
    end_val = combined[-1]["value"]
    combined_return = (end_val - start_val) / start_val * 100

    # Calculate combined Sharpe and max drawdown
    values = pd.Series([c["value"] for c in combined], dtype=float)
    daily_returns = values.pct_change().dropna()

    sharpe = 0.0
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        import numpy as np
        sharpe = float(daily_returns.mean() / daily_returns.std() * np.sqrt(365))

    cummax = values.cummax()
    max_dd = float(((values - cummax) / cummax).min()) * 100

    panel_text = (
        f"[bold]Equal-Weight Combined Portfolio[/] (all {n_strategies} strategies)\n\n"
        f"  Starting Capital:  ${starting_capital:>12,.2f}\n"
        f"  Ending Capital:    ${end_val:>12,.2f}\n"
        f"  Combined Return:   [{'green' if combined_return >= 0 else 'red'}]{combined_return:+.2f}%[/]\n"
        f"  Sharpe Ratio:      {sharpe:.4f}\n"
        f"  Max Drawdown:      [red]{max_dd:.2f}%[/red]\n"
        f"  Period:            {combined[0]['date']} → {combined[-1]['date']}"
    )
    console.print(Panel(panel_text, style="bold cyan", title="Combined Portfolio"))


def _total_return(result: BacktestResult, starting_capital: float) -> float:
    """Calculate total return percentage."""
    if not result.portfolio_values:
        return 0.0
    end_val = result.portfolio_values[-1]["value"]
    return (end_val - starting_capital) / starting_capital * 100


def _save_results(results: list[BacktestResult], days: int, starting_capital: float) -> None:
    """Save backtest results to JSON file."""
    output_dir = Path(__file__).parent.parent / "knowledge" / "backtests"
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"backtest_{days}d_{timestamp}.json"

    summary = []
    for r in results:
        ret = _total_return(r, starting_capital)
        summary.append({
            "strategy": r.strategy_name,
            "total_return_pct": round(ret, 4),
            "metrics": r.metrics,
            "total_trades": r.metrics.get("total_trades", 0),
            "start_date": r.start_date,
            "end_date": r.end_date,
        })

    payload = {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "starting_capital": starting_capital,
        "strategies": sorted(summary, key=lambda x: x["total_return_pct"], reverse=True),
    }

    output_file.write_text(json.dumps(payload, indent=2))
    console.print(f"\n[dim]Results saved to {output_file}[/dim]")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run backtests for all strategies")
    parser.add_argument("--days", type=int, default=730, help="Number of days (default: 730 = 2 years)")
    parser.add_argument("--capital", type=float, default=100_000, help="Starting capital (default: $100,000)")
    parser.add_argument("--strategy", type=str, default=None, help="Run single strategy (default: all)")
    parser.add_argument("--leverage", type=int, default=1, help="Leverage multiplier (default: 1x)")
    parser.add_argument("--verbose", action="store_true", help="Print individual strategy reports")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    strategies = [args.strategy] if args.strategy else None
    results = run_all_backtests(
        days=args.days,
        starting_capital=args.capital,
        strategies=strategies,
        leverage=args.leverage,
    )

    # Optionally print detailed reports
    if args.verbose:
        console.print("\n[bold yellow]Detailed Individual Reports[/]\n")
        for r in results:
            print_backtest_report(r)
            console.print()


if __name__ == "__main__":
    main()
