"""Post-trade analysis — win/loss patterns, strategy grading, performance reviews.

v2: Fixed Sharpe ratio to use daily portfolio returns (not trade P&Ls),
    annualized with sqrt(365) for 24/7 crypto markets.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

from trading.config import REVIEWS_DIR
from trading.db.store import get_trades, get_daily_pnl, insert_review

log = logging.getLogger(__name__)


def calculate_metrics(trades: list[dict]) -> dict:
    """Calculate performance metrics from a list of trades."""
    if not trades:
        return {"total_trades": 0, "win_rate": 0, "total_pnl": 0}

    closed = [t for t in trades if t.get("pnl") is not None]
    if not closed:
        return {"total_trades": len(trades), "win_rate": 0, "total_pnl": 0, "open_trades": len(trades)}

    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    pnls = [t["pnl"] for t in closed]

    win_rate = len(wins) / len(closed) if closed else 0
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0

    # Profit factor = gross wins / gross losses
    gross_wins = sum(t["pnl"] for t in wins)
    gross_losses = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf") if gross_wins > 0 else 0

    # Max drawdown from cumulative PnL
    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    drawdown = (peak - cumulative)
    max_drawdown = np.max(drawdown) if len(drawdown) > 0 else 0

    return {
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "open_trades": len(trades) - len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 3),
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl": round(np.mean(pnls), 2),
        "avg_win": round(float(avg_win), 2),
        "avg_loss": round(float(avg_loss), 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown": round(float(max_drawdown), 2),
        "best_trade": round(max(pnls), 2) if pnls else 0,
        "worst_trade": round(min(pnls), 2) if pnls else 0,
    }


def calculate_sharpe_from_daily_pnl(days: int = 30) -> float:
    """Calculate Sharpe ratio from daily portfolio returns.

    Uses daily_pnl table (portfolio snapshots), NOT individual trade P&Ls.
    Annualized with sqrt(365) since the fund trades crypto 24/7.
    """
    records = get_daily_pnl(limit=days + 1)
    if len(records) < 3:
        return 0.0

    # Records come newest-first; reverse to chronological
    records = list(reversed(records))

    # Compute daily returns from portfolio values
    values = [r["portfolio_value"] for r in records if r["portfolio_value"] and r["portfolio_value"] > 0]
    if len(values) < 3:
        return 0.0

    daily_returns = []
    for i in range(1, len(values)):
        ret = (values[i] - values[i - 1]) / values[i - 1]
        daily_returns.append(ret)

    if not daily_returns:
        return 0.0

    mean_ret = np.mean(daily_returns)
    std_ret = np.std(daily_returns, ddof=1)  # Use sample std

    if std_ret == 0:
        return 0.0

    # Annualize: crypto trades 365 days/year
    sharpe = (mean_ret / std_ret) * np.sqrt(365)
    return round(float(sharpe), 2)


def metrics_by_strategy(trades: list[dict]) -> dict[str, dict]:
    """Break down metrics by strategy."""
    by_strategy = {}
    for trade in trades:
        strategy = trade.get("strategy", "unknown")
        if strategy not in by_strategy:
            by_strategy[strategy] = []
        by_strategy[strategy].append(trade)
    return {name: calculate_metrics(trades) for name, trades in by_strategy.items()}


def generate_review(period: str = "weekly") -> str:
    """Generate a performance review as markdown.

    Args:
        period: 'weekly' or 'monthly'

    Returns markdown string and saves to REVIEWS_DIR.
    """
    days = 7 if period == "weekly" else 30
    all_trades = get_trades(limit=500)

    # Filter to period
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    period_trades = [t for t in all_trades if t["timestamp"] >= cutoff]

    overall = calculate_metrics(period_trades)
    by_strat = metrics_by_strategy(period_trades)

    # Proper Sharpe from daily portfolio returns
    sharpe = calculate_sharpe_from_daily_pnl(days=days)

    # Find best/worst strategy
    best_strategy = max(by_strat.items(), key=lambda x: x[1].get("total_pnl", 0), default=("none", {}))[0] if by_strat else "none"
    worst_strategy = min(by_strat.items(), key=lambda x: x[1].get("total_pnl", 0), default=("none", {}))[0] if by_strat else "none"

    # Build markdown
    now = datetime.now(timezone.utc)
    start_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    lines = [
        f"# {'Weekly' if period == 'weekly' else 'Monthly'} Performance Review",
        f"**Period**: {start_date} to {end_date}\n",
        "## Overall Metrics",
        f"- **Total Trades**: {overall['total_trades']}",
        f"- **Win Rate**: {overall['win_rate']*100:.1f}%",
        f"- **Total P&L**: ${overall['total_pnl']:+.2f}",
        f"- **Sharpe Ratio**: {sharpe:.2f} (annualized, sqrt365)",
        f"- **Profit Factor**: {overall.get('profit_factor', 0):.2f}",
        f"- **Max Drawdown**: ${overall.get('max_drawdown', 0):.2f}",
        f"- **Best Trade**: ${overall.get('best_trade', 0):+.2f}",
        f"- **Worst Trade**: ${overall.get('worst_trade', 0):+.2f}",
        "",
        "## By Strategy",
    ]

    for name, metrics in sorted(by_strat.items()):
        lines.append(f"\n### {name}")
        lines.append(f"- Trades: {metrics['total_trades']} | Win rate: {metrics['win_rate']*100:.1f}%")
        lines.append(f"- P&L: ${metrics['total_pnl']:+.2f} | Avg: ${metrics.get('avg_pnl', 0):+.2f}")
        lines.append(f"- Avg win: ${metrics.get('avg_win', 0):+.2f} | Avg loss: ${metrics.get('avg_loss', 0):+.2f}")
        lines.append(f"- Profit factor: {metrics.get('profit_factor', 0):.2f}")

    lines.append(f"\n## Summary")
    lines.append(f"- **Best strategy**: {best_strategy}")
    lines.append(f"- **Worst strategy**: {worst_strategy}")

    if overall["total_trades"] == 0:
        lines.append("- No trades in this period.")
    elif overall["win_rate"] >= 0.5:
        lines.append(f"- Positive win rate at {overall['win_rate']*100:.0f}%. Continue current approach.")
    else:
        lines.append(f"- Win rate below 50% at {overall['win_rate']*100:.0f}%. Review entry criteria.")

    content = "\n".join(lines)

    # Save to file
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = REVIEWS_DIR / f"{period}-{end_date}.md"
    filepath.write_text(content)

    # Record in database
    insert_review(
        period=period,
        start_date=start_date,
        end_date=end_date,
        total_trades=overall["total_trades"],
        win_rate=overall["win_rate"],
        total_pnl=overall["total_pnl"],
        sharpe_ratio=sharpe,
        max_drawdown=overall.get("max_drawdown", 0),
        best_strategy=best_strategy,
        worst_strategy=worst_strategy,
        summary=content,
        file_path=str(filepath),
    )

    log.info("Generated %s review: %d trades, $%+.2f P&L, Sharpe %.2f",
             period, overall["total_trades"], overall["total_pnl"], sharpe)

    return content
