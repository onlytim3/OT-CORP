"""Position synchronization and fill verification.

Bridges the gap between Alpaca broker state and local DB state:
  - sync_positions(): pulls Alpaca positions into the positions table
  - verify_fills(): checks pending orders for fill confirmations
  - pair_trades(): matches buy/sell trades to calculate realised P&L
  - run_sync(): convenience runner for all three in order
"""

from __future__ import annotations

import logging
import traceback

from rich.console import Console

log = logging.getLogger(__name__)

from trading.db.store import (
    close_trade,
    get_db,
    get_open_trades,
    get_positions,
    log_action,
    remove_position,
    update_trade_status,
    upsert_position,
)
from trading.execution.router import get_order_status, get_positions_from_aster as get_positions_from_alpaca
from trading.learning.journal import record_outcome

console = Console()


class SyncError(Exception):
    """Raised when position sync fails in a way that makes trading unsafe."""
    pass


# ---------------------------------------------------------------------------
# 1. sync_positions — Alpaca (or paper) positions  -->  local DB
# ---------------------------------------------------------------------------

def sync_positions() -> int:
    """Pull positions from the broker and upsert them into the local DB.

    For each position returned by the broker:
      - Determine which strategy opened it by looking at open trades for
        that symbol.
      - Call upsert_position so the positions table reflects reality.

    Positions that exist locally but are no longer held at the broker are
    removed (the position was closed externally or via a stop-loss on the
    broker side).

    Returns the number of positions synced.
    """
    try:
        broker_positions = get_positions_from_alpaca()
    except Exception as exc:
        console.print(f"[red]sync_positions: failed to fetch broker positions: {exc}[/red]")
        log_action("error", "sync_positions_fetch", details=str(exc))
        raise SyncError(f"Cannot fetch broker positions: {exc}") from exc

    # Build a set of symbols currently held at the broker.
    broker_symbols: set[str] = set()

    # Map symbol -> strategy from open trades so we can tag positions.
    # Handle format mismatches: trades may have "BTC/USD", broker returns "BTCUSD"
    open_trades = get_open_trades()
    strategy_by_symbol: dict[str, str] = {}
    for trade in open_trades:
        sym = trade.get("symbol", "")
        strat = trade.get("strategy", "")
        if sym and strat:
            sym_flat = sym.replace("/", "")
            if sym not in strategy_by_symbol:
                strategy_by_symbol[sym] = strat
            if sym_flat not in strategy_by_symbol:
                strategy_by_symbol[sym_flat] = strat

    synced = 0
    for pos in broker_positions:
        symbol = pos["symbol"]
        broker_symbols.add(symbol)
        strategy = (strategy_by_symbol.get(symbol)
                     or strategy_by_symbol.get(symbol.replace("/", ""))
                     or "unknown")

        try:
            upsert_position(
                symbol=symbol,
                qty=pos["qty"],
                avg_cost=pos["avg_cost"],
                current_price=pos["current_price"],
                strategy=strategy,
            )
            synced += 1
        except Exception as exc:
            console.print(f"[red]sync_positions: failed to upsert {symbol}: {exc}[/red]")
            log_action("error", "sync_position_upsert", symbol=symbol, details=str(exc))

    # Remove local positions that the broker no longer holds.
    try:
        local_positions = get_positions()
        for local_pos in local_positions:
            if local_pos["symbol"] not in broker_symbols:
                remove_position(local_pos["symbol"])
                console.print(
                    f"[yellow]sync_positions: removed stale position {local_pos['symbol']}[/yellow]"
                )
                log_action(
                    "system",
                    "position_removed",
                    symbol=local_pos["symbol"],
                    details="Position no longer held at broker",
                )
    except Exception as exc:
        console.print(f"[red]sync_positions: failed to prune stale positions: {exc}[/red]")
        log_action("error", "sync_positions_prune", details=str(exc))

    console.print(f"[green]sync_positions: synced {synced} position(s)[/green]")
    log_action("system", "sync_positions", details=f"Synced {synced} positions")
    return synced


# ---------------------------------------------------------------------------
# 2. verify_fills — check pending orders for fill status
# ---------------------------------------------------------------------------

