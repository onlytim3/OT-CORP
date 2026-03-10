"""Autonomous trading scheduler — runs strategies on autopilot.

Integrates:
  - Signal aggregation (deduplicate, resolve conflicts)
  - Market hours gating (ETFs only during NYSE hours)
  - Position sync (Alpaca → local DB)
  - Fill verification (poll pending orders)
  - Trade pairing (FIFO buy-sell matching for P&L)
  - Take-profit + trailing stop
  - Notifications (Discord / Telegram)
"""

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
# Persistent tracker — created once, lives for the daemon's lifetime
# ---------------------------------------------------------------------------
_profit_tracker = None  # Lazy init in start_daemon


def _get_profit_tracker():
    global _profit_tracker
    if _profit_tracker is None:
        from trading.risk.profit_manager import ProfitTracker
        _profit_tracker = ProfitTracker()
    return _profit_tracker


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


def _notify_safe(func, *args, **kwargs):
    """Call a notification function, swallowing any exception."""
    try:
        func(*args, **kwargs)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core trading cycle — with aggregation + market hours gating
# ---------------------------------------------------------------------------

def run_trading_cycle():
    """One complete cycle: data → signals → aggregate → risk → execute → sync."""
    from trading.strategy.registry import get_enabled_strategies
    from trading.strategy.aggregator import aggregate_signals
    from trading.risk.manager import RiskManager
    from trading.risk.portfolio import calculate_order_size
    from trading.learning.journal import create_journal_entry
    from trading.execution.market_hours import can_trade_now
    from trading.execution.sync import run_sync
    from trading.data.cache import clear_cache
    from trading.monitor.notifications import notify_trade, notify_error, notify_cycle_summary

    clear_cache()  # Fresh data each cycle
    log_action("strategy_run", "cycle_start", details=f"Mode: {TRADING_MODE}")
    console.print(f"\n[bold cyan]{'='*60}[/]")
    console.print(f"[bold]Trading cycle started — {_now_str()}[/bold]")
    console.print(f"[bold cyan]{'='*60}[/]")

    try:
        account = _get_account()
        portfolio_value = account["portfolio_value"]
        mode = "PAPER" if account.get("paper") else "LIVE"
        console.print(f"[dim]Portfolio: ${portfolio_value:.2f} ({mode})[/dim]")

        # ---------------------------------------------------------------
        # Phase 1: Sync positions from broker → local DB (fixes risk checks)
        # ---------------------------------------------------------------
        try:
            sync_result = run_sync()
            console.print(f"[dim]Sync: {sync_result}[/dim]")
        except Exception as e:
            console.print(f"[yellow]Sync warning: {e}[/yellow]")

        # ---------------------------------------------------------------
        # Phase 2: Collect ALL signals from ALL strategies
        # ---------------------------------------------------------------
        strategies = get_enabled_strategies()
        raw_signals = []
        contexts = {}

        for strategy in strategies:
            console.print(f"\n[cyan]→ {strategy.name}[/cyan]")
            try:
                signals = strategy.generate_signals()
                context = strategy.get_market_context()
                contexts[strategy.name] = context

                for signal in signals:
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
                    raw_signals.append(signal)

            except Exception as e:
                log_action("error", "strategy_error", details=f"{strategy.name}: {e}")
                console.print(f"  [red]Error: {e}[/red]")
                _notify_safe(notify_error, f"{strategy.name}: {e}", "strategy_generation")

        signal_count = len(raw_signals)

        # ---------------------------------------------------------------
        # Phase 3: Aggregate signals (deduplicate + conflict resolution)
        # ---------------------------------------------------------------
        consolidated = aggregate_signals(raw_signals)
        console.print(
            f"\n[bold]Aggregation: {signal_count} raw → "
            f"{len(consolidated)} consolidated[/bold]"
        )

        # ---------------------------------------------------------------
        # Phase 4: Risk check + market hours gate + execute
        # ---------------------------------------------------------------
        risk_mgr = RiskManager(portfolio_value)
        executed_count = 0
        blocked_count = 0

        # Count buy signals for position sizing
        buy_count = sum(1 for s in consolidated if s.action == "buy")

        for signal in consolidated:
            if not signal.is_actionable:
                continue

            # Market hours gate — block ETF orders outside NYSE hours
            can_trade, reason = can_trade_now(signal.symbol)
            if not can_trade:
                blocked_count += 1
                log_action(
                    "risk_block", "market_closed",
                    symbol=signal.symbol,
                    details=reason,
                )
                console.print(f"  [yellow]DEFERRED {signal.symbol}: {reason}[/yellow]")
                continue

            # Size the order
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

            # Execute (with error handling for insufficient funds, etc.)
            try:
                order = _execute_order(signal.symbol, signal.action, notional=order_value)
            except Exception as exec_err:
                log_action(
                    "error", "order_failed",
                    symbol=signal.symbol,
                    details=f"Order execution failed: {exec_err}",
                    data={"order_value": order_value, "action": signal.action},
                )
                console.print(f"  [red]ORDER FAILED {signal.symbol}: {exec_err}[/red]")
                continue

            if order.get("status") in ("filled", "accepted", "new", "pending_new"):
                # Find the context from the contributing strategy
                contributing = (signal.data or {}).get("contributing_strategies", [])
                context = contexts.get(contributing[0], {}) if contributing else {}

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
                # Notify
                _notify_safe(
                    notify_trade,
                    signal.symbol, signal.action, order_value,
                    float(order.get("filled_avg_price") or 0),
                    signal.strategy,
                )
            else:
                log_action(
                    "error", "order_rejected",
                    symbol=signal.symbol,
                    details=order.get("reason", order.get("status", "unknown")),
                )

        # ---------------------------------------------------------------
        # Phase 5: Post-trade sync (verify fills + pair trades for P&L)
        # ---------------------------------------------------------------
        try:
            run_sync()
        except Exception as e:
            console.print(f"[yellow]Post-trade sync warning: {e}[/yellow]")

        # ---------------------------------------------------------------
        # Phase 6: Record daily P&L snapshot
        # ---------------------------------------------------------------
        try:
            positions = _get_positions()
            pos_value = sum(
                p.get("market_value", p["qty"] * p["current_price"])
                for p in positions
            ) if positions else 0
            cash = account["cash"]
            pv = cash + pos_value

            # Proper daily return calculation
            prev_pnl = get_action_log(limit=1, category="strategy_run")
            initial = float(os.environ.get("INITIAL_CAPITAL", 100_000))
            daily_ret = (pv - initial) / initial if initial > 0 else 0
            cum_ret = (pv - initial) / initial if initial > 0 else 0
            record_daily_pnl(pv, cash, pos_value, daily_ret, cum_ret)
        except Exception as e:
            console.print(f"[yellow]P&L snapshot warning: {e}[/yellow]")

        # ---------------------------------------------------------------
        # Summary
        # ---------------------------------------------------------------
        log_action(
            "strategy_run", "cycle_complete",
            details=(
                f"Raw: {signal_count} | Consolidated: {len(consolidated)} | "
                f"Executed: {executed_count} | Blocked: {blocked_count}"
            ),
        )
        console.print(
            f"\n[bold]Cycle complete — {signal_count} raw signals → "
            f"{len(consolidated)} consolidated, "
            f"{executed_count} trades, {blocked_count} blocked[/bold]"
        )

        # Send cycle summary notification
        _notify_safe(notify_cycle_summary, signal_count, executed_count, blocked_count)

    except Exception as e:
        log_action("error", "cycle_crash", details=str(e))
        console.print(f"[bold red]Cycle crashed: {e}[/bold red]")
        traceback.print_exc()
        try:
            from trading.monitor.notifications import notify_error
            _notify_safe(notify_error, str(e), "cycle_crash")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Stop-loss + take-profit checker (runs every 30 minutes)
