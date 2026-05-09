"""Main entry point — CLI for the trading system."""

import sys
from rich.console import Console

from trading.db.store import init_db, insert_trade, update_trade_status, insert_signal

console = Console()


def get_account():
    """Get account from Bybit (primary execution venue)."""
    from trading.execution.router import get_account as _get_account
    return _get_account()


def get_positions_list():
    """Get positions from Bybit."""
    from trading.execution.router import get_positions_from_bybit
    return get_positions_from_bybit()


def execute_order(symbol, side, notional=None, qty=None, stop_loss_price=None):
    """Execute order via Bybit perpetual futures."""
    from trading.execution.router import submit_order
    return submit_order(symbol, side, notional=notional, qty=qty,
                        stop_loss_price=stop_loss_price)


def cmd_run(paper: bool = False):
    """Run all strategies and execute trades."""
    from trading.strategy.registry import get_enabled_strategies
    from trading.risk.manager import RiskManager, compute_trade_targets
    from trading.risk.portfolio import calculate_order_size
    from trading.learning.journal import create_journal_entry
    from trading.monitor.dashboard import show_signals, show_portfolio, show_positions
    from trading.data.cache import clear_cache

    if paper:
        import trading.config as cfg
        cfg.TRADING_MODE = "paper"

    clear_cache()
    account = get_account()
    portfolio_value = account["portfolio_value"]
    mode = "PAPER" if account.get("paper") else "LIVE"

    console.print(f"\n[bold]Running strategies in {mode} mode[/bold]")
    console.print(f"Portfolio: ${portfolio_value:.2f}\n")

    # Initialize strategies via registry
    strategies = get_enabled_strategies()

    risk_mgr = RiskManager(portfolio_value)
    all_signals = []
    executed = []

    # Pre-fetch positions so we can skip sells for symbols we don't hold
    positions = get_positions_list()
    held_symbols = {p["symbol"] for p in positions}

    for strategy in strategies:
        console.print(f"[cyan]Running {strategy.name}...[/cyan]")
        try:
            signals = strategy.generate_signals()
            context = strategy.get_market_context()

            for signal in signals:
                all_signals.append({
                    "strategy": signal.strategy,
                    "symbol": signal.symbol,
                    "action": signal.action,
                    "strength": signal.strength,
                    "reason": signal.reason,
                })

                # Log signal
                insert_signal(signal.strategy, signal.symbol, signal.action, signal.strength, signal.data)

                if not signal.is_actionable:
                    continue

                # Skip sells for symbols we don't hold (unless short-selling is allowed)
                if signal.action == "sell":
                    # Normalize: Alpaca positions use e.g. "BTCUSD" not "BTC/USD"
                    norm = signal.symbol.replace("/", "")
                    if norm not in held_symbols and signal.symbol not in held_symbols:
                        from trading.config import ALLOW_SHORT_SELLING, SHORT_ALLOWED_STRATEGIES
                        # For aggregator signals, check contributing strategies
                        strat_name = signal.strategy
                        if strat_name == "aggregator":
                            contributing = (signal.data or {}).get("contributing_strategies", [])
                            short_allowed = any(s in SHORT_ALLOWED_STRATEGIES for s in contributing)
                        else:
                            short_allowed = strat_name in SHORT_ALLOWED_STRATEGIES
                        if not ALLOW_SHORT_SELLING or not short_allowed:
                            # Skip sell for non-short-allowed strategies
                            console.print(f"  [dim]Skipping SELL {signal.symbol} — no position held[/dim]")
                            continue
                        # Allow short for permitted strategies

                # Calculate order size
                buy_signals_count = sum(1 for s in signals if s.action == "buy")
                order_value = calculate_order_size(signal, portfolio_value, buy_signals_count)
                if order_value <= 0:
                    console.print(f"  [dim]Skipping {signal.symbol} — order too small[/dim]")
                    continue

                # Risk check
                risk_check = risk_mgr.check_trade(signal, order_value)
                if not risk_check.allowed:
                    console.print(f"  [red]BLOCKED: {risk_check.reason}[/red]")
                    continue

                # Compute SL/TP targets before executing
                entry_price = signal.data.get("price") if signal.data else None
                if not entry_price:
                    # Try to get a real quote — never use order_value as a price
                    if "/" in signal.symbol:
                        try:
                            from trading.execution.router import get_crypto_quote
                            q = get_crypto_quote(signal.symbol)
                            entry_price = q.get("mid") or None
                        except Exception:
                            pass

                targets = compute_trade_targets(
                    symbol=signal.symbol,
                    entry_price=entry_price,
                    order_value=order_value,
                    signal_strength=signal.strength,
                )

                # Execute
                console.print(f"  [{'green' if signal.action == 'buy' else 'red'}]"
                              f"{signal.action.upper()} ${order_value:.2f} of {signal.symbol}[/]")
                console.print(f"    [dim]SL: ${targets.stop_loss_price:,.2f} (-{targets.max_loss_pct:.1f}%) | "
                              f"TP: ${targets.take_profit_price:,.2f} (+{targets.max_gain_pct:.1f}%) | "
                              f"R:R {targets.risk_reward_ratio:.1f}:1[/dim]")

                order = execute_order(signal.symbol, signal.action, notional=order_value,
                                     stop_loss_price=targets.stop_loss_price)

                if order.get("status") in ("filled", "accepted", "new", "pending_new"):
                    filled_price = float(order.get("filled_avg_price") or 0)
                    # Recompute targets with actual fill price if available
                    if filled_price > 0 and filled_price != entry_price:
                        targets = compute_trade_targets(
                            symbol=signal.symbol,
                            entry_price=filled_price,
                            order_value=order_value,
                            signal_strength=signal.strength,
                        )

                    trade_id = insert_trade(
                        symbol=signal.symbol,
                        side=signal.action,
                        qty=float(order.get("filled_qty") or order.get("qty") or 0),
                        price=filled_price,
                        total=order_value,
                        strategy=signal.strategy,
                        status=order["status"],
                        alpaca_order_id=order.get("id"),
                        stop_loss_price=targets.stop_loss_price,
                        take_profit_price=targets.take_profit_price,
                        trailing_stop_activate=targets.trailing_stop_activate_price,
                        risk_reward_ratio=targets.risk_reward_ratio,
                    )
                    create_journal_entry(trade_id, signal, context)
                    executed.append(order)
                    console.print(f"    [green]Order {order['status']}: {order['id']}[/green]")
                else:
                    console.print(f"    [red]Order rejected: {order.get('reason', order.get('status'))}[/red]")

        except Exception as e:
            console.print(f"  [red]Error in {strategy.name}: {e}[/red]")

    console.print(f"\n[bold]Executed {len(executed)} trades[/bold]\n")
    show_signals(all_signals)
    console.print()
    show_positions(get_positions_list())


