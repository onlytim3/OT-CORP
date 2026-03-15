"""Autonomous trading scheduler v3.0 — runs strategies on autopilot.

Integrates:
  - Structured logging (Python logging module)
  - Signal aggregation (deduplicate, resolve conflicts)
  - Market hours gating (ETFs only during NYSE hours)
  - Position sync (Alpaca -> local DB)
  - Fill verification (poll pending orders)
  - Trade pairing (FIFO buy-sell matching for P&L)
  - Take-profit + trailing stop
  - Notifications (Discord / Telegram)
  - Correlation limits + crypto exposure cap
  - Volatility-adjusted position sizing (ATR)
  - Per-strategy risk budgets
  - Order retry with exponential backoff
  - Approved adaptation application
  - Account safety checks (trading_blocked, buying_power)
  - Proper daily P&L calculation (prev-day comparison)
"""

import logging
import os
import signal as signal_mod
import time
import traceback
from datetime import datetime, timezone

import schedule
from rich.console import Console

from trading.config import TRADING_MODE, RISK
from trading.db.store import (
    init_db, insert_trade, insert_signal, log_action,
    record_daily_pnl, get_action_log, get_daily_pnl,
)

log = logging.getLogger(__name__)
console = Console()

# ---------------------------------------------------------------------------
# Persistent tracker -- created once, lives for the daemon's lifetime
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
    """Get account from AsterDex (primary execution venue)."""
    from trading.execution.router import get_account
    return get_account()


def _get_positions():
    """Get positions from AsterDex."""
    from trading.execution.router import get_positions_from_aster
    return get_positions_from_aster()


def _execute_order(symbol, side, notional=None, qty=None):
    """Execute order via AsterDex perpetual futures."""
    from trading.execution.router import submit_order
    return submit_order(symbol, side, notional=notional, qty=qty)


def _notify_safe(func, *args, **kwargs):
    """Call a notification function, swallowing any exception."""
    try:
        func(*args, **kwargs)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core trading cycle -- with aggregation + market hours gating
# ---------------------------------------------------------------------------