def verify_fills() -> int:
    """Poll the broker for order status on trades that are not yet terminal.

    Terminal statuses (no polling needed): filled, closed, rejected, canceled.
    Everything else (pending, new, accepted, partially_filled, etc.) gets
    checked against the broker.

    In paper mode orders are always instantly filled, so we simply mark any
    lingering non-terminal trades as filled.

    Returns the number of fills verified / updated.
    """
    terminal_statuses = {"filled", "closed", "rejected", "canceled"}

    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status NOT IN ('filled', 'closed', 'rejected', 'canceled') "
                "AND closed_at IS NULL"
            ).fetchall()
            pending_trades = [dict(r) for r in rows]
    except Exception as exc:
        console.print(f"[red]verify_fills: failed to query pending trades: {exc}[/red]")
        log_action("error", "verify_fills_query", details=str(exc))
        return 0

    if not pending_trades:
        console.print("[dim]verify_fills: no pending trades to check[/dim]")
        return 0

    verified = 0
    for trade in pending_trades:
        trade_id = trade["id"]
        alpaca_order_id = trade.get("alpaca_order_id")
        symbol = trade.get("symbol", "?")

        try:
            # Need an order ID to poll Alpaca for fill status.
            if not alpaca_order_id:
                console.print(
                    f"[yellow]verify_fills: trade {trade_id} ({symbol}) has no alpaca_order_id, skipping[/yellow]"
                )
                continue

            order_info = get_order_status(alpaca_order_id, symbol=symbol)
            broker_status = order_info.get("status", "").lower()

            if broker_status == "filled":
                filled_price = order_info.get("filled_avg_price")
                filled_qty = order_info.get("filled_qty")

                # Update trade status to filled.
                update_trade_status(trade_id, "filled")

                # Update trade record with actual fill data (price, qty, total).
                if filled_price is not None:
                    try:
                        price_f = float(filled_price)
                        qty_f = float(filled_qty) if filled_qty else trade.get("qty", 0)
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE trades SET qty=?, price=?, total=? WHERE id=?",
                                (qty_f, price_f, price_f * qty_f, trade_id),
                            )
                    except (ValueError, TypeError):
                        pass  # Non-numeric value — leave trade as-is.

                console.print(
                    f"[green]verify_fills: trade {trade_id} ({symbol}) filled @ {filled_price}[/green]"
                )
                log_action(
                    "trade",
                    "fill_verified",
                    symbol=symbol,
                    details=f"Trade {trade_id} filled @ {filled_price}, qty {filled_qty}",
                )
                verified += 1

            elif broker_status in ("rejected", "canceled", "expired"):
                update_trade_status(trade_id, broker_status)
                console.print(
                    f"[yellow]verify_fills: trade {trade_id} ({symbol}) status -> {broker_status}[/yellow]"
                )
                log_action(
                    "trade",
                    "order_terminal",
                    symbol=symbol,
                    details=f"Trade {trade_id} broker status: {broker_status}",
                )
                verified += 1

            else:
                # Still in-flight (e.g. partially_filled, accepted, new).
                console.print(
                    f"[dim]verify_fills: trade {trade_id} ({symbol}) still {broker_status}[/dim]"
                )

        except Exception as exc:
            console.print(
                f"[red]verify_fills: error checking trade {trade_id} ({symbol}): {exc}[/red]"
            )
            log_action(
                "error",
                "verify_fills_check",
                symbol=symbol,
                details=f"Trade {trade_id}: {exc}",
            )

    console.print(f"[green]verify_fills: {verified} fill(s) verified[/green]")
    log_action("system", "verify_fills", details=f"Verified {verified} fills")
    return verified


# ---------------------------------------------------------------------------
# 3. pair_trades — match buys with sells (FIFO) to realise P&L
# ---------------------------------------------------------------------------