def cmd_status():
    """Show portfolio status."""
    from trading.monitor.dashboard import show_portfolio, show_positions, show_daily_pnl

    account = get_account()
    show_portfolio(account)
    console.print()
    show_positions(get_positions_list())
    console.print()
    show_daily_pnl()


def cmd_strategies():
    """List all registered strategies and their enabled status."""
    from trading.strategy.registry import list_registered
    from trading.config import STRATEGY_ENABLED
    console.print("[bold]Registered strategies:[/bold]\n")
    for name in list_registered():
        enabled = STRATEGY_ENABLED.get(name, False)
        status = "[green]ENABLED[/green]" if enabled else "[red]DISABLED[/red]"
        console.print(f"  {name:25s} {status}")
    console.print(f"\n  Total: {len(list_registered())} strategies")


def cmd_signals():
    """Show current signals without trading."""
    from trading.strategy.registry import get_enabled_strategies
    from trading.monitor.dashboard import show_signals
    from trading.data.cache import clear_cache

    clear_cache()
    strategies = get_enabled_strategies()
    all_signals = []

    for strategy in strategies:
        console.print(f"[cyan]Analyzing {strategy.name}...[/cyan]")
        try:
            signals = strategy.generate_signals()
            for s in signals:
                all_signals.append({
                    "strategy": s.strategy,
                    "symbol": s.symbol,
                    "action": s.action,
                    "strength": s.strength,
                    "reason": s.reason,
                })
        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")

    console.print()
    show_signals(all_signals)


