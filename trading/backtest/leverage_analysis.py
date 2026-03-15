"""Leverage analysis — test all strategies across leverage levels to find optimal risk profiles.

Runs each strategy at 1x, 2x, 3x, 5x, 7x, and 10x leverage, then categorizes
strategies into risk profiles:

    Conservative (1x):  Best risk-adjusted return, minimal liquidation risk
    Moderate (2-3x):    Higher returns with acceptable drawdown
    Aggressive (5x):    Maximum returns for high risk tolerance
    Greedy (10x):       Extreme leverage — liquidation-prone, only for conviction trades

Usage:
    python -m trading.backtest.leverage_analysis [--days 180] [--capital 100000]
    python -m trading.backtest.leverage_analysis --quick   # 90-day quick scan
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from trading.backtest.engine import (
    Backtester,
    BacktestResult,
    _fetch_historical_data,
)
from trading.backtest.run_all import ALL_STRATEGIES

logger = logging.getLogger(__name__)
console = Console()

# Leverage levels to test
LEVERAGE_LEVELS = [1, 2, 3, 5, 7, 10]

# Risk profile definitions
RISK_PROFILES = {
    "Conservative": {"description": "Capital preservation, steady growth", "max_dd": -0.10, "min_sharpe": 0.5},
    "Moderate": {"description": "Balanced risk/reward", "max_dd": -0.25, "min_sharpe": 0.3},
    "Aggressive": {"description": "High returns, accepts volatility", "max_dd": -0.50, "min_sharpe": 0.1},
    "Greedy": {"description": "Maximum returns, high liquidation risk", "max_dd": -1.0, "min_sharpe": -999},
}


def run_leverage_analysis(
    days: int = 180,
    starting_capital: float = 100_000,
    strategies: list[str] | None = None,
) -> dict:
    """Run all strategies at all leverage levels and analyze optimal leverage.

    Returns a dict with per-strategy recommendations and aggregate analysis.
    """
    strategy_names = strategies or ALL_STRATEGIES

    console.print(Panel(
        f"[bold]Leverage Analysis[/]\n"
        f"Period: {days} days | Capital: ${starting_capital:,.0f}\n"
        f"Strategies: {len(strategy_names)} | Leverage levels: {LEVERAGE_LEVELS}",
        style="bold magenta",
    ))

    # Phase 1: Fetch data once
    console.print("\n[bold yellow]Phase 1: Fetching historical data...[/]\n")
    t0 = time.time()
    historical_data = _fetch_historical_data("all", days)
    console.print(f"[green]Data fetched in {time.time() - t0:.1f}s[/]\n")

    end_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    # Phase 2: Run all strategy x leverage combinations
    console.print(f"[bold yellow]Phase 2: Running {len(strategy_names) * len(LEVERAGE_LEVELS)} backtests...[/]\n")

    # results[strategy_name][leverage] = BacktestResult
    results: dict[str, dict[int, BacktestResult]] = {}

    total_runs = len(strategy_names) * len(LEVERAGE_LEVELS)
    run_count = 0

    for strategy_name in strategy_names:
        results[strategy_name] = {}
        for lev in LEVERAGE_LEVELS:
            run_count += 1
            console.print(
                f"  [{run_count}/{total_runs}] {strategy_name} @ {lev}x...",
                end=" ",
            )
            t0 = time.time()

            try:
                backtester = Backtester(
                    starting_capital=starting_capital,
                    commission_pct=0.001,
                    leverage=lev,
                )
                result = backtester.run(
                    strategy_name=strategy_name,
                    historical_data=historical_data,
                    start_date=str(start_date),
                    end_date=str(end_date),
                )
                results[strategy_name][lev] = result
                elapsed = time.time() - t0

                ret = _total_return(result, starting_capital)
                m = result.metrics
                liqs = m.get("liquidations", 0)
                liq_str = f" [red]({liqs} liqs)[/red]" if liqs > 0 else ""
                color = "green" if ret >= 0 else "red"
                console.print(
                    f"[{color}]{ret:+.1f}%[/{color}] "
                    f"DD:{m.get('max_drawdown', 0)*100:.1f}% "
                    f"Sharpe:{m.get('sharpe_ratio', 0):.2f}{liq_str} "
                    f"[dim]({elapsed:.1f}s)[/dim]"
                )
            except Exception as exc:
                console.print(f"[red]FAILED: {exc}[/red]")
                results[strategy_name][lev] = BacktestResult(
                    strategy_name=strategy_name,
                    metrics={"total_trades": 0, "closed_trades": 0, "win_rate": 0,
                             "total_pnl": 0, "avg_trade_pnl": 0, "sharpe_ratio": 0,
                             "max_drawdown": 0, "calmar_ratio": 0, "best_trade": 0,
                             "worst_trade": 0, "leverage": lev, "liquidations": 0,
                             "liquidation_losses": 0},
                    start_date=str(start_date),
                    end_date=str(end_date),
                    starting_capital=starting_capital,
                )

    # Phase 3: Analysis
    console.print(f"\n[bold yellow]Phase 3: Analyzing optimal leverage...[/]\n")

    _print_leverage_matrix(results, starting_capital)
    _print_leverage_risk_matrix(results, starting_capital)
    recommendations = _print_recommendations(results, starting_capital)
    _print_risk_profiles(results, starting_capital)
    _print_portfolio_scenarios(results, starting_capital)

    # Save results
    analysis = _save_analysis(results, recommendations, days, starting_capital)

    return analysis


def _total_return(result: BacktestResult, starting_capital: float) -> float:
    if not result.portfolio_values:
        return 0.0
    return (result.portfolio_values[-1]["value"] - starting_capital) / starting_capital * 100


def _print_leverage_matrix(results: dict, starting_capital: float) -> None:
    """Print return matrix: strategy x leverage level."""
    table = Table(title="Return Matrix (%) — Strategy x Leverage", show_lines=True)
    table.add_column("Strategy", style="bold", min_width=22)
    for lev in LEVERAGE_LEVELS:
        table.add_column(f"{lev}x", justify="right", min_width=10)
    table.add_column("Best Lev", justify="center", style="bold cyan")

    for strategy_name in sorted(results.keys()):
        row = [strategy_name]
        best_lev = 1
        best_ret = -999
        for lev in LEVERAGE_LEVELS:
            r = results[strategy_name].get(lev)
            if r:
                ret = _total_return(r, starting_capital)
                color = "green" if ret >= 0 else "red"
                liqs = r.metrics.get("liquidations", 0)
                liq_mark = "*" if liqs > 0 else ""
                row.append(f"[{color}]{ret:+.1f}%{liq_mark}[/{color}]")
                if ret > best_ret:
                    best_ret = ret
                    best_lev = lev
            else:
                row.append("-")
        row.append(f"{best_lev}x")
        table.add_row(*row)

    console.print(table)
    console.print("[dim]* = liquidations occurred[/dim]\n")


def _print_leverage_risk_matrix(results: dict, starting_capital: float) -> None:
    """Print risk matrix: max drawdown and Sharpe at each leverage."""
    table = Table(title="Risk Matrix — Max Drawdown / Sharpe / Liquidations", show_lines=True)
    table.add_column("Strategy", style="bold", min_width=22)
    for lev in LEVERAGE_LEVELS:
        table.add_column(f"{lev}x", justify="right", min_width=16)

    for strategy_name in sorted(results.keys()):
        row = [strategy_name]
        for lev in LEVERAGE_LEVELS:
            r = results[strategy_name].get(lev)
            if r:
                m = r.metrics
                dd = m.get("max_drawdown", 0) * 100
                sharpe = m.get("sharpe_ratio", 0)
                liqs = m.get("liquidations", 0)
                liq_str = f" L:{liqs}" if liqs > 0 else ""
                row.append(f"DD:{dd:.1f}% S:{sharpe:.2f}{liq_str}")
            else:
                row.append("-")
        table.add_row(*row)

    console.print(table)


def _print_recommendations(results: dict, starting_capital: float) -> dict:
    """Print per-strategy optimal leverage recommendation."""
    console.print()
    table = Table(title="Optimal Leverage Recommendations", show_lines=True)
    table.add_column("Strategy", style="bold", min_width=22)
    table.add_column("Conservative\n(safe)", justify="center")
    table.add_column("Moderate\n(balanced)", justify="center")
    table.add_column("Aggressive\n(high risk)", justify="center")
    table.add_column("Greedy\n(max return)", justify="center")
    table.add_column("Notes")

    recommendations = {}

    for strategy_name in sorted(results.keys()):
        lev_data = results[strategy_name]

        # Find best leverage for each risk profile
        rec = {"conservative": 1, "moderate": 1, "aggressive": 1, "greedy": 1}
        notes = []

        # Conservative: best Sharpe with DD < 10% and no liquidations
        best_sharpe_safe = -999
        for lev in LEVERAGE_LEVELS:
            r = lev_data.get(lev)
            if not r:
                continue
            m = r.metrics
            dd = m.get("max_drawdown", 0)
            sharpe = m.get("sharpe_ratio", 0)
            liqs = m.get("liquidations", 0)
            if dd > -0.10 and liqs == 0 and sharpe > best_sharpe_safe:
                best_sharpe_safe = sharpe
                rec["conservative"] = lev

        # Moderate: best return with DD < 25% and liquidations < 3
        best_ret_mod = -999
        for lev in LEVERAGE_LEVELS:
            r = lev_data.get(lev)
            if not r:
                continue
            m = r.metrics
            dd = m.get("max_drawdown", 0)
            liqs = m.get("liquidations", 0)
            ret = _total_return(r, starting_capital)
            if dd > -0.25 and liqs <= 2 and ret > best_ret_mod:
                best_ret_mod = ret
                rec["moderate"] = lev

        # Aggressive: best return with DD < 50%
        best_ret_agg = -999
        for lev in LEVERAGE_LEVELS:
            r = lev_data.get(lev)
            if not r:
                continue
            m = r.metrics
            dd = m.get("max_drawdown", 0)
            ret = _total_return(r, starting_capital)
            if dd > -0.50 and ret > best_ret_agg:
                best_ret_agg = ret
                rec["aggressive"] = lev

        # Greedy: highest absolute return regardless of risk
        best_ret_greedy = -999
        for lev in LEVERAGE_LEVELS:
            r = lev_data.get(lev)
            if not r:
                continue
            ret = _total_return(r, starting_capital)
            if ret > best_ret_greedy:
                best_ret_greedy = ret
                rec["greedy"] = lev

        # Generate notes
        r_1x = lev_data.get(1)
        r_greedy = lev_data.get(rec["greedy"])
        if r_1x and r_greedy:
            ret_1x = _total_return(r_1x, starting_capital)
            ret_g = _total_return(r_greedy, starting_capital)
            if ret_1x < 0:
                notes.append("loses at 1x")
            liqs_g = r_greedy.metrics.get("liquidations", 0)
            if liqs_g > 0:
                notes.append(f"{liqs_g} liqs at {rec['greedy']}x")

        recommendations[strategy_name] = rec

        table.add_row(
            strategy_name,
            f"[green]{rec['conservative']}x[/green]",
            f"[yellow]{rec['moderate']}x[/yellow]",
            f"[red]{rec['aggressive']}x[/red]",
            f"[bold red]{rec['greedy']}x[/bold red]",
            ", ".join(notes) if notes else "-",
        )

    console.print(table)
    return recommendations


def _print_risk_profiles(results: dict, starting_capital: float) -> None:
    """Print which strategies work best in each risk profile."""
    console.print()

    profiles = {
        "Conservative (1x-2x, Capital Preservation)": [],
        "Moderate (2x-3x, Balanced Growth)": [],
        "Aggressive (3x-5x, High Growth)": [],
        "Greedy (5x-10x, Maximum Returns)": [],
    }

    for strategy_name in sorted(results.keys()):
        lev_data = results[strategy_name]

        # Find which profile this strategy fits best
        r_1x = lev_data.get(1)
        if not r_1x:
            continue

        # Score each leverage level: return * sharpe / (1 + |drawdown|)
        best_score = -999
        best_lev = 1
        for lev in LEVERAGE_LEVELS:
            r = lev_data.get(lev)
            if not r:
                continue
            ret = _total_return(r, starting_capital)
            m = r.metrics
            sharpe = m.get("sharpe_ratio", 0)
            dd = abs(m.get("max_drawdown", 0))
            liqs = m.get("liquidations", 0)
            # Penalize liquidations heavily
            score = ret * max(sharpe, 0.01) / (1 + dd + liqs * 10)
            if score > best_score:
                best_score = score
                best_lev = lev

        ret = _total_return(lev_data.get(best_lev, r_1x), starting_capital)
        entry = (strategy_name, best_lev, ret)

        if best_lev <= 2:
            profiles["Conservative (1x-2x, Capital Preservation)"].append(entry)
        elif best_lev <= 3:
            profiles["Moderate (2x-3x, Balanced Growth)"].append(entry)
        elif best_lev <= 5:
            profiles["Aggressive (3x-5x, High Growth)"].append(entry)
        else:
            profiles["Greedy (5x-10x, Maximum Returns)"].append(entry)

    for profile, strats in profiles.items():
        if not strats:
            continue
        strats.sort(key=lambda x: x[2], reverse=True)
        console.print(f"\n[bold]{profile}[/bold]")
        for name, lev, ret in strats:
            color = "green" if ret >= 0 else "red"
            console.print(f"  {lev}x  [{color}]{ret:+.1f}%[/{color}]  {name}")


def _print_portfolio_scenarios(results: dict, starting_capital: float) -> None:
    """Show combined portfolio performance at each leverage level."""
    console.print()

    rows = []
    for lev in LEVERAGE_LEVELS:
        returns = []
        total_liqs = 0
        sharpes = []
        drawdowns = []

        for strategy_name in results:
            r = results[strategy_name].get(lev)
            if not r or not r.portfolio_values:
                continue
            ret = _total_return(r, starting_capital)
            returns.append(ret)
            total_liqs += r.metrics.get("liquidations", 0)
            sharpes.append(r.metrics.get("sharpe_ratio", 0))
            drawdowns.append(r.metrics.get("max_drawdown", 0))

        if not returns:
            continue

        avg_ret = np.mean(returns)
        avg_sharpe = np.mean(sharpes)
        worst_dd = min(drawdowns)
        avg_dd = np.mean(drawdowns)

        rows.append({
            "leverage": lev,
            "avg_return": avg_ret,
            "avg_sharpe": avg_sharpe,
            "avg_dd": avg_dd * 100,
            "worst_dd": worst_dd * 100,
            "total_liqs": total_liqs,
            "winners": sum(1 for r in returns if r > 0),
            "losers": sum(1 for r in returns if r <= 0),
        })

    table = Table(title="Portfolio Scenarios by Leverage", show_lines=True)
    table.add_column("Leverage", style="bold", justify="center")
    table.add_column("Avg Return", justify="right")
    table.add_column("Avg Sharpe", justify="right")
    table.add_column("Avg DD", justify="right")
    table.add_column("Worst DD", justify="right")
    table.add_column("Liquidations", justify="right")
    table.add_column("W/L", justify="center")

    for r in rows:
        color = "green" if r["avg_return"] >= 0 else "red"
        liq_style = "red" if r["total_liqs"] > 0 else "green"
        table.add_row(
            f"{r['leverage']}x",
            f"[{color}]{r['avg_return']:+.1f}%[/{color}]",
            f"{r['avg_sharpe']:.2f}",
            f"[red]{r['avg_dd']:.1f}%[/red]",
            f"[red]{r['worst_dd']:.1f}%[/red]",
            f"[{liq_style}]{r['total_liqs']}[/{liq_style}]",
            f"{r['winners']}W / {r['losers']}L",
        )

    console.print(table)

    # Find optimal leverage for equal-weight portfolio
    if rows:
        # Best risk-adjusted: highest (avg_return / |avg_dd|) with positive Sharpe
        best = max(rows, key=lambda r: r["avg_return"] * max(r["avg_sharpe"], 0.01) / (1 + abs(r["avg_dd"]) + r["total_liqs"] * 5))
        console.print(
            f"\n[bold cyan]Optimal portfolio leverage: {best['leverage']}x[/bold cyan] "
            f"(return: {best['avg_return']:+.1f}%, Sharpe: {best['avg_sharpe']:.2f}, "
            f"DD: {best['avg_dd']:.1f}%, liquidations: {best['total_liqs']})"
        )


def _save_analysis(
    results: dict, recommendations: dict, days: int, starting_capital: float
) -> dict:
    """Save full leverage analysis to JSON."""
    output_dir = Path(__file__).parent.parent / "knowledge" / "backtests"
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"leverage_analysis_{days}d_{timestamp}.json"

    analysis = {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "starting_capital": starting_capital,
        "leverage_levels": LEVERAGE_LEVELS,
        "recommendations": recommendations,
        "strategy_results": {},
    }

    for strategy_name in sorted(results.keys()):
        strategy_data = {}
        for lev in LEVERAGE_LEVELS:
            r = results[strategy_name].get(lev)
            if not r:
                continue
            strategy_data[str(lev)] = {
                "total_return_pct": round(_total_return(r, starting_capital), 4),
                "metrics": r.metrics,
            }
        analysis["strategy_results"][strategy_name] = strategy_data

    output_file.write_text(json.dumps(analysis, indent=2))
    console.print(f"\n[dim]Analysis saved to {output_file}[/dim]")
    return analysis


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Leverage analysis across all strategies")
    parser.add_argument("--days", type=int, default=180, help="Backtest period in days (default: 180)")
    parser.add_argument("--capital", type=float, default=100_000, help="Starting capital (default: $100,000)")
    parser.add_argument("--strategy", type=str, default=None, help="Single strategy to analyze")
    parser.add_argument("--quick", action="store_true", help="Quick 90-day scan")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    days = 90 if args.quick else args.days
    strategies = [args.strategy] if args.strategy else None

    run_leverage_analysis(
        days=days,
        starting_capital=args.capital,
        strategies=strategies,
    )


if __name__ == "__main__":
    main()
