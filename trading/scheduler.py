"""Autonomous trading scheduler — runs strategies on autopilot."""

import os
import time
import traceback
from datetime import datetime, timezone

import schedule
from rich.console import Console

from trading.config import TRADING_MODE, RISK
from trading.db.store import (
    init_db, insert_trade, insert_signal, log_action,
    record_daily_pnl, get_action_log,
)

console = Console()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _get_account():
    if TRADING_MODE == "paper":
        from trading.execution.paper import get_paper_account
        return get_paper_account()
    from trading.execution.alpaca_client import get_account
    return get_account()


def _get_positions():
    if TRADING_MODE == "paper":
        from trading.execution.paper import get_paper_positions
        return get_paper_positions()
    from trading.execution.alpaca_client import get_positions_from_alpaca
    return get_positions_from_alpaca()


def _execute_order(symbol, side, notional=None, qty=None):
    if TRADING_MODE == "paper":
        from trading.execution.paper import submit_paper_order
        return submit_paper_order(symbol, side, notional=notional, qty=qty)
    from trading.execution.alpaca_client import submit_order
    return submit_order(symbol, side, notional=notional, qty=qty)


# ---------------------------------------------------------------------------
# Core trading cycle
# ---------------------------------------------------------------------------

def run_trading_cycle():
    """One complete cycle: collect data → generate signals → risk check → execute."""
    from trading.strategy.registry import get_enabled_strategies
    from trading.risk.manager import RiskManager
    from trading.risk.portfolio import calculate_order_size
    from trading.learning.journal import create_journal_entry
    from trading.data.cache import clear_cache

    clear_cache()  # Fresh data each cycle
    log_action("strategy_run", "cycle_start", details=f"Mode: {TRADING_MODE}")
    console.print(f"\n[bold cyan]{'='*50}[/]")
    console.print(f"[bold]Trading cycle started — {_now_str()}[/bold]")
    console.print(f"[bold cyan]{'='*50}[/]")

    try:
        account = _get_account()
        portfolio_value = account["portfolio_value"]
        mode = "PAPER" if account.get("paper") else "LIVE"
        console.print(f"[dim]Portfolio: ${portfolio_value:.2f} ({mode})[/dim]")

        strategies = get_enabled_strategies()

        risk_mgr = RiskManager(portfolio_value)
        executed_count = 0
        signal_count = 0
        blocked_count = 0

        for strategy in strategies:
            console.print(f"\n[cyan]→ {strategy.name}[/cyan]")
            try:
                signals = strategy.generate_signals()
                context = strategy.get_market_context()

                for signal in signals:
                    signal_count += 1
                    insert_signal(
                        signal.strategy, signal.symbol, signal.action,
                        signal.strength, signal.data,
                    )
                    log_action(
                        "signal", signal.action,
                        symbol=signal.symbol,
                        details=signal.reason,
                        data={"strength": signal.strength, "strategy": signal.strategy},
                    )

                    if not signal.is_actionable:
                        continue

                    # Size the order
                    buy_count = sum(1 for s in signals if s.action == "buy")
                    order_value = calculate_order_size(
                        signal, portfolio_value, buy_count
                    )
                    if order_value <= 0:
                        console.print(f"  [dim]Skip {signal.symbol} — too small[/dim]")
                        continue

                    # Risk check
                    risk_check = risk_mgr.check_trade(signal, order_value)
                    if not risk_check.allowed:
                        blocked_count += 1
                        log_action(
                            "risk_block", "trade_blocked",
                            symbol=signal.symbol,
                            details=risk_check.reason,
                            data={"order_value": order_value},
                        )
                        console.print(f"  [red]BLOCKED {signal.symbol}: {risk_check.reason}[/red]")
                        continue

                    # Execute
                    order = _execute_order(signal.symbol, signal.action, notional=order_value)

                    if order.get("status") in ("filled", "accepted", "new", "pending_new"):
                        trade_id = insert_trade(
                            symbol=signal.symbol,
                            side=signal.action,
                            qty=float(order.get("filled_qty") or order.get("qty") or 0),
                            price=float(order.get("filled_avg_price") or 0),
                            total=order_value,
                            strategy=signal.strategy,
                            status=order["status"],
                            alpaca_order_id=order.get("id"),
                        )
                        create_journal_entry(trade_id, signal, context)
                        executed_count += 1
                        log_action(
                            "trade", signal.action,
                            symbol=signal.symbol,
                            details=f"${order_value:.2f}",
                            result=order["status"],
                            data={
                                "trade_id": trade_id,
                                "qty": float(order.get("filled_qty") or 0),
                                "price": float(order.get("filled_avg_price") or 0),
                            },
                        )
                        side_style = "green" if signal.action == "buy" else "red"
                        console.print(
                            f"  [{side_style}]{signal.action.upper()} "
                            f"${order_value:.2f} {signal.symbol} — {order['status']}[/]"
                        )
                    else:
                        log_action(
                            "error", "order_rejected",
                            symbol=signal.symbol,
                            details=order.get("reason", order.get("status", "unknown")),
                        )

            except Exception as e:
                log_action("error", "strategy_error", details=f"{strategy.name}: {e}")
                console.print(f"  [red]Error: {e}[/red]")

        # Record daily P&L snapshot
        try:
            positions = _get_positions()
            pos_value = sum(
                p.get("market_value", p["qty"] * p["current_price"])
                for p in positions
            ) if positions else 0
            cash = account["cash"]
            pv = cash + pos_value
            initial = float(os.environ.get("INITIAL_CAPITAL", pv))
            daily_ret = (pv - initial) / initial if initial > 0 else 0
            record_daily_pnl(pv, cash, pos_value, daily_ret, daily_ret)
        except Exception:
            pass

        log_action(
            "strategy_run", "cycle_complete",
            details=f"Signals: {signal_count} | Executed: {executed_count} | Blocked: {blocked_count}",
        )
        console.print(
            f"\n[bold]Cycle complete — {signal_count} signals, "
            f"{executed_count} trades, {blocked_count} blocked[/bold]"
        )

    except Exception as e:
        log_action("error", "cycle_crash", details=str(e))
        console.print(f"[bold red]Cycle crashed: {e}[/bold red]")
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Stop-loss checker
# ---------------------------------------------------------------------------