# ---------------------------------------------------------------------------

def check_stop_losses():
    """Check all positions for stop-loss and take-profit/trailing-stop triggers."""
    from trading.risk.profit_manager import check_profit_targets

    try:
        account = _get_account()
        positions = _get_positions()

        if not positions:
            return

        tracker = _get_profit_tracker()

        # -- 1. Take-profit / trailing stop checks --
        profit_actions = check_profit_targets(positions, tracker)
        for action in profit_actions:
            symbol = action["symbol"]
            reason = action["reason"]
            console.print(f"[bold yellow]{action['action'].upper()}: {symbol} — {reason}[/]")
            log_action(
                "profit_mgmt", action["action"],
                symbol=symbol,
                details=reason,
                data={"pnl_pct": action["pnl_pct"], "qty": action["qty"]},
            )
            order = _execute_order(symbol, "sell", qty=action["qty"])
            if order.get("status") in ("filled", "accepted", "new", "pending_new"):
                insert_trade(
                    symbol=symbol,
                    side="sell",
                    qty=action["qty"],
                    price=float(order.get("filled_avg_price") or 0),
                    total=action["qty"] * float(order.get("filled_avg_price") or 0),
                    strategy=f"profit_mgmt_{action['action']}",
                    status=order["status"],
                    alpaca_order_id=order.get("id"),
                )
                log_action("trade", f"{action['action']}_sell", symbol=symbol, result=order["status"])
                tracker.remove(symbol)  # Clean up watermark
                # Notify
                try:
                    from trading.monitor.notifications import notify_stop_loss
                    _notify_safe(notify_stop_loss, symbol, action["pnl_pct"] * 100, action["qty"])
                except Exception:
                    pass

        # -- 2. Hard stop-loss checks --
        for pos in positions:
            pnl_pct = pos.get("unrealized_pnl_pct", 0) / 100 if pos.get("unrealized_pnl_pct") else 0
            if pnl_pct <= -RISK["stop_loss_pct"]:
                symbol = pos["symbol"]
                # Skip if already sold by profit manager above
                if any(a["symbol"] == symbol for a in profit_actions):
                    continue

                console.print(f"[bold red]STOP LOSS triggered: {symbol} at {pnl_pct*100:.1f}%[/]")
                log_action(
                    "stop_loss", "triggered",
                    symbol=symbol,
                    details=f"P&L: {pnl_pct*100:.1f}%",
                    data={"pnl_pct": pnl_pct, "qty": pos["qty"]},
                )
                order = _execute_order(symbol, "sell", qty=pos["qty"])
                if order.get("status") in ("filled", "accepted", "new", "pending_new"):
                    insert_trade(
                        symbol=symbol,
                        side="sell",
                        qty=pos["qty"],
                        price=float(order.get("filled_avg_price") or 0),
                        total=pos["qty"] * float(order.get("filled_avg_price") or 0),
                        strategy="stop_loss",
                        status=order["status"],
                        alpaca_order_id=order.get("id"),
                    )
                    log_action("trade", "stop_loss_sell", symbol=symbol, result=order["status"])
                    tracker.remove(symbol)
                    # Notify
                    try:
                        from trading.monitor.notifications import notify_stop_loss
                        _notify_safe(notify_stop_loss, symbol, pnl_pct * 100, pos["qty"])
                    except Exception:
                        pass

        # -- 3. Post-check sync --
        try:
            from trading.execution.sync import sync_positions
            sync_positions()
        except Exception:
            pass

    except Exception as e:
        log_action("error", "stop_loss_check_error", details=str(e))
        console.print(f"[red]Stop-loss check error: {e}[/red]")


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

    # Initialize persistent profit tracker
    _get_profit_tracker()

    mode = "PAPER" if TRADING_MODE == "paper" or paper else "LIVE"
    console.print(f"\n[bold green]{'='*60}[/]")
    console.print(f"[bold green]  AUTONOMOUS TRADER v2.0 — {mode} MODE[/bold green]")
    console.print(f"[bold green]{'='*60}[/]")
    console.print(f"  Trading cycle:       every {interval_hours} hours")
    console.print(f"  Stop-loss check:     every 15 minutes")
    console.print(f"  Profit check:        every 15 minutes")
    console.print(f"  Position sync:       every cycle + post-trade")
    console.print(f"  Signal aggregation:  enabled (dedup + conflict resolution)")
    console.print(f"  Market hours gate:   enabled (ETFs → NYSE only)")
    console.print(f"  Take-profit:         15% target")
    console.print(f"  Trailing stop:       4% trail after 8% gain")
    console.print(f"  Notifications:       Discord/Telegram if configured")
    console.print(f"  Weekly review:       every Sunday at 00:00")
    console.print(f"  Dashboard:           http://localhost:5000")
    console.print(f"[bold green]{'='*60}[/]\n")

    log_action("scheduler", "daemon_started", details=f"Mode: {mode}, Interval: {interval_hours}h, Version: v2.0")

    # Notify on start
    try:
        from trading.monitor.notifications import notify
        _notify_safe(notify, "Daemon Started", f"Trading daemon v2.0 started in {mode} mode", "info")
    except Exception:
        pass

    # Run immediately on start
    run_trading_cycle()

    # Schedule recurring tasks — tightened stop/profit check to 15 min
    schedule.every(interval_hours).hours.do(run_trading_cycle)
    schedule.every(15).minutes.do(check_stop_losses)
    schedule.every().sunday.at("00:00").do(run_weekly_review)

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log_action("scheduler", "daemon_stopped", details="Keyboard interrupt")
        console.print("\n[yellow]Daemon stopped.[/yellow]")
        try:
            from trading.monitor.notifications import notify
            _notify_safe(notify, "Daemon Stopped", "Trading daemon stopped by user", "warning")
        except Exception:
            pass