def cmd_history(limit: int = 20):
    """Show trade history."""
    from trading.monitor.dashboard import show_trades
    show_trades(limit=limit)


def cmd_journal(limit: int = 20):
    """Show trade journal."""
    from trading.learning.journal import export_journal_markdown
    content = export_journal_markdown(limit=limit)
    console.print(content)


def cmd_review(monthly: bool = False):
    """Generate performance review."""
    from trading.learning.reviewer import generate_review
    period = "monthly" if monthly else "weekly"
    content = generate_review(period)
    console.print(content)


def cmd_learn(filepath: str):
    """Ingest a trading document into the knowledge base."""
    from trading.learning.knowledge import ingest_file
    result = ingest_file(filepath)
    console.print(f"[green]Ingested: {result['title']}[/green]")
    console.print(f"  Category: {result['category']}")
    console.print(f"  Rules extracted: {result['rules_extracted']}")


def cmd_adapt():
    """Show suggested parameter adaptations."""
    from trading.learning.adaptor import analyze_and_suggest, get_pending
    suggestions = analyze_and_suggest()

    if not suggestions:
        console.print("[dim]No parameter changes suggested (need more trades).[/dim]")
        pending = get_pending()
        if pending:
            console.print(f"\n[yellow]{len(pending)} pending unapproved changes:[/yellow]")
            for p in pending:
                console.print(f"  {p['strategy']}.{p['param_name']}: {p['old_value']} → {p['new_value']}")
                console.print(f"    Reason: {p['reason']}")
        return

    console.print("[bold]Suggested parameter changes:[/bold]\n")
    for s in suggestions:
        console.print(f"[yellow]{s['strategy']}.{s['param']}[/yellow]")
        console.print(f"  Current: {s['current']} → Suggested: {s['suggested']}")
        console.print(f"  Reason: {s['reason']}")
        console.print()


def cmd_daemon(paper: bool = False, interval: int = 4):
    """Start the autonomous trading daemon."""
    from trading.config import validate_config, ConfigError

    if paper:
        import trading.config as cfg
        cfg.TRADING_MODE = "paper"

    # Validate config and API connectivity before starting the daemon
    console.print("[bold]Pre-flight checks...[/bold]")
    try:
        warnings = validate_config(test_api=True)
        for w in warnings:
            console.print(f"  [yellow]⚠ {w}[/yellow]")
        console.print("  [green]✓ Config valid, API connected[/green]")
    except ConfigError as e:
        console.print(f"\n[bold red]STARTUP ABORTED: {e}[/bold red]")
        return

    # Validate leverage profile
    from trading.config import validate_leverage_profile
    lev_warnings = validate_leverage_profile()
    for w in lev_warnings:
        console.print(f"  [yellow]⚠ {w}[/yellow]")
    if lev_warnings:
        console.print(f"  [yellow]⚠ {len(lev_warnings)} leverage warning(s) — review LEVERAGE_PROFILE setting[/yellow]")
    else:
        console.print("  [green]✓ Leverage profile validated[/green]")

    from trading.scheduler import start_daemon
    start_daemon(interval_hours=interval, paper=paper)


def cmd_dashboard(port: int = 5050):
    """Start the web dashboard."""
    import os
    from trading.monitor.web import start_dashboard
    # Respect PORT env from launch.json / preview
    actual_port = int(os.environ.get("PORT", port))
    console.print(f"[bold green]Starting dashboard at http://localhost:{actual_port}[/bold green]")
    start_dashboard(port=actual_port)