def check_stop_losses():
    """Check all positions for stop-loss triggers."""
    from trading.risk.manager import RiskManager

    try:
        account = _get_account()
        portfolio_value = account["portfolio_value"]
        risk_mgr = RiskManager(portfolio_value)
        positions = _get_positions()

        if not positions:
            return

        for pos in positions:
            pnl_pct = pos.get("unrealized_pnl_pct", 0) / 100 if pos.get("unrealized_pnl_pct") else 0
            if pnl_pct <= -RISK["stop_loss_pct"]:
                symbol = pos["symbol"]
                console.print(f"[bold red]STOP LOSS triggered: {symbol} at {pnl_pct*100:.1f}%[/]")
                log_action(
                    "stop_loss", "triggered",
                    symbol=symbol,
                    details=f"P&L: {pnl_pct*100:.1f}%",
                    data={"pnl_pct": pnl_pct, "qty": pos["qty"]},
                )
                # Close position
                order = _execute_order(symbol, "sell", qty=pos["qty"])
                if order.get("status") in ("filled", "accepted", "new", "pending_new"):
                    log_action("trade", "stop_loss_sell", symbol=symbol, result=order["status"])

    except Exception as e:
        log_action("error", "stop_loss_check_error", details=str(e))


# ---------------------------------------------------------------------------
# Weekly review
# ---------------------------------------------------------------------------

def run_weekly_review():
    """Generate a weekly performance review."""
    try:
        from trading.learning.reviewer import generate_review
        generate_review("weekly")
        log_action("review", "weekly_review_generated")
        console.print("[green]Weekly review generated.[/green]")
    except Exception as e:
        log_action("error", "review_error", details=str(e))


# ---------------------------------------------------------------------------
# Daemon entry point
# ---------------------------------------------------------------------------

def start_daemon(interval_hours=4, paper=False):
    """Start the autonomous trading daemon."""
    if paper:
        import trading.config as cfg
        cfg.TRADING_MODE = "paper"

    init_db()

    mode = "PAPER" if TRADING_MODE == "paper" or paper else "LIVE"
    console.print(f"\n[bold green]{'='*50}[/]")
    console.print(f"[bold green]  AUTONOMOUS TRADER STARTED — {mode} MODE[/bold green]")
    console.print(f"[bold green]{'='*50}[/]")
    console.print(f"  Trading cycle: every {interval_hours} hours")
    console.print(f"  Stop-loss check: every 30 minutes")
    console.print(f"  Weekly review: every Sunday at 00:00")
    console.print(f"  Dashboard: http://localhost:5000")
    console.print(f"[bold green]{'='*50}[/]\n")

    log_action("scheduler", "daemon_started", details=f"Mode: {mode}, Interval: {interval_hours}h")

    # Run immediately on start
    run_trading_cycle()

    # Schedule recurring tasks
    schedule.every(interval_hours).hours.do(run_trading_cycle)
    schedule.every(30).minutes.do(check_stop_losses)
    schedule.every().sunday.at("00:00").do(run_weekly_review)

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log_action("scheduler", "daemon_stopped", details="Keyboard interrupt")
        console.print("\n[yellow]Daemon stopped.[/yellow]")