def run_trading_cycle():
    """One complete cycle: data -> signals -> aggregate -> risk -> execute -> sync."""
    from trading.strategy.registry import get_enabled_strategies
    from trading.strategy.aggregator import aggregate_signals
    from trading.risk.manager import RiskManager
    from trading.risk.portfolio import calculate_order_size
    from trading.learning.journal import create_journal_entry
    from trading.execution.market_hours import can_trade_now
    from trading.execution.sync import run_sync
    from trading.data.cache import clear_cache
    from trading.risk.portfolio import clear_allocation_cache
    from trading.monitor.notifications import notify_trade, notify_error, notify_cycle_summary

    clear_cache()  # Fresh data each cycle
    clear_allocation_cache()  # Reset dynamic sizing caches (confluence, regime, perf)
    log_action("strategy_run", "cycle_start", details=f"Mode: {TRADING_MODE}")
    log.info("Trading cycle started")
    console.print(f"\n[bold cyan]{'='*60}[/]")
    console.print(f"[bold]Trading cycle started -- {_now_str()}[/bold]")
    console.print(f"[bold cyan]{'='*60}[/]")

    try:
        account = _get_account()
        portfolio_value = account["portfolio_value"]
        mode = "PAPER" if account.get("paper") else "LIVE"
        console.print(f"[dim]Portfolio: ${portfolio_value:.2f} ({mode})[/dim]")

        # ---------------------------------------------------------------
        # Phase 0: Account safety checks
        # ---------------------------------------------------------------
        if account.get("trading_blocked"):
            msg = "Trading blocked by broker -- aborting cycle"
            log.error(msg)
            console.print(f"[bold red]{msg}[/bold red]")
            log_action("error", "trading_blocked", details=msg)
            _notify_safe(notify_error, msg, "account_check")
            return

        # ---------------------------------------------------------------
        # Phase 0.5: Apply approved parameter adaptations
        # ---------------------------------------------------------------
        try:
            from trading.learning.adaptor import apply_approved
            applied = apply_approved()
            if applied:
                for a in applied:
                    console.print(f"  [magenta]ADAPTED {a['strategy']}.{a['param']}: {a['old']} -> {a['new']}[/]")
                log_action("system", "adaptations_applied", details=f"{len(applied)} params updated")
        except Exception as e:
            log.warning("Adaptation application failed: %s", e)

        # ---------------------------------------------------------------
        # Phase 1: Sync positions from broker -> local DB (fixes risk checks)
        # ---------------------------------------------------------------
        try:
            sync_result = run_sync()
            console.print(f"[dim]Sync: {sync_result}[/dim]")
        except Exception as e:
            log.warning("Sync warning: %s", e)
            console.print(f"[yellow]Sync warning: {e}[/yellow]")

        # ---------------------------------------------------------------
        # Phase 1.5: Market Intelligence Briefing
        # ---------------------------------------------------------------
        briefing = None
        try:
            from trading.intelligence.engine import generate_briefing
            briefing = generate_briefing()
            console.print(f"\n[bold gold1]Intelligence:[/] {briefing.summary()}")
            for cat, info in briefing.categories.items():
                score = info['score']
                color = "green" if score > 0.1 else "red" if score < -0.1 else "dim"
                conf = info.get('confidence', 0)
                comps = ", ".join(info.get('components', []))
                console.print(f"  [{color}]{cat}: {score:+.2f} ({info['label']}, {conf:.0%} conf)[/{color}]")
                if comps:
                    console.print(f"    [dim]{comps}[/dim]")
            if briefing.event_risk:
                console.print(f"  [bold red]EVENT RISK: {', '.join(briefing.event_risk)}[/bold red]")
            log_action("intelligence", "briefing", details=briefing.summary(),
                       data=briefing.to_dict())
        except Exception as e:
            log.warning("Intelligence briefing failed (non-fatal): %s", e)
            console.print(f"[yellow]Intelligence briefing skipped: {e}[/yellow]")

        # ---------------------------------------------------------------
        # Phase 2: Collect ALL signals from ALL strategies
        # ---------------------------------------------------------------
        strategies = get_enabled_strategies()
        raw_signals = []
        contexts = {}
        if briefing:
            contexts["_intelligence"] = briefing.to_dict()

        for strategy in strategies:
            console.print(f"\n[cyan]-> {strategy.name}[/cyan]")
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
                log.error("Strategy %s failed: %s", strategy.name, e)
                console.print(f"  [red]Error: {e}[/red]")
                _notify_safe(notify_error, f"{strategy.name}: {e}", "strategy_generation")

        signal_count = len(raw_signals)

        # ---------------------------------------------------------------
        # Phase 3: Aggregate signals (deduplicate + conflict resolution)
        # ---------------------------------------------------------------
        consolidated = aggregate_signals(raw_signals)
        log.info("Aggregation: %d raw -> %d consolidated", signal_count, len(consolidated))
        console.print(
            f"\n[bold]Aggregation: {signal_count} raw -> "
            f"{len(consolidated)} consolidated[/bold]"
        )

        # ---------------------------------------------------------------
        # Phase 3.5: Replay deferred signals from previous cycles
        # ---------------------------------------------------------------
        try:
            from trading.db.store import get_deferred_signals, clear_deferred_signal
            deferred = get_deferred_signals()
            if deferred:
                from trading.strategy.base import Signal
                for ds in deferred:
                    can_trade_deferred, _ = can_trade_now(ds["symbol"])
                    if can_trade_deferred:
                        replay_signal = Signal(
                            strategy=ds["strategy"],
                            symbol=ds["symbol"],
                            action=ds["action"],
                            strength=ds["strength"],
                            reason=f"Deferred replay: {ds['reason']}",
                        )
                        consolidated.append(replay_signal)
                        clear_deferred_signal(ds["id"])
                        console.print(f"  [green]REPLAYED deferred {ds['symbol']} {ds['action']}[/green]")
                        log_action("signal", "deferred_replay", symbol=ds["symbol"],
                                   details=f"Replayed from {ds['timestamp']}")
                    # Clean up expired signals silently
                # Re-log consolidated count if replays added
                if any(can_trade_now(ds["symbol"])[0] for ds in deferred):
                    log.info("After deferred replay: %d consolidated signals", len(consolidated))
        except Exception as e:
            log.warning("Deferred signal replay failed: %s", e)

        # ---------------------------------------------------------------
        # Phase 4: Risk check + market hours gate + execute
        # ---------------------------------------------------------------
        risk_mgr = RiskManager(portfolio_value, account=account)
        executed_count = 0
        blocked_count = 0

        # Count buy signals for position sizing
        buy_count = sum(1 for s in consolidated if s.action == "buy")

        for signal in consolidated:
            if not signal.is_actionable:
                continue

            # Market hours gate -- block ETF orders outside NYSE hours
            can_trade, reason = can_trade_now(signal.symbol)
            if not can_trade:
                blocked_count += 1
                # Queue signal for execution when market opens (expires in 8h)
                from trading.db.store import save_deferred_signal
                from datetime import timedelta
                expires = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
                try:
                    save_deferred_signal(
                        symbol=signal.symbol,
                        action=signal.action,
                        strength=signal.strength,
                        strategy=signal.strategy,
                        reason=reason,
                        expires_at=expires,
                    )
                except Exception:
                    pass
                log_action(
                    "risk_block", "market_closed_queued",
                    symbol=signal.symbol,
                    details=f"Queued for market open: {reason}",
                )
                console.print(f"  [yellow]QUEUED {signal.symbol}: {reason}[/yellow]")
                continue

            # Size the order (volatility-adjusted + strategy budgets)
            order_value = calculate_order_size(
                signal, portfolio_value, buy_count
            )
            if order_value <= 0:
                console.print(f"  [dim]Skip {signal.symbol} -- too small or budget exhausted[/dim]")
                continue

            # Extra safety: don't exceed actual buying power
            buying_power = account.get("buying_power", float("inf"))
            if order_value > buying_power:
                order_value = max(buying_power - 10, 0)  # Leave $10 buffer
                if order_value <= 5:
                    console.print(f"  [yellow]Skip {signal.symbol} -- insufficient buying power[/yellow]")
                    continue

            # Risk check (correlation limits, crypto cap, etc.)
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
                log.error("Order failed for %s: %s", signal.symbol, exec_err)
                console.print(f"  [red]ORDER FAILED {signal.symbol}: {exec_err}[/red]")
                continue

            if order.get("status") in ("filled", "accepted", "new", "pending_new"):
                # Poll for fill data if not immediately available
                filled_qty = order.get("filled_qty")
                filled_price = order.get("filled_avg_price")
                if (not filled_qty or not filled_price) and order.get("id"):
                    from trading.execution.router import get_order_status
                    for _poll in range(3):
                        time.sleep(1)
                        try:
                            fill_info = get_order_status(order["id"])
                            if fill_info.get("filled_qty"):
                                filled_qty = fill_info["filled_qty"]
                                filled_price = fill_info.get("filled_avg_price")
                                order["status"] = fill_info.get("status", order["status"])
                                break
                        except Exception:
                            pass

                qty_f = float(filled_qty) if filled_qty else 0
                price_f = float(filled_price) if filled_price else 0

                # Find the context from the contributing strategy
                contributing = (signal.data or {}).get("contributing_strategies", [])
                context = contexts.get(contributing[0], {}) if contributing else {}

                trade_id = insert_trade(
                    symbol=signal.symbol,
                    side=signal.action,
                    qty=qty_f,
                    price=price_f,
                    total=price_f * qty_f if (price_f and qty_f) else order_value,
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
                        "qty": qty_f,
                        "price": price_f,
                    },
                )
                side_style = "green" if signal.action == "buy" else "red"
                console.print(
                    f"  [{side_style}]{signal.action.upper()} "
                    f"${order_value:.2f} {signal.symbol} -- {order['status']}[/]"
                )
                log.info("Executed %s $%.2f %s -> %s (qty=%.6f, price=%.2f)",
                         signal.action, order_value, signal.symbol, order["status"],
                         qty_f, price_f)
                # Notify
                _notify_safe(
                    notify_trade,
                    signal.symbol, signal.action, order_value,
                    price_f,
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
            log.warning("Post-trade sync warning: %s", e)
            console.print(f"[yellow]Post-trade sync warning: {e}[/yellow]")

        # ---------------------------------------------------------------
        # Phase 6: Record daily P&L snapshot (proper daily return calc)
        # ---------------------------------------------------------------
        try:
            positions = _get_positions()
            pos_value = sum(
                p.get("market_value", p["qty"] * p["current_price"])
                for p in positions
            ) if positions else 0
            cash = account["cash"]
            pv = cash + pos_value

            # Proper daily return: compare to yesterday's portfolio value
            prev_records = get_daily_pnl(limit=2)
            if prev_records:
                prev_pv = prev_records[0]["portfolio_value"]
                # Check if prev record is from today (already written) or yesterday
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if prev_records[0]["date"] == today_str and len(prev_records) > 1:
                    prev_pv = prev_records[1]["portfolio_value"]
                daily_ret = (pv - prev_pv) / prev_pv if prev_pv > 0 else 0
            else:
                daily_ret = 0

            # Cumulative return from initial capital
            initial = float(os.environ.get("INITIAL_CAPITAL", 100_000))
            cum_ret = (pv - initial) / initial if initial > 0 else 0
            record_daily_pnl(pv, cash, pos_value, daily_ret, cum_ret)
        except Exception as e:
            log.warning("P&L snapshot warning: %s", e)
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
        log.info(
            "Cycle complete: %d raw -> %d consolidated, %d executed, %d blocked",
            signal_count, len(consolidated), executed_count, blocked_count,
        )
        console.print(
            f"\n[bold]Cycle complete -- {signal_count} raw signals -> "
            f"{len(consolidated)} consolidated, "
            f"{executed_count} trades, {blocked_count} blocked[/bold]"
        )

        # Send cycle summary notification
        _notify_safe(notify_cycle_summary, signal_count, executed_count, blocked_count)

        # ---------------------------------------------------------------
        # Phase 7: Autonomous improvement cycle
        # ---------------------------------------------------------------
        # Agents analyze performance, risk, regime, and research gaps.
        # Safe actions are auto-applied; dangerous ones stored for review.
        try:
            from trading.intelligence.autonomous import run_autonomous_cycle
            auto_result = run_autonomous_cycle()
            auto_applied = auto_result.get("auto_applied", 0)
            auto_review = auto_result.get("needs_review", 0)
            auto_total = auto_result.get("total_recommendations", 0)
            if auto_total > 0:
                console.print(
                    f"\n[bold magenta]Autonomous: {auto_total} recommendations — "
                    f"{auto_applied} auto-applied, {auto_review} need review[/bold magenta]"
                )
                for action in auto_result.get("applied_actions", []):
                    console.print(
                        f"  [magenta]→ {action['action']}: {action['target']} — {action['result']}[/magenta]"
                    )
            else:
                console.print("[dim]Autonomous: no recommendations this cycle[/dim]")
        except Exception as e:
            log.warning("Autonomous cycle failed (non-fatal): %s", e)
            console.print(f"[yellow]Autonomous cycle skipped: {e}[/yellow]")

    except Exception as e:
        log_action("error", "cycle_crash", details=str(e))
        log.exception("Cycle crashed: %s", e)
        console.print(f"[bold red]Cycle crashed: {e}[/bold red]")
        traceback.print_exc()
        try:
            from trading.monitor.notifications import notify_error
            _notify_safe(notify_error, str(e), "cycle_crash")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Stop-loss + take-profit checker (runs every 15 minutes)
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
            console.print(f"[bold yellow]{action['action'].upper()}: {symbol} -- {reason}[/]")
            log_action(
                "profit_mgmt", action["action"],
                symbol=symbol,
                details=reason,
                data={"pnl_pct": action["pnl_pct"], "qty": action["qty"]},
            )
            try:
                order = _execute_order(symbol, "sell", qty=action["qty"])
            except Exception as e:
                log.error("Profit management sell failed for %s: %s", symbol, e)
                continue

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
                tracker.remove(symbol)
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
                try:
                    order = _execute_order(symbol, "sell", qty=pos["qty"])
                except Exception as e:
                    log.error("Stop-loss sell failed for %s: %s", symbol, e)
                    continue

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
        log.error("Stop-loss check error: %s", e)
        console.print(f"[red]Stop-loss check error: {e}[/red]")


# ---------------------------------------------------------------------------
# Weekly review + adaptation analysis
# ---------------------------------------------------------------------------

def run_weekly_review():
    """Generate a weekly performance review and suggest adaptations."""
    try:
        from trading.learning.reviewer import generate_review
        generate_review("weekly")
        log_action("review", "weekly_review_generated")
        log.info("Weekly review generated")
        console.print("[green]Weekly review generated.[/green]")
    except Exception as e:
        log_action("error", "review_error", details=str(e))
        log.error("Review error: %s", e)

    # Also run adaptation analysis
    try:
        from trading.learning.adaptor import analyze_and_suggest
        suggestions = analyze_and_suggest()
        if suggestions:
            log_action("system", "adaptations_suggested",
                       details=f"{len(suggestions)} parameter changes suggested")
            log.info("Suggested %d parameter adaptations", len(suggestions))
            console.print(f"[magenta]{len(suggestions)} adaptation suggestions generated[/]")
    except Exception as e:
        log.warning("Adaptation analysis failed: %s", e)

    # Run strategy research cycle — discover gaps and update catalog
    try:
        from trading.intelligence.strategy_researcher import run_research_cycle
        research = run_research_cycle()
        log_action("research", "strategy_research",
                   details=research.summary(),
                   data=research.to_dict())
        log.info("Strategy research: %s", research.summary())
        console.print(f"[cyan]{research.summary()}[/cyan]")
    except Exception as e:
        log.warning("Strategy research cycle failed: %s", e)


# ---------------------------------------------------------------------------
# Daemon entry point
# ---------------------------------------------------------------------------

def start_daemon(interval_hours=4, paper=False):
    """Start the autonomous trading daemon."""
    if paper:
        import trading.config as cfg
        cfg.TRADING_MODE = "paper"

    # Initialize structured logging
    from trading.logging_config import setup_logging
    setup_logging(level="INFO")

    init_db()

    # Initialize persistent profit tracker
    _get_profit_tracker()

    mode = "PAPER" if TRADING_MODE == "paper" or paper else "LIVE"
    console.print(f"\n[bold green]{'='*60}[/]")
    console.print(f"[bold green]  OT-CORP. AUTONOMOUS TRADER v5.0 -- {mode} MODE[/bold green]")
    console.print(f"[bold green]  Self-Evolving Trading System[/bold green]")
    console.print(f"[bold green]  Execution: AsterDex Perpetual Futures[/bold green]")
    console.print(f"[bold green]{'='*60}[/]")
    console.print(f"  Trading cycle:       every {interval_hours} hours")
    console.print(f"  Stop/profit check:   every 15 minutes")
    console.print(f"  Signal aggregation:  enabled (dedup + conflict resolution)")
    console.print(f"  Dynamic allocation:  confluence + regime + performance tilt")
    console.print(f"  Autonomous agents:   5 agents (perf, risk, regime, research, learning)")
    console.print(f"  Agent conversation:  continuous self-improvement loop")
    console.print(f"  Auto-disable:        strategies with <25% win rate")
    console.print(f"  Auto-rebalance:      shift capital to winners")
    console.print(f"  Risk auto-tighten:   at 10% drawdown, halt at 18%")
    console.print(f"  Regime adaptation:   defensive posture in bearish markets")
    console.print(f"  Strategy research:   weekly gap analysis + priority queue")
    console.print(f"  Knowledge base:      continuous learning from outcomes")
    console.print(f"  Dashboard:           http://localhost:5050")
    console.print(f"[bold green]{'='*60}[/]\n")

    log.info("Daemon v3.0 started in %s mode, interval=%dh", mode, interval_hours)
    log_action("scheduler", "daemon_started", details=f"Mode: {mode}, Interval: {interval_hours}h, Version: v3.0")

    # Notify on start
    try:
        from trading.monitor.notifications import notify
        _notify_safe(notify, "Daemon Started", f"Trading daemon v3.0 started in {mode} mode", "info")
    except Exception:
        pass

    # Note: AsterDex orders are immediate (market orders) — no orphaned order reconciliation needed
    log.info("AsterDex primary venue — skipping Alpaca order reconciliation")

    # Run immediately on start
    run_trading_cycle()

    # Schedule recurring tasks
    schedule.every(interval_hours).hours.do(run_trading_cycle)
    schedule.every(15).minutes.do(check_stop_losses)
    schedule.every().sunday.at("00:00").do(run_weekly_review)

    # Graceful shutdown via SIGTERM/SIGINT
    _shutdown_requested = False

    def _handle_shutdown(signum, frame):
        nonlocal _shutdown_requested
        _shutdown_requested = True
        sig_name = signal_mod.Signals(signum).name
        log.info("Received %s — completing current cycle before shutdown", sig_name)
        console.print(f"\n[yellow]Received {sig_name} — shutting down gracefully...[/yellow]")

    signal_mod.signal(signal_mod.SIGTERM, _handle_shutdown)
    signal_mod.signal(signal_mod.SIGINT, _handle_shutdown)

    consecutive_errors = 0
    max_consecutive_errors = 10
    backoff_minutes = 5

    try:
        while not _shutdown_requested:
            try:
                schedule.run_pending()
                consecutive_errors = 0  # Reset on success
            except KeyboardInterrupt:
                break
            except Exception as exc:
                consecutive_errors += 1
                log.error("Daemon loop error (%d/%d): %s", consecutive_errors, max_consecutive_errors, exc)
                log_action("error", "daemon_loop", details=f"Error {consecutive_errors}/{max_consecutive_errors}: {exc}")

                if consecutive_errors >= max_consecutive_errors:
                    pause_msg = f"Too many consecutive errors ({consecutive_errors}), pausing {backoff_minutes}m"
                    log.warning(pause_msg)
                    console.print(f"[red]{pause_msg}[/red]")
                    log_action("scheduler", "daemon_backoff", details=pause_msg)
                    try:
                        from trading.monitor.notifications import notify
                        _notify_safe(notify, "Daemon Backoff", pause_msg, "warning")
                    except Exception:
                        pass
                    time.sleep(backoff_minutes * 60)
                    consecutive_errors = 0  # Reset after backoff

            time.sleep(30)
    except KeyboardInterrupt:
        pass

    # Clean shutdown
    log_action("scheduler", "daemon_stopped", details="Graceful shutdown")
    log.info("Daemon stopped gracefully")
    console.print("\n[yellow]Daemon stopped.[/yellow]")
    try:
        from trading.monitor.notifications import notify
        _notify_safe(notify, "Daemon Stopped", "Trading daemon stopped gracefully", "warning")
    except Exception:
        pass