def cmd_backtest():
    """Run backtest on historical data."""
    from trading.data.crypto import get_historical_prices
    from trading.data.sentiment import get_fear_greed

    console.print("[bold]Running backtest on 90 days of historical data...[/bold]\n")

    # Simple momentum backtest
    console.print("[cyan]Backtesting Momentum Strategy[/cyan]")
    try:
        from trading.config import DEFAULT_COINS, CRYPTO_SYMBOLS
        coins = DEFAULT_COINS[:5]  # Top 5 for speed
        results = {}
        for coin in coins:
            hist = get_historical_prices(coin, days=90)
            if hist.empty:
                continue
            # Simple: buy at day 0, sell at day 90
            entry = hist["price"].iloc[0]
            exit_price = hist["price"].iloc[-1]
            ret = (exit_price - entry) / entry * 100
            results[coin] = {"entry": entry, "exit": exit_price, "return_pct": ret}
            symbol = CRYPTO_SYMBOLS.get(coin, coin)
            style = "green" if ret > 0 else "red"
            console.print(f"  {symbol}: ${entry:.2f} → ${exit_price:.2f} [{style}]{ret:+.1f}%[/]")

        if results:
            avg_return = sum(r["return_pct"] for r in results.values()) / len(results)
            console.print(f"\n  Average return: {avg_return:+.1f}%")
    except Exception as e:
        console.print(f"  [red]Error: {e}[/red]")

    # Fear & Greed backtest
    console.print("\n[cyan]Backtesting Mean Reversion (Fear & Greed)[/cyan]")
    try:
        fg = get_fear_greed(limit=30)
        history = fg["history"]
        buy_days = history[history["value"] <= 25]
        sell_days = history[history["value"] >= 75]
        console.print(f"  Fear & Greed buy signals (≤25) in last 30 days: {len(buy_days)}")
        console.print(f"  Fear & Greed sell signals (≥75) in last 30 days: {len(sell_days)}")
        console.print(f"  Current: {fg['current']['value']} ({fg['current']['classification']})")
    except Exception as e:
        console.print(f"  [red]Error: {e}[/red]")

    console.print("\n[bold]Backtest complete.[/bold]")


def main():
    init_db()

    if len(sys.argv) < 2:
        console.print("[bold]Usage:[/bold]")
        console.print("  [bold cyan]Autonomous:[/bold cyan]")
        console.print("  python -m trading.main daemon [--paper] [--interval 4]  Start autonomous trader")
        console.print("  python -m trading.main dashboard [--port 5000]          Start web dashboard")
        console.print()
        console.print("  [bold cyan]Manual:[/bold cyan]")
        console.print("  python -m trading.main run [--paper]    Run strategies once and trade")
        console.print("  python -m trading.main status           Portfolio status")
        console.print("  python -m trading.main signals          Show signals (no trading)")
        console.print("  python -m trading.main history          Trade history")
        console.print("  python -m trading.main backtest         Backtest on historical data")
        console.print("  python -m trading.main journal          Show trade journal")
        console.print("  python -m trading.main review [--monthly]  Performance review")
        console.print("  python -m trading.main learn <file>     Ingest trading material")
        console.print("  python -m trading.main adapt            Show parameter suggestions")
        console.print("  python -m trading.main strategies       List all strategies")
        return

    command = sys.argv[1]

    if command == "strategies":
        cmd_strategies()
    elif command == "daemon":
        paper = "--paper" in sys.argv
        interval = 4
        if "--interval" in sys.argv:
            idx = sys.argv.index("--interval")
            if idx + 1 < len(sys.argv):
                interval = int(sys.argv[idx + 1])
        cmd_daemon(paper=paper, interval=interval)
    elif command == "dashboard":
        port = 5050
        if "--port" in sys.argv:
            idx = sys.argv.index("--port")
            if idx + 1 < len(sys.argv):
                port = int(sys.argv[idx + 1])
        cmd_dashboard(port=port)
    elif command == "run":
        paper = "--paper" in sys.argv
        cmd_run(paper=paper)
    elif command == "status":
        cmd_status()
    elif command == "signals":
        cmd_signals()
    elif command == "history":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        cmd_history(limit)
    elif command == "backtest":
        cmd_backtest()
    elif command == "journal":
        cmd_journal()
    elif command == "review":
        monthly = "--monthly" in sys.argv
        cmd_review(monthly)
    elif command == "learn":
        if len(sys.argv) < 3:
            console.print("[red]Usage: python -m trading.main learn <filepath>[/red]")
            return
        cmd_learn(sys.argv[2])
    elif command == "adapt":
        cmd_adapt()
    else:
        console.print(f"[red]Unknown command: {command}[/red]")


if __name__ == "__main__":
    main()