def _reduce_trade_qty(trade_id: int, new_qty: float, new_total: float) -> None:
    """Reduce an open trade's qty and total without closing it.

    Used when a partial sell consumes only part of a buy position.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE trades SET qty=?, total=? WHERE id=?",
            (new_qty, new_total, trade_id),
        )


def _pair_one_direction(
    entries: list[dict],
    exits: list[dict],
    symbol: str,
    entry_side: str,
) -> int:
    """Pair entry trades with opposite-side exit trades using FIFO order.

    *entry_side* is ``"buy"`` for longs or ``"sell"`` for shorts.
    P&L is direction-aware: longs profit when price rises, shorts when it drops.

    Returns the number of pairings made.
    """
    entry_idx = 0
    paired = 0

    for exit_trade in exits:
        exit_remaining: float = exit_trade.get("qty", 0)
        exit_total_pnl: float = 0.0

        while exit_remaining > 0 and entry_idx < len(entries):
            entry = entries[entry_idx]
            entry_id = entry["id"]
            exit_id = exit_trade["id"]

            entry_price = entry.get("price") or 0
            exit_price = exit_trade.get("price") or 0
            entry_qty: float = entry.get("qty", 0)

            if entry_qty <= 0 or entry_price <= 0 or exit_price <= 0:
                entry_idx += 1
                continue

            matched_qty = min(entry_qty, exit_remaining)
            if entry_side == "buy":
                pnl = (exit_price - entry_price) * matched_qty
            else:
                # Short: profit when price drops
                pnl = (entry_price - exit_price) * matched_qty
            exit_total_pnl += pnl

            try:
                if matched_qty >= entry_qty:
                    close_trade(entry_id, exit_price, pnl)
                    entry_idx += 1
                else:
                    remaining_qty = entry_qty - matched_qty
                    _reduce_trade_qty(
                        entry_id,
                        remaining_qty,
                        remaining_qty * entry_price,
                    )
                    entry["qty"] = remaining_qty
                    entry["total"] = remaining_qty * entry_price

                exit_remaining -= matched_qty

                exit_label = "sell" if entry_side == "buy" else "buy-to-cover"
                console.print(
                    f"[green]pair_trades: {symbol} {entry_side} #{entry_id} + {exit_label} #{exit_id} "
                    f"matched {matched_qty} units -> P&L ${pnl:+.2f}[/green]"
                )
                log_action(
                    "trade",
                    "trade_paired",
                    symbol=symbol,
                    details=(
                        f"{entry_side.capitalize()} #{entry_id} @ ${entry_price:.2f} paired with "
                        f"{exit_label} #{exit_id} @ ${exit_price:.2f}, "
                        f"matched_qty {matched_qty}, P&L ${pnl:+.2f}"
                    ),
                )

                try:
                    record_outcome(
                        trade_id=entry_id,
                        pnl=pnl,
                        exit_price=exit_price,
                        entry_price=entry_price,
                    )
                except Exception as exc:
                    console.print(
                        f"[yellow]pair_trades: journal record_outcome failed for trade "
                        f"{entry_id}: {exc}[/yellow]"
                    )

                try:
                    from trading.strategy.circuit_breaker import record_trade_result
                    strategy_name = entry.get("strategy", "")
                    if strategy_name:
                        record_trade_result(strategy_name, is_loss=(pnl < 0))
                except Exception as cb_err:
                    log.warning("Circuit breaker record failed for %s: %s", symbol, cb_err)

                try:
                    from trading.llm.engine import generate_post_trade_review
                    from trading.db.store import insert_trade_analysis
                    trade_dict = {
                        "id": entry_id, "symbol": symbol,
                        "side": entry_side, "price": entry_price,
                        "strategy": entry.get("strategy", "unknown"),
                        "entry_reasoning": entry.get("entry_reasoning", ""),
                    }
                    market_conds = {
                        "exit_price": exit_price,
                        "exit_trade_id": exit_id,
                        "matched_qty": matched_qty,
                    }
                    review = generate_post_trade_review(
                        trade_dict,
                        entry.get("entry_reasoning", ""),
                        pnl,
                        market_conds,
                    )
                    if review and "LLM unavailable" not in review:
                        insert_trade_analysis(
                            entry_id, review,
                            {"pnl": pnl, "exit_price": exit_price},
                            source="post_trade_review",
                        )
                except Exception as ptf:
                    log.debug("Post-trade review failed (non-fatal): %s", ptf)

                paired += 1

            except Exception as exc:
                exit_label = "sell" if entry_side == "buy" else "buy-to-cover"
                console.print(
                    f"[red]pair_trades: failed to pair {symbol} {entry_side} #{entry_id} "
                    f"with {exit_label} #{exit_id}: {exc}[/red]"
                )
                log_action(
                    "error",
                    "pair_trades_error",
                    symbol=symbol,
                    details=f"{entry_side.capitalize()} #{entry_id}, Exit #{exit_id}: {exc}",
                )
                entry_idx += 1
                break

        # Close the exit trade with accumulated P&L
        try:
            close_trade(exit_trade["id"], exit_trade.get("price", 0), exit_total_pnl)
        except Exception as exc:
            console.print(
                f"[red]pair_trades: failed to close exit #{exit_trade['id']}: {exc}[/red]"
            )

    return paired


def pair_trades() -> int:
    """Match open entry trades with opposite-side exit trades using FIFO order.

    Handles both directions for futures trading:
      - Long entries (buy) paired with sell exits
      - Short entries (sell) paired with buy-to-cover exits

    For each direction, an entry trade that precedes an opposite-side trade
    is considered an entry/exit pair. FIFO ordering is used.

    P&L is direction-aware:
      - Long:  (exit_price - entry_price) * qty
      - Short: (entry_price - exit_price) * qty

    Returns the number of pairings made.
    """
    open_trades = get_open_trades()
    if not open_trades:
        console.print("[dim]pair_trades: no open trades to pair[/dim]")
        return 0

    # Bucket by symbol, separating buys and sells.
    buys_by_symbol: dict[str, list[dict]] = {}
    sells_by_symbol: dict[str, list[dict]] = {}

    for trade in open_trades:
        symbol = trade.get("symbol")
        side = trade.get("side", "").lower()
        if not symbol:
            continue
        if side == "buy":
            buys_by_symbol.setdefault(symbol, []).append(trade)
        elif side in ("sell", "short"):
            sells_by_symbol.setdefault(symbol, []).append(trade)

    # Sort each bucket by timestamp ascending (oldest first = FIFO).
    for sym in buys_by_symbol:
        buys_by_symbol[sym].sort(key=lambda t: t.get("timestamp", ""))
    for sym in sells_by_symbol:
        sells_by_symbol[sym].sort(key=lambda t: t.get("timestamp", ""))

    paired = 0

    # Direction 1: Long entries (buys) closed by sell exits
    # For each symbol, sells that appear AFTER open buys are exits.
    for symbol, sells in sells_by_symbol.items():
        buys = buys_by_symbol.get(symbol, [])
        if not buys:
            continue
        # Only sells that come AFTER the oldest open buy are potential exits
        oldest_buy_ts = buys[0].get("timestamp", "")
        exit_sells = [s for s in sells if s.get("timestamp", "") > oldest_buy_ts]
        if exit_sells:
            paired += _pair_one_direction(buys, exit_sells, symbol, entry_side="buy")

    # Direction 2: Short entries (sells) closed by buy-to-cover exits
    # For each symbol, buys that appear AFTER open sells are exits (covers).
    for symbol, buys in buys_by_symbol.items():
        sells = sells_by_symbol.get(symbol, [])
        if not sells:
            continue
        oldest_sell_ts = sells[0].get("timestamp", "")
        exit_buys = [b for b in buys if b.get("timestamp", "") > oldest_sell_ts]
        if exit_buys:
            paired += _pair_one_direction(sells, exit_buys, symbol, entry_side="sell")

    console.print(f"[green]pair_trades: {paired} trade(s) paired[/green]")
    log_action("system", "pair_trades", details=f"Paired {paired} trades")
    return paired


# ---------------------------------------------------------------------------
# 4. run_sync — convenience runner
# ---------------------------------------------------------------------------

def run_sync() -> dict:
    """Run the full sync pipeline: positions -> fills -> pairing.

    Position sync failures raise SyncError — callers must decide whether
    to halt trading. Fill verification and trade pairing failures are
    non-fatal and logged.

    Returns a summary dict with counts from each step.
    """
    console.rule("[bold]Sync Pipeline[/bold]")
    results: dict[str, int | str] = {}

    # Step 1 — sync positions (CRITICAL — raises SyncError on failure)
    results["positions_synced"] = sync_positions()

    # Step 2 — verify fills (non-fatal)
    try:
        results["fills_verified"] = verify_fills()
    except Exception as exc:
        console.print(f"[red]run_sync: verify_fills crashed: {exc}[/red]")
        log_action("error", "run_sync_fills", details=traceback.format_exc())
        results["fills_verified"] = f"error: {exc}"

    # Step 3 — pair trades (non-fatal)
    try:
        results["trades_paired"] = pair_trades()
    except Exception as exc:
        console.print(f"[red]run_sync: pair_trades crashed: {exc}[/red]")
        log_action("error", "run_sync_pairing", details=traceback.format_exc())
        results["trades_paired"] = f"error: {exc}"

    console.rule("[bold]Sync Complete[/bold]")
    console.print(results)
    return results
