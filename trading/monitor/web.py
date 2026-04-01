"""Flask web dashboard — autonomous monitoring interface."""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, jsonify, request, send_from_directory, redirect

try:
    from flask_cors import CORS as _CORS
except ImportError:
    _CORS = None

from trading.config import TRADING_MODE, RISK, PROJECT_ROOT, DB_PATH, DISPLAY_TIMEZONE, STRATEGY_ENABLED
from trading.db.store import (
    init_db, get_db, get_read_db, get_trades, get_positions, get_daily_pnl,
    get_signals, get_action_log, get_action_log_summary,
    get_journal, get_reviews, get_setting, get_open_trades,
    symbol_variants,
)
from trading.execution.router import get_account, get_positions_from_aster as get_positions_from_alpaca

log = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
)

# Enable CORS for React dashboard dev server
if _CORS:
    _CORS(app, origins=["http://localhost:5173", "http://localhost:5174", "http://127.0.0.1:5173"])

_START_TIME = time.monotonic()

# ---------------------------------------------------------------------------
# React Dashboard (served from dashboard/dist/ at /app/)
# ---------------------------------------------------------------------------
_REACT_DIST = Path(__file__).resolve().parent.parent.parent / "dashboard" / "dist"


@app.route("/app/")
@app.route("/app/<path:path>")
def serve_react(path=""):
    """Serve the React SPA from the pre-built dashboard/dist/ directory."""
    if not _REACT_DIST.is_dir():
        return jsonify({"error": "React dashboard not built. Run: cd dashboard && npm run build"}), 404
    # Serve static assets (JS, CSS, images)
    full_path = _REACT_DIST / path
    if path and full_path.is_file():
        return send_from_directory(str(_REACT_DIST), path)
    # SPA fallback — serve index.html for all routes
    return send_from_directory(str(_REACT_DIST), "index.html")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _display_now() -> str:
    """Current time formatted in the user's display timezone."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(DISPLAY_TIMEZONE)
        return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _safe_account():
    """Get account info without crashing the dashboard."""
    try:
        return get_account()
    except Exception:
        log.exception("Failed to fetch account info")
        return {
            "portfolio_value": 0,
            "cash": 0,
            "buying_power": 0,
            "equity": 0,
            "status": "unavailable",
        }


def _safe_positions():
    try:
        return get_positions_from_alpaca()
    except Exception:
        log.exception("Failed to fetch positions from Alpaca")
        return []


# Rate-limit orphan reconciliation to avoid DB write contention with daemon
_last_reconcile_time = 0.0
_RECONCILE_INTERVAL = 120  # seconds — run at most once per 2 minutes


def _reconcile_orphan_trades(broker_positions: list) -> int:
    """Close stale open entry trades that should have been paired with an exit.

    Rate-limited to avoid DB write contention with the trading daemon.

    Works for both long entries (side='buy', closed by a sell) and short
    entries (side='sell', closed by a buy) — required for futures trading.

    Handles three scenarios per entry trade:
    1. Symbol no longer held at broker — the position was fully exited.
    2. Symbol IS still held but there's an opposite-side trade AFTER this
       entry in the DB (e.g. position was closed and re-entered).
    3. Symbol IS still held, no opposite-side trade recorded, but a NEWER
       same-side entry exists — the old entry is from a previous position
       that was closed before proper sell tracking was added.

    Returns the number of orphan trades closed.
    """
    global _last_reconcile_time
    now = time.time()
    if now - _last_reconcile_time < _RECONCILE_INTERVAL:
        return 0  # Skip — too soon since last reconciliation
    _last_reconcile_time = now

    try:
        open_trades = get_open_trades()
        if not open_trades:
            return 0

        # Build set of symbols currently held at the broker (all variants)
        held: set[str] = set()
        for p in broker_positions:
            sym = p.get("symbol", "")
            held.update(symbol_variants(sym))

        if not open_trades:
            return 0

        from trading.db.store import close_trade
        closed = 0

        for t in open_trades:
            side = t.get("side", "").lower()
            if side not in ("buy", "sell", "short"):
                continue

            sym = t.get("symbol", "")
            symbol_held = any(v in held for v in symbol_variants(sym))

            # The opposite side closes this entry
            closing_side = "sell" if side == "buy" else "buy"
            entry_ts = t.get("timestamp", "")

            # Look for an opposite-side trade that happened AFTER this entry
            try:
                with get_read_db() as conn:
                    variants = symbol_variants(sym)
                    placeholders = ",".join("?" for _ in variants)
                    row = conn.execute(
                        f"SELECT price, timestamp FROM trades WHERE symbol IN ({placeholders}) "
                        f"AND side=? AND timestamp > ? ORDER BY timestamp ASC LIMIT 1",
                        variants + [closing_side, entry_ts],
                    ).fetchone()
            except Exception:
                row = None

            if symbol_held and not row:
                # Check for a NEWER same-side entry — if one exists, this
                # old entry is from a previous position (pre-update trades).
                try:
                    with get_db() as conn2:
                        newer_entry = conn2.execute(
                            f"SELECT id FROM trades WHERE symbol IN ({placeholders}) "
                            f"AND side=? AND timestamp > ? AND id != ? LIMIT 1",
                            variants + [side, entry_ts, t["id"]],
                        ).fetchone()
                except Exception:
                    newer_entry = None

                if not newer_entry:
                    # Symbol still at broker, no exit after, no newer entry — genuinely open
                    continue
                # Old entry predates a re-entry — try to get actual exit price
                entry_price = t.get("price") or 0
                exit_price = entry_price  # fallback
                try:
                    from trading.execution.aster_client import get_aster_mark_prices
                    from trading.execution.router import _to_aster
                    aster_sym = _to_aster(sym)
                    mark_data = get_aster_mark_prices(aster_sym)
                    if isinstance(mark_data, dict) and mark_data.get("markPrice"):
                        exit_price = mark_data["markPrice"]
                except Exception:
                    pass
                qty_t = t.get("qty") or 0
                if entry_price > 0 and qty_t > 0:
                    if side == "buy":
                        pnl = (exit_price - entry_price) * qty_t
                    else:
                        pnl = (entry_price - exit_price) * qty_t
                else:
                    pnl = 0.0
                close_trade(t["id"], exit_price, round(pnl, 2))
                closed += 1
                log.info("Reconciled stale re-entry orphan #%d %s (%s)", t["id"], sym, side)
                continue

            # Either the symbol is gone from broker, or there's an exit after this entry
            if row and row["price"]:
                exit_price = row["price"]
            else:
                # No DB exit trade found — try to get current/last market price
                # so P&L reflects reality instead of defaulting to zero
                exit_price = None
                try:
                    from trading.execution.aster_client import get_aster_mark_prices
                    from trading.execution.router import _to_aster
                    aster_sym = _to_aster(sym)
                    mark_data = get_aster_mark_prices(aster_sym)
                    if isinstance(mark_data, dict):
                        exit_price = mark_data.get("markPrice")
                except Exception:
                    pass
                if not exit_price:
                    exit_price = t.get("price") or 0

            entry_price = t.get("price") or 0
            qty = t.get("qty") or 0
            if entry_price > 0:
                if side == "buy":
                    pnl = (exit_price - entry_price) * qty
                else:
                    # Short: profit when price drops
                    pnl = (entry_price - exit_price) * qty
            else:
                pnl = 0
            close_trade(t["id"], exit_price, round(pnl, 2))
            closed += 1
            log.info("Reconciled orphan trade #%d %s %s (P&L $%.2f)", t["id"], sym, side, pnl)

        return closed
    except Exception:
        log.debug("Orphan trade reconciliation error", exc_info=True)
        return 0


def _add_position_ages(positions: list) -> list:
    """Add human-readable age to each position based on earliest open buy trade.

    Falls back to the most recent buy trade if no explicitly 'open' trade is found.
    """
    if not positions:
        return positions
    try:
        now = datetime.now(timezone.utc)
        with get_read_db() as conn:
            for p in positions:
                sym = p["symbol"]
                variants = symbol_variants(sym)
                placeholders = ",".join("?" for _ in variants)

                # Try 1: earliest open buy trade (not closed)
                row = conn.execute(
                    f"SELECT MIN(timestamp) as opened FROM trades "
                    f"WHERE symbol IN ({placeholders}) AND side='buy' "
                    f"AND (status IS NULL OR status != 'closed') AND closed_at IS NULL",
                    variants,
                ).fetchone()

                # Try 2: fall back to earliest unclosed buy trade
                if not row or not row["opened"]:
                    row = conn.execute(
                        f"SELECT MIN(timestamp) as opened FROM trades "
                        f"WHERE symbol IN ({placeholders}) AND side='buy' AND closed_at IS NULL",
                        variants,
                    ).fetchone()

                # Try 3: fall back to most recent buy trade for this symbol
                if not row or not row["opened"]:
                    row = conn.execute(
                        f"SELECT MAX(timestamp) as opened FROM trades "
                        f"WHERE symbol IN ({placeholders}) AND side='buy'",
                        variants,
                    ).fetchone()

                if row and row["opened"]:
                    opened_str = row["opened"]
                    try:
                        opened = datetime.fromisoformat(opened_str.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        p["age"] = ""
                        continue
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=timezone.utc)
                    delta = now - opened
                    mins = int(delta.total_seconds() / 60)
                    if mins < 0:
                        p["age"] = "0m"
                    elif mins < 60:
                        p["age"] = f"{mins}m"
                    elif mins < 1440:
                        hours = mins // 60
                        remaining_mins = mins % 60
                        p["age"] = f"{hours}h {remaining_mins}m" if remaining_mins else f"{hours}h"
                    else:
                        days = mins // 1440
                        remaining_hours = (mins % 1440) // 60
                        p["age"] = f"{days}d {remaining_hours}h" if remaining_hours else f"{days}d"
                else:
                    p["age"] = "new"
    except Exception:
        log.debug("Could not compute position ages", exc_info=True)
    return positions


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def root_redirect():
    return redirect("/app/")


@app.route("/old-dashboard")
def dashboard():
    account = _safe_account()
    positions = _add_position_ages(sorted(_safe_positions(), key=lambda p: p.get("unrealized_pnl", 0) or 0, reverse=True))
    summary = get_action_log_summary()
    actions = get_action_log(limit=30)
    trades = get_trades(limit=15)
    signals = get_signals(limit=15)
    pnl_history = get_daily_pnl(limit=14)

    # Compute totals
    total_pnl = sum(p.get("unrealized_pnl", 0) or 0 for p in positions)
    positions_value = sum(
        p.get("market_value", p.get("qty", 0) * p.get("current_price", 0))
        for p in positions
    )

    # Group consecutive identical actions for cleaner display
    grouped_actions = []
    for a in actions:
        a_dict = dict(a) if not isinstance(a, dict) else a
        if (grouped_actions
                and grouped_actions[-1].get("action") == a_dict.get("action")
                and grouped_actions[-1].get("category") == a_dict.get("category")):
            grouped_actions[-1]["repeat_count"] = grouped_actions[-1].get("repeat_count", 1) + 1
        else:
            a_copy = dict(a_dict)
            a_copy["repeat_count"] = 1
            grouped_actions.append(a_copy)

    # Sparkline data: portfolio values from P&L history (oldest first)
    sparkline_data = json.dumps(
        [p.get("portfolio_value", 0) for p in reversed(pnl_history)]
    )

    return render_template(
        "dashboard.html",
        account=account,
        positions=positions,
        summary=summary,
        actions=grouped_actions,
        trades=trades,
        signals=signals,
        pnl_history=pnl_history,
        total_pnl=total_pnl,
        positions_value=positions_value,
        mode="PAPER" if get_setting("trading_mode", TRADING_MODE) == "paper" else "LIVE",
        risk=RISK,
        sparkline_data=sparkline_data,
        now=_display_now(),
    )


@app.route("/api/status")
def api_status():
    """JSON endpoint for programmatic access / auto-refresh."""
    account = _safe_account()
    raw_positions = _safe_positions()
    positions = _add_position_ages(sorted(raw_positions, key=lambda p: p.get("unrealized_pnl", 0) or 0, reverse=True))
    summary = get_action_log_summary()

    # Orphan trade reconciliation is now handled by the daemon (Phase 5 sync)
    # to avoid cross-process SQLite write contention with trade execution.

    # Enrich positions with leverage and strategy from their opening trades
    try:
        trades = get_trades(limit=200)
        # Build maps: symbol → max leverage, symbol → strategy
        symbol_leverage: dict[str, int] = {}
        symbol_strategy: dict[str, str] = {}
        for t in trades:
            sym = t.get("symbol", "")
            sym_flat = sym.replace("/", "")
            # Leverage: keep the highest
            lev = t.get("leverage")
            if lev and lev > 1:
                symbol_leverage[sym] = max(symbol_leverage.get(sym, 1), lev)
                symbol_leverage[sym_flat] = max(symbol_leverage.get(sym_flat, 1), lev)
            # Strategy: use the most recent open buy trade's strategy
            # If "aggregator", try to extract the primary contributing strategy
            if t.get("side") == "buy" and not t.get("closed_at"):
                strat = t.get("strategy", "")
                if strat and sym not in symbol_strategy:
                    # Try to get contributing strategies from entry_reasoning
                    if strat == "aggregator":
                        reasoning = t.get("entry_reasoning", "") or ""
                        # Extract first strategy name from reasoning (format: "Strategy: foo, bar")
                        for known in STRATEGY_ENABLED:
                            if known.replace("_", " ") in reasoning.lower() or known in reasoning.lower():
                                strat = known
                                break
                    symbol_strategy[sym] = strat
                    symbol_strategy[sym_flat] = strat
        for pos in positions:
            psym = pos.get("symbol", "")
            psym_flat = psym.replace("/", "")
            lev = symbol_leverage.get(psym) or symbol_leverage.get(psym_flat)
            if lev and lev > 1:
                pos["leverage"] = lev
            # Fill in strategy if broker returned empty
            if not pos.get("strategy"):
                strat = symbol_strategy.get(psym) or symbol_strategy.get(psym_flat)
                if strat:
                    pos["strategy"] = strat
    except Exception:
        pass

    # Merge with DB positions for any fields the broker doesn't provide
    try:
        db_positions = get_positions()
        db_by_symbol: dict[str, dict] = {}
        for dbp in db_positions:
            sym = dbp.get("symbol", "")
            db_by_symbol[sym] = dbp
            db_by_symbol[sym.replace("/", "")] = dbp
        for pos in positions:
            psym = pos.get("symbol", "")
            dbp = db_by_symbol.get(psym) or db_by_symbol.get(psym.replace("/", ""))
            if dbp:
                if not pos.get("strategy") and dbp.get("strategy"):
                    pos["strategy"] = dbp["strategy"]
    except Exception:
        pass

    # Include P&L from daily_pnl as fallback for position-level P&L
    pnl_snapshot = {}
    try:
        pnl_data = get_daily_pnl(limit=1)
        if pnl_data:
            latest = pnl_data[0]
            pnl_snapshot = {
                "portfolio_value": latest.get("portfolio_value", 0),
                "daily_return": latest.get("daily_return", 0),
                "cumulative_return": latest.get("cumulative_return", 0),
                "date": latest.get("date", ""),
            }
    except Exception:
        pass

    return jsonify({
        "account": account,
        "positions": positions,
        "summary": summary,
        "mode": get_setting("trading_mode", TRADING_MODE),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pnl_snapshot": pnl_snapshot,
    })


@app.route("/api/actions")
def api_actions():
    limit = int(request.args.get("limit", 30))
    offset = int(request.args.get("offset", 0))
    actions = get_action_log(limit=limit + offset)
    return jsonify(actions[offset:])


@app.route("/api/trades")
def api_trades():
    trades = get_trades(limit=50)

    # Build maps of the most recent entry price per symbol for P&L computation
    # In futures, entries can be buy (long) or sell (short)
    buy_prices: dict[str, float] = {}
    sell_prices: dict[str, float] = {}
    for t in reversed(trades):  # oldest first
        side = t.get("side", "")
        price_val = t.get("price") or 0
        if price_val <= 0:
            continue
        target = buy_prices if side == "buy" else sell_prices if side in ("sell", "short") else None
        if target is None:
            continue
        for v in symbol_variants(t["symbol"]):
            target[v] = price_val

    for t in trades:
        price = t.get("price") or 0
        close_price = t.get("close_price") or 0
        pnl = t.get("pnl")
        qty = abs(t.get("qty", 0) or 0)
        side = t.get("side", "")

        # Mark each trade with consistent open/closed status
        # In futures trading, both buy (long) and sell/short entries can be open positions
        t["is_open"] = (t.get("closed_at") is None and t.get("status") != "closed")

        # If sell trade has no P&L, compute from buy entry price
        if pnl is None and side == "sell" and price > 0 and qty > 0:
            entry = buy_prices.get(t.get("symbol", ""))
            if entry and entry > 0:
                pnl = round((price - entry) * qty, 2)
                t["pnl"] = pnl

        if pnl is not None and price > 0 and qty > 0:
            entry_for_pct = buy_prices.get(t.get("symbol", ""), price)
            if entry_for_pct > 0:
                t["pnl_pct"] = round((price - entry_for_pct) / entry_for_pct * 100, 2) if side == "sell" else round(pnl / (entry_for_pct * qty) * 100, 2)
            else:
                t["pnl_pct"] = round(pnl / (price * qty) * 100, 2)
        elif close_price > 0 and price > 0:
            t["pnl_pct"] = round((close_price - price) / price * 100, 2)
        else:
            t["pnl_pct"] = None
    return jsonify(trades)


@app.route("/api/position/<symbol>")
def api_position_detail(symbol):
    """Detailed breakdown for a single position — strategies, reasoning, risk."""
    import json as _json
    from trading.config import RISK
    from trading.risk.manager import CORRELATION_GROUPS, LEVERAGE_FACTORS, compute_trade_targets
    from trading.risk.profit_manager import TAKE_PROFIT_PCT, TRAILING_STOP_ACTIVATE, TRAILING_STOP_PCT

    # Normalize: allow both BTCUSD and BTC/USD lookups
    match_symbols = symbol_variants(symbol)

    # Current position from Alpaca
    positions = _safe_positions()
    pos = next((p for p in positions if p["symbol"] in match_symbols), None)
    if not pos:
        return jsonify({"error": f"No open position for {symbol}"}), 404

    current_price = pos["current_price"]
    avg_cost = pos["avg_cost"]
    qty = pos["qty"]
    market_value = pos.get("market_value", qty * current_price)
    pnl = pos.get("unrealized_pnl", 0) or 0
    pnl_pct = ((current_price - avg_cost) / avg_cost * 100) if avg_cost else 0

    # All trades for this symbol
    placeholders = ",".join("?" for _ in match_symbols)
    with get_read_db() as conn:
        trades = [dict(r) for r in conn.execute(
            f"SELECT * FROM trades WHERE symbol IN ({placeholders}) ORDER BY timestamp DESC LIMIT 20",
            match_symbols,
        ).fetchall()]

        # All signals for this symbol (last 50)
        signals = [dict(r) for r in conn.execute(
            f"SELECT * FROM signals WHERE symbol IN ({placeholders}) ORDER BY timestamp DESC LIMIT 50",
            match_symbols,
        ).fetchall()]

        # Journal entries for trades on this symbol
        journal_entries = [dict(r) for r in conn.execute(
            f"SELECT j.*, t.symbol, t.side, t.qty, t.price, t.strategy "
            f"FROM journal j JOIN trades t ON j.trade_id = t.id "
            f"WHERE t.symbol IN ({placeholders}) ORDER BY j.timestamp DESC LIMIT 10",
            match_symbols,
        ).fetchall()]

    # Parse signal data JSON
    for s in signals:
        if s.get("data") and isinstance(s["data"], str):
            try:
                s["data"] = _json.loads(s["data"])
            except Exception:
                pass

    # Parse journal market_context JSON
    for j in journal_entries:
        if j.get("market_context") and isinstance(j["market_context"], str):
            try:
                j["market_context"] = _json.loads(j["market_context"])
            except Exception:
                pass

    # Strategies that contributed to opening this position
    contributing_strategies = list({t["strategy"] for t in trades if t["strategy"] and t["side"] == "buy"})

    # Recent buy signals with reasoning
    buy_signals = [s for s in signals if s["signal"] == "buy"]
    latest_reasons = {}
    for s in buy_signals:
        strat = s["strategy"]
        if strat not in latest_reasons:
            latest_reasons[strat] = {
                "strategy": strat,
                "strength": s["strength"],
                "timestamp": s["timestamp"],
                "data": s.get("data"),
            }

    # Risk analysis — use stored trade targets if available, else compute live
    buy_trades = [t for t in trades if t["side"] == "buy"]
    latest_buy = buy_trades[0] if buy_trades else None

    # Check if trade has stored SL/TP targets
    stored_sl = latest_buy.get("stop_loss_price") if latest_buy else None
    stored_tp = latest_buy.get("take_profit_price") if latest_buy else None
    stored_trail = latest_buy.get("trailing_stop_activate") if latest_buy else None
    stored_rr = latest_buy.get("risk_reward_ratio") if latest_buy else None

    if stored_sl and stored_tp:
        # Use targets from the trade record
        stop_loss_price = stored_sl
        take_profit_price = stored_tp
        trailing_activate_price = stored_trail or avg_cost * (1 + TRAILING_STOP_ACTIVATE)
        rr_ratio = stored_rr or 0
    else:
        # Compute targets live (for legacy trades without stored targets)
        targets = compute_trade_targets(
            symbol=pos["symbol"],
            entry_price=avg_cost,
            order_value=market_value,
        )
        stop_loss_price = targets.stop_loss_price
        take_profit_price = targets.take_profit_price
        trailing_activate_price = targets.trailing_stop_activate_price
        rr_ratio = targets.risk_reward_ratio

    stop_loss_value = (stop_loss_price - current_price) * qty
    take_profit_value = (take_profit_price - current_price) * qty
    distance_to_stop = ((current_price - stop_loss_price) / current_price * 100) if current_price else 0
    distance_to_tp = ((take_profit_price - current_price) / current_price * 100) if current_price else 0
    sl_pct = ((avg_cost - stop_loss_price) / avg_cost * 100) if avg_cost else 0
    tp_pct = ((take_profit_price - avg_cost) / avg_cost * 100) if avg_cost else 0

    leverage = LEVERAGE_FACTORS.get(pos["symbol"], 1.0)

    # Find correlation group
    corr_group = None
    for group_name, group_symbols in CORRELATION_GROUPS.items():
        if symbol_slash in group_symbols or symbol_flat in group_symbols:
            corr_group = group_name
            break

    # Total invested (sum of all buy trades)
    total_invested = sum(t["total"] or 0 for t in trades if t["side"] == "buy")

    return jsonify({
        "symbol": pos["symbol"],
        "qty": qty,
        "avg_cost": avg_cost,
        "current_price": current_price,
        "market_value": market_value,
        "pnl": pnl,
        "pnl_pct": round(pnl_pct, 2),
        "total_invested": total_invested,
        "contributing_strategies": contributing_strategies,
        "strategy_reasons": latest_reasons,
        "trades": trades,
        "journal_entries": journal_entries,
        "risk": {
            "stop_loss_pct": round(sl_pct, 2),
            "stop_loss_price": round(stop_loss_price, 2),
            "stop_loss_value": round(stop_loss_value, 2),
            "distance_to_stop_pct": round(distance_to_stop, 2),
            "take_profit_pct": round(tp_pct, 2),
            "take_profit_price": round(take_profit_price, 2),
            "take_profit_value": round(take_profit_value, 2),
            "distance_to_tp_pct": round(distance_to_tp, 2),
            "trailing_activate_price": round(trailing_activate_price, 2),
            "risk_reward_ratio": round(rr_ratio, 2),
            "leverage": leverage,
            "correlation_group": corr_group,
            "max_position_pct": RISK["max_position_pct"] * 100,
            "max_daily_loss_pct": RISK["max_daily_loss_pct"] * 100,
        },
        "recent_signals": buy_signals[:10],
    })


@app.route("/api/trade/<int:trade_id>")
def api_trade_detail(trade_id):
    """Full detail for a single trade — journal, signals, risk targets."""
    import json as _json

    with get_read_db() as conn:
        trade = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not trade:
            return jsonify({"error": f"Trade #{trade_id} not found"}), 404
        trade = dict(trade)

        # Journal entry for this trade
        journal = [dict(r) for r in conn.execute(
            "SELECT * FROM journal WHERE trade_id=? ORDER BY timestamp DESC", (trade_id,)
        ).fetchall()]
        for j in journal:
            if j.get("market_context") and isinstance(j["market_context"], str):
                try:
                    j["market_context"] = _json.loads(j["market_context"])
                except Exception:
                    pass

        # Related signals (same symbol, within 1 hour before the trade)
        signals = []
        if trade.get("symbol") and trade.get("timestamp"):
            sym = trade["symbol"]
            sym_variants = symbol_variants(sym)
            placeholders = ",".join("?" for _ in sym_variants)
            signals = [dict(r) for r in conn.execute(
                f"SELECT * FROM signals WHERE symbol IN ({placeholders}) "
                f"AND timestamp <= ? ORDER BY timestamp DESC LIMIT 10",
                sym_variants + [trade["timestamp"]],
            ).fetchall()]
            for s in signals:
                if s.get("data") and isinstance(s["data"], str):
                    try:
                        s["data"] = _json.loads(s["data"])
                    except Exception:
                        pass

    # Trade analyses (12h cycle updates)
    analyses = []
    try:
        from trading.db.store import get_trade_analyses
        analyses = get_trade_analyses(trade_id, limit=20)
    except Exception:
        pass

    return jsonify({"trade": trade, "journal": journal, "signals": signals, "analyses": analyses})


@app.route("/api/trade/<int:trade_id>/analyses")
def api_trade_analyses(trade_id):
    """Get analysis timeline for a trade."""
    try:
        from trading.db.store import get_trade_analyses
        analyses = get_trade_analyses(trade_id, limit=20)
        return jsonify(analyses)
    except Exception as e:
        return jsonify([])


@app.route("/api/signal/<int:signal_id>")
def api_signal_detail(signal_id):
    """Full detail for a single signal."""
    import json as _json

    with get_read_db() as conn:
        sig = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        if not sig:
            return jsonify({"error": f"Signal #{signal_id} not found"}), 404
        sig = dict(sig)
        if sig.get("data") and isinstance(sig["data"], str):
            try:
                sig["data"] = _json.loads(sig["data"])
            except Exception:
                pass

        # Find trades that happened shortly after this signal (same symbol, within 5 min)
        related_trades = []
        if sig.get("symbol") and sig.get("timestamp"):
            sym = sig["symbol"]
            sym_variants = symbol_variants(sym)
            placeholders = ",".join("?" for _ in sym_variants)
            related_trades = [dict(r) for r in conn.execute(
                f"SELECT * FROM trades WHERE symbol IN ({placeholders}) "
                f"AND timestamp >= ? ORDER BY timestamp ASC LIMIT 5",
                sym_variants + [sig["timestamp"]],
            ).fetchall()]

    return jsonify({"signal": sig, "related_trades": related_trades})


@app.route("/api/action/<int:action_id>")
def api_action_detail(action_id):
    """Full detail for a single action log entry."""
    import json as _json

    with get_read_db() as conn:
        action = conn.execute("SELECT * FROM action_log WHERE id=?", (action_id,)).fetchone()
        if not action:
            return jsonify({"error": f"Action #{action_id} not found"}), 404
        action = dict(action)
        if action.get("data") and isinstance(action["data"], str):
            try:
                action["data"] = _json.loads(action["data"])
            except Exception:
                pass

        # Get neighboring actions for context (5 before and 5 after)
        context_before = [dict(r) for r in conn.execute(
            "SELECT * FROM action_log WHERE id < ? ORDER BY id DESC LIMIT 5", (action_id,)
        ).fetchall()]
        context_after = [dict(r) for r in conn.execute(
            "SELECT * FROM action_log WHERE id > ? ORDER BY id ASC LIMIT 5", (action_id,)
        ).fetchall()]

    # Get or generate narrative
    narrative_data = {"narrative": "", "interpretation": {}, "lessons": [], "quality_score": None}
    try:
        from trading.intelligence.action_narrator import get_or_generate_narrative
        narrative_data = get_or_generate_narrative(action)
    except Exception as e:
        log.warning("Narrative generation failed for action #%s: %s", action_id, e)

    return jsonify({
        "action": action,
        "context_before": list(reversed(context_before)),
        "context_after": context_after,
        "narrative": narrative_data.get("narrative", ""),
        "interpretation": narrative_data.get("interpretation", {}),
        "lessons": narrative_data.get("lessons", []),
        "quality_score": narrative_data.get("quality_score"),
    })


@app.route("/api/actions/generate-narratives", methods=["POST"])
def api_generate_narratives():
    """Trigger narrative generation for recent un-narrated actions."""
    try:
        from trading.intelligence.action_narrator import generate_missing_narratives
        count = generate_missing_narratives(limit=20)
        return jsonify({"generated": count})
    except Exception as e:
        log.warning("Batch narrative generation failed: %s", e)
        return jsonify({"generated": 0, "error": str(e)})


@app.route("/api/pnl/history")
def api_pnl_history():
    """Daily P&L time series for charts. Returns up to 90 days."""
    import json as _json
    from trading.db.store import get_daily_pnl

    days = min(int(request.args.get("days", 90)), 365)
    daily = get_daily_pnl(limit=days)

    # Reverse to chronological order (oldest first)
    daily.reverse()

    # Also compute weekly and monthly aggregations
    weekly = {}
    monthly = {}
    for d in daily:
        date_str = d.get("date", "")
        portfolio_value = d.get("portfolio_value", 0)
        daily_return = d.get("daily_return", 0) or 0

        # Week key: ISO year-week
        try:
            from datetime import date as dt_date
            parts = date_str.split("-")
            dt = dt_date(int(parts[0]), int(parts[1]), int(parts[2]))
            week_key = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
            month_key = date_str[:7]  # YYYY-MM
        except Exception:
            continue

        if week_key not in weekly:
            weekly[week_key] = {"period": week_key, "start_value": portfolio_value, "end_value": portfolio_value, "trades": 0, "return_pct": 0}
        weekly[week_key]["end_value"] = portfolio_value

        if month_key not in monthly:
            monthly[month_key] = {"period": month_key, "start_value": portfolio_value, "end_value": portfolio_value, "trades": 0, "return_pct": 0}
        monthly[month_key]["end_value"] = portfolio_value

    # Compute period returns
    for w in weekly.values():
        if w["start_value"] > 0:
            w["return_pct"] = round((w["end_value"] - w["start_value"]) / w["start_value"] * 100, 2)
    for m in monthly.values():
        if m["start_value"] > 0:
            m["return_pct"] = round((m["end_value"] - m["start_value"]) / m["start_value"] * 100, 2)

    # Add trade counts per period
    with get_read_db() as conn:
        trades = conn.execute(
            "SELECT timestamp, side FROM trades WHERE timestamp >= ? ORDER BY timestamp",
            (daily[0]["date"] if daily else "2000-01-01",),
        ).fetchall()
        for t in trades:
            ts = t["timestamp"][:10]
            try:
                parts = ts.split("-")
                dt = dt_date(int(parts[0]), int(parts[1]), int(parts[2]))
                wk = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
                mk = ts[:7]
                if wk in weekly:
                    weekly[wk]["trades"] += 1
                if mk in monthly:
                    monthly[mk]["trades"] += 1
            except Exception:
                continue

    return jsonify({
        "daily": daily,
        "weekly": sorted(weekly.values(), key=lambda x: x["period"]),
        "monthly": sorted(monthly.values(), key=lambda x: x["period"]),
    })


@app.route("/api/pnl/<date>")
def api_pnl_detail(date):
    """Full detail for a single day's P&L — all trades, signals, actions."""
    import json as _json

    with get_read_db() as conn:
        pnl = conn.execute("SELECT * FROM daily_pnl WHERE date=?", (date,)).fetchone()
        if not pnl:
            return jsonify({"error": f"No P&L data for {date}"}), 404
        pnl = dict(pnl)

        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE timestamp LIKE ? ORDER BY timestamp", (f"{date}%",)
        ).fetchall()]

        signals = [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE timestamp LIKE ? ORDER BY timestamp", (f"{date}%",)
        ).fetchall()]
        for s in signals:
            if s.get("data") and isinstance(s["data"], str):
                try:
                    s["data"] = _json.loads(s["data"])
                except Exception:
                    pass

        actions = [dict(r) for r in conn.execute(
            "SELECT * FROM action_log WHERE timestamp LIKE ? ORDER BY timestamp", (f"{date}%",)
        ).fetchall()]

    # Compute day stats
    buys = [t for t in trades if t["side"] == "buy"]
    sells = [t for t in trades if t["side"] == "sell"]
    buy_volume = sum(t.get("total") or 0 for t in buys)
    sell_volume = sum(t.get("total") or 0 for t in sells)
    strategies_active = list({t["strategy"] for t in trades if t.get("strategy")})
    symbols_traded = list({t["symbol"] for t in trades if t.get("symbol")})

    return jsonify({
        "pnl": pnl,
        "trades": trades,
        "signals": signals,
        "actions": actions,
        "stats": {
            "total_trades": len(trades),
            "buys": len(buys),
            "sells": len(sells),
            "buy_volume": round(buy_volume, 2),
            "sell_volume": round(sell_volume, 2),
            "strategies_active": strategies_active,
            "symbols_traded": symbols_traded,
            "total_signals": len(signals),
            "total_actions": len(actions),
        },
    })


@app.route("/api/strategies")
def api_strategies():
    """Strategy performance breakdown for dashboard."""
    import json as _json
    from trading.config import STRATEGY_ENABLED

    enabled = {k: v for k, v in STRATEGY_ENABLED.items() if v}

    # Signal counts per strategy
    signals = get_signals(limit=500)
    strat_data: dict[str, dict] = {}
    for s in signals:
        name = s["strategy"]
        if name not in strat_data:
            strat_data[name] = {"signals": 0, "buys": 0, "sells": 0, "holds": 0}
        strat_data[name]["signals"] += 1
        if s["signal"] == "buy":
            strat_data[name]["buys"] += 1
        elif s["signal"] == "sell":
            strat_data[name]["sells"] += 1
        else:
            strat_data[name]["holds"] += 1

    # Trade counts and P&L per strategy
    # Helper: resolve trade strategy to contributing enabled strategies
    enabled_names = set(enabled.keys())

    def _resolve_strategies(trade_strategy: str, symbol: str = "") -> list[str]:
        """Map a trade's strategy to one or more enabled strategy names.

        Handles: direct match, confluence 'a+b+c', 'aggregator' (lookup signals),
        'profit_mgmt_*' and 'stop_loss' (lookup opening trade for same symbol).
        """
        if not trade_strategy:
            return []
        # Direct match
        if trade_strategy in enabled_names:
            return [trade_strategy]
        # Confluence: kalman_trend+hmm_regime → [kalman_trend, hmm_regime]
        if "+" in trade_strategy:
            parts = [p.strip() for p in trade_strategy.split("+")]
            return [p for p in parts if p in enabled_names] or parts[:1]
        # aggregator: try to find what signals triggered this trade
        if trade_strategy == "aggregator" and symbol:
            matched = set()
            for s in signals:
                if s.get("symbol") == symbol:
                    sn = s["strategy"]
                    if sn in enabled_names:
                        matched.add(sn)
                    elif "+" in sn:
                        for p in sn.split("+"):
                            if p.strip() in enabled_names:
                                matched.add(p.strip())
            return list(matched) if matched else ["aggregator"]
        # profit_mgmt_*, stop_loss: attribute to original trade's strategy for same symbol
        if (trade_strategy.startswith("profit_mgmt_") or trade_strategy == "stop_loss") and symbol:
            # Find the buy trade for this symbol to get its strategy
            for prev_t in trades:
                if prev_t.get("symbol") == symbol and prev_t.get("side") == "buy":
                    prev_strat = prev_t.get("strategy", "")
                    if prev_strat and prev_strat != trade_strategy:
                        return _resolve_strategies(prev_strat, symbol)
        return [trade_strategy]

    trades = get_trades(limit=500)
    for t in trades:
        raw_name = t.get("strategy", "")
        if not raw_name:
            continue
        resolved = _resolve_strategies(raw_name, t.get("symbol", ""))
        pnl_share = (t.get("pnl") or 0) / max(len(resolved), 1)
        for name in resolved:
            if name not in strat_data:
                strat_data[name] = {"signals": 0, "buys": 0, "sells": 0, "holds": 0}
            strat_data[name].setdefault("trades", 0)
            strat_data[name]["trades"] = strat_data[name].get("trades", 0) + 1
            if t.get("pnl") is not None:
                strat_data[name].setdefault("total_pnl", 0)
                strat_data[name]["total_pnl"] = strat_data[name].get("total_pnl", 0) + pnl_share
            if t.get("closed_at"):
                strat_data[name].setdefault("closed", 0)
                strat_data[name]["closed"] = strat_data[name].get("closed", 0) + 1
                if pnl_share > 0:
                    strat_data[name].setdefault("wins", 0)
                    strat_data[name]["wins"] = strat_data[name].get("wins", 0) + 1

    # Add unrealized P&L from open positions
    positions = _safe_positions()
    for p in positions:
        strat = p.get("strategy", "")
        if strat and strat in strat_data:
            strat_data[strat].setdefault("total_pnl", 0)
            strat_data[strat]["total_pnl"] += (p.get("unrealized_pnl") or 0)

    result = []
    for name in sorted(enabled.keys()):
        d = strat_data.get(name, {})
        closed = d.get("closed", 0)
        wins = d.get("wins", 0)
        result.append({
            "name": name,
            "enabled": True,
            "signals": d.get("signals", 0),
            "trades": d.get("trades", 0),
            "buys": d.get("buys", 0),
            "sells": d.get("sells", 0),
            "closed_trades": closed,
            "win_rate": round(wins / closed, 2) if closed > 0 else None,
            "total_pnl": round(d.get("total_pnl", 0), 2),
        })

    return jsonify(result)


@app.route("/api/intelligence")
def api_intelligence():
    """Market intelligence and sentiment data for dashboard."""
    result = {"fear_greed": None, "briefings": [], "regime_signals": []}

    # Fear & Greed
    try:
        from trading.data.sentiment import get_fear_greed
        fg = get_fear_greed(limit=7)
        if fg and fg.get("current"):
            result["fear_greed"] = fg["current"]  # {value, classification, timestamp}
    except Exception as e:
        log.debug("Fear & Greed fetch failed: %s", e)

    # Recent intelligence briefings from action log
    with get_read_db() as conn:
        rows = conn.execute(
            "SELECT * FROM action_log WHERE "
            "(action LIKE '%briefing%' OR action LIKE '%intelligence%' OR action LIKE '%regime%') "
            "ORDER BY timestamp DESC LIMIT 5"
        ).fetchall()
        result["briefings"] = [dict(r) for r in rows]

        # Regime signals
        rows = conn.execute(
            "SELECT * FROM signals WHERE strategy IN ('hmm_regime', 'volatility_regime', 'regime_mean_reversion') "
            "ORDER BY timestamp DESC LIMIT 5"
        ).fetchall()
        regime_sigs = []
        for r in rows:
            d = dict(r)
            if d.get("data") and isinstance(d["data"], str):
                try:
                    import json as _json
                    d["data"] = _json.loads(d["data"])
                except Exception:
                    pass
            # Fix: when strength is 0, extract confidence from regime data fields
            if (d.get("strength") or 0) == 0 and isinstance(d.get("data"), dict):
                rd = d["data"]
                prob = (
                    rd.get("regime_prob")
                    or rd.get("probability")
                    or rd.get("confidence")
                    or 0
                )
                if not prob:
                    # volatility regime: derive from vol_ratio distance from 1.0
                    vol_ratio = rd.get("vol_ratio")
                    if vol_ratio is not None:
                        prob = min(abs(float(vol_ratio) - 1.0), 1.0)
                if not prob:
                    # regime_mean_reversion: derive from ADX
                    adx = rd.get("adx")
                    if adx is not None:
                        prob = min(float(adx) / 100.0, 1.0)
                if prob:
                    d["strength"] = round(float(prob), 3)
            regime_sigs.append(d)
        result["regime_signals"] = regime_sigs

        # News analysis — latest LLM interpretation from news agent
        try:
            news_rows = conn.execute(
                "SELECT * FROM action_log WHERE "
                "(action = 'news_analysis' OR action LIKE '%news_interpretation%') "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchall()
            if news_rows:
                news_data = dict(news_rows[0])
                if isinstance(news_data.get("data"), str):
                    try:
                        news_data["data"] = json.loads(news_data["data"])
                    except Exception:
                        pass
                data = news_data.get("data", {})
                if isinstance(data, dict):
                    result["news_analysis"] = {
                        "timestamp": news_data.get("timestamp", ""),
                        "interpretation": data.get("full_analysis") or news_data.get("details", ""),
                        "headline_count": data.get("headline_count", 0),
                        "source_count": data.get("source_count", 0),
                        "regime": data.get("regime", ""),
                    }
        except Exception:
            pass

        # Asset impacts from latest briefing or news analysis
        try:
            briefing_rows = conn.execute(
                "SELECT data FROM action_log WHERE "
                "(action LIKE '%briefing%' OR action = 'news_analysis') "
                "AND data IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 3"
            ).fetchall()
            for brow in briefing_rows:
                bdata = brow["data"]
                if isinstance(bdata, str):
                    try:
                        bdata = json.loads(bdata)
                    except Exception:
                        continue
                if isinstance(bdata, dict):
                    if bdata.get("asset_impacts") and "asset_impacts" not in result:
                        result["asset_impacts"] = bdata["asset_impacts"]
                    if bdata.get("news_interpretation") and "news_interpretation" not in result:
                        result["news_interpretation"] = bdata["news_interpretation"][:5000]
        except Exception:
            pass

        # Live headlines — fetch current headlines for display
        try:
            from trading.data.news import fetch_all_headlines
            headlines = fetch_all_headlines(max_per_source=3)
            result["headlines"] = [
                {"title": h.get("title", ""), "source": h.get("source", ""),
                 "category": h.get("category", ""), "published": h.get("published", "")}
                for h in headlines[:15]
            ]
        except Exception:
            result["headlines"] = []

    return jsonify(result)


@app.route("/api/agents")
def api_agents():
    """Autonomous agent recommendations and activity."""
    from trading.db.store import get_recommendation_history, get_pending_recommendations

    result = {"pending": [], "recent": [], "activity": [], "agent_stats": []}

    try:
        import json as _json
        recs = get_recommendation_history(limit=100)

        # Parse data field from JSON string if needed
        for r in recs:
            if r.get("data") and isinstance(r["data"], str):
                try:
                    r["data"] = _json.loads(r["data"])
                except Exception:
                    pass

        result["pending"] = [r for r in recs if r.get("status") == "pending"]
        result["recent"] = [r for r in recs if r.get("status") != "pending"][:10]

        # Compute per-agent performance stats
        agent_names = [
            "performance_agent", "research_agent", "risk_agent",
            "regime_agent", "learning_agent", "backtest_agent",
        ]
        agent_stats = {}
        for r in recs:
            agent = r.get("from_agent", "unknown")
            # Skip executor_agent from tiles — it's the executor, not a thinking agent
            if agent == "executor_agent":
                continue
            if agent not in agent_stats:
                agent_stats[agent] = {
                    "name": agent,
                    "total": 0,
                    "applied": 0,
                    "rejected": 0,
                    "pending": 0,
                    "last_active": None,
                    "categories": {},
                }
            s = agent_stats[agent]
            s["total"] += 1
            status = r.get("status", "pending")
            resolution = r.get("resolution", "")
            if status == "pending":
                s["pending"] += 1
            elif resolution in ("applied", "accepted"):
                s["applied"] += 1
            elif resolution in ("rejected",):
                s["rejected"] += 1
            cat = r.get("category", "other")
            s["categories"][cat] = s["categories"].get(cat, 0) + 1
            ts = r.get("timestamp")
            if ts and (s["last_active"] is None or ts > s["last_active"]):
                s["last_active"] = ts

        # Ensure all known agents appear even with 0 recs
        for name in agent_names:
            if name not in agent_stats:
                agent_stats[name] = {
                    "name": name,
                    "total": 0, "applied": 0, "rejected": 0, "pending": 0,
                    "last_active": None, "categories": {},
                }

        result["agent_stats"] = sorted(
            agent_stats.values(), key=lambda x: x["total"], reverse=True,
        )
    except Exception:
        pass

    # Agent-related actions from action log
    with get_read_db() as conn:
        rows = conn.execute(
            "SELECT * FROM action_log WHERE category IN ('review', 'system', 'scheduler') "
            "ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
        result["activity"] = [dict(r) for r in rows]

    return jsonify(result)


@app.route("/api/allocation")
def api_allocation():
    """Portfolio allocation configuration and current state."""
    from trading.config import STRATEGY_ENABLED

    result = {"strategies": [], "factors": [
        "Base Budget", "Confluence Boost", "Regime Alignment",
        "Performance Tilt", "Volatility Scaling", "Signal Strength",
    ]}

    try:
        from trading.risk.portfolio import STRATEGY_BUDGETS, DEFAULT_BUDGET
        enabled = {k for k, v in STRATEGY_ENABLED.items() if v}
        for name in sorted(enabled):
            result["strategies"].append({
                "name": name,
                "base_pct": round(STRATEGY_BUDGETS.get(name, DEFAULT_BUDGET) * 100, 1),
            })
    except ImportError:
        pass

    return jsonify(result)


@app.route("/api/strategy/<name>")
def api_strategy_detail(name):
    """Detailed breakdown for a single strategy — signals, trades, performance, config."""
    import json as _json
    from trading.config import STRATEGY_ENABLED

    result = {
        "name": name,
        "enabled": STRATEGY_ENABLED.get(name, False),
        "signals": [],
        "trades": [],
        "recommendations": [],
        "backtest": None,
        "config": {},
    }

    # Recent signals for this strategy
    with get_read_db() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE strategy = ? ORDER BY timestamp DESC LIMIT 30", (name,)
        ).fetchall()
        for r in rows:
            d = dict(r)
            if d.get("data") and isinstance(d["data"], str):
                try:
                    d["data"] = _json.loads(d["data"])
                except Exception:
                    pass
            result["signals"].append(d)

        # Trade history for this strategy
        rows = conn.execute(
            "SELECT * FROM trades WHERE strategy = ? ORDER BY timestamp DESC LIMIT 20", (name,)
        ).fetchall()
        result["trades"] = [dict(r) for r in rows]

        # Agent recommendations about this strategy
        rows = conn.execute(
            "SELECT * FROM agent_recommendations WHERE target = ? ORDER BY timestamp DESC LIMIT 10", (name,)
        ).fetchall()
        for r in rows:
            d = dict(r)
            if d.get("data") and isinstance(d["data"], str):
                try:
                    d["data"] = _json.loads(d["data"])
                except Exception:
                    pass
            result["recommendations"].append(d)

    # Budget allocation
    try:
        from trading.risk.portfolio import STRATEGY_BUDGETS, DEFAULT_BUDGET
        result["config"]["base_budget_pct"] = round(STRATEGY_BUDGETS.get(name, DEFAULT_BUDGET) * 100, 2)
    except Exception:
        pass

    # Latest backtest results
    from pathlib import Path as _Path
    backtests_dir = _Path(__file__).parent.parent / "knowledge" / "backtests"
    if backtests_dir.exists():
        bt_files = sorted(backtests_dir.glob("backtest_*.json"), reverse=True)
        for bf in bt_files[:5]:
            try:
                bt = _json.loads(bf.read_text())
                strategies = bt.get("strategies", {})
                if name in strategies:
                    result["backtest"] = strategies[name]
                    result["backtest"]["file"] = bf.name
                    break
            except Exception:
                pass

    # Compute stats
    closed = [t for t in result["trades"] if t.get("closed_at")]
    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    total_pnl = sum(t.get("pnl", 0) or 0 for t in closed)
    result["stats"] = {
        "total_trades": len(result["trades"]),
        "closed_trades": len(closed),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(closed), 2) if closed else None,
        "total_pnl": round(total_pnl, 2),
        "total_signals": len(result["signals"]),
        "buy_signals": len([s for s in result["signals"] if s.get("signal") == "buy"]),
        "sell_signals": len([s for s in result["signals"] if s.get("signal") == "sell"]),
    }

    return jsonify(result)


@app.route("/api/recommendation/<int:rec_id>")
def api_recommendation_detail(rec_id):
    """Detailed view of a single agent recommendation."""
    import json as _json

    with get_read_db() as conn:
        row = conn.execute("SELECT * FROM agent_recommendations WHERE id = ?", (rec_id,)).fetchone()
        if not row:
            return jsonify({"error": "Recommendation not found"}), 404

        rec = dict(row)
        if rec.get("data") and isinstance(rec["data"], str):
            try:
                rec["data"] = _json.loads(rec["data"])
            except Exception:
                pass

        # Get other recommendations from same agent around same time
        related = conn.execute(
            "SELECT * FROM agent_recommendations WHERE from_agent = ? AND id != ? "
            "ORDER BY ABS(JULIANDAY(timestamp) - JULIANDAY(?)) LIMIT 5",
            (rec["from_agent"], rec_id, rec["timestamp"]),
        ).fetchall()
        related_recs = []
        for r in related:
            d = dict(r)
            if d.get("data") and isinstance(d["data"], str):
                try:
                    d["data"] = _json.loads(d["data"])
                except Exception:
                    pass
            related_recs.append(d)

        # Get actions that resulted from this recommendation
        actions = conn.execute(
            "SELECT * FROM action_log WHERE category = 'autonomous' "
            "AND timestamp >= ? ORDER BY timestamp ASC LIMIT 5",
            (rec["timestamp"],),
        ).fetchall()

    return jsonify({
        "recommendation": rec,
        "related": related_recs,
        "resulting_actions": [dict(a) for a in actions],
    })


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Chat assistant endpoint — operator console first, then read-only fallback."""
    from trading.monitor.operator import handle_operator_message
    from trading.monitor.chat import handle_chat

    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "No message provided"}), 400

    try:
        # Try operator console first (actions + enhanced reads)
        result = handle_operator_message(message)
        if result is not None:
            return jsonify(result)
        # Fall through to read-only chat
        answer = handle_chat(message)
        return jsonify({"answer": answer})
    except Exception as exc:
        log.exception("Chat handler error")
        return jsonify({"answer": f"Sorry, I encountered an error: {exc}"}), 200


@app.route("/api/chat/confirm", methods=["POST"])
def api_chat_confirm():
    """Confirm a pending operator action."""
    from trading.monitor.operator import handle_operator_message

    data = request.get_json(silent=True) or {}
    action_id = data.get("action_id", "").strip()
    if not action_id:
        return jsonify({"error": "No action_id provided"}), 400

    try:
        result = handle_operator_message("", confirmed_action_id=action_id)
        return jsonify(result)
    except Exception as exc:
        log.exception("Operator confirm error")
        return jsonify({"answer": f"Action failed: {exc}"}), 200


@app.route("/api/llm/status", methods=["GET"])
def api_llm_status():
    """Check LLM provider availability."""
    try:
        from trading.llm.engine import check_llm_availability
        return jsonify(check_llm_availability())
    except ImportError:
        return jsonify({"error": "LLM module not installed", "any_available": False})


@app.route("/api/journal/entries", methods=["GET"])
def api_journal_entries():
    """Get past journal entries from the database (trade-level journal)."""
    try:
        from trading.db.store import get_journal
        limit = request.args.get("limit", 30, type=int)
        entries = get_journal(limit=limit)
        # Parse market_context JSON if string
        for j in entries:
            if j.get("market_context") and isinstance(j["market_context"], str):
                try:
                    j["market_context"] = _json.loads(j["market_context"])
                except Exception:
                    pass
        return jsonify({"entries": entries})
    except Exception as exc:
        log.exception("Journal entries error")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/journal/daily", methods=["GET", "POST"])
def api_journal_daily():
    """Get stored daily journal entries, or POST to trigger generation."""
    if request.method == "POST":
        try:
            from trading.scheduler import run_daily_journal
            run_daily_journal()
            return jsonify({"status": "generated"})
        except Exception as exc:
            log.exception("On-demand journal generation failed")
            return jsonify({"error": str(exc)}), 500
    try:
        from trading.db.store import get_action_log
        limit = request.args.get("limit", 30, type=int)
        all_actions = get_action_log(limit=500, category="journal")
        journals = []
        for a in all_actions:
            data = a.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            if not isinstance(data, dict):
                data = {}
            if data.get("type") == "daily":
                journals.append({
                    "id": a["id"],
                    "timestamp": a["timestamp"],
                    "content": data.get("content", a.get("details", "")),
                    "type": "daily",
                })
            if len(journals) >= limit:
                break
        return jsonify({"journals": journals})
    except Exception as exc:
        log.exception("Daily journal fetch error")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/journal/weekly", methods=["GET"])
def api_journal_weekly():
    """Get stored weekly synthesis entries (auto-generated by scheduler)."""
    try:
        from trading.db.store import get_action_log
        limit = request.args.get("limit", 10, type=int)
        all_actions = get_action_log(limit=500, category="journal")
        weeklies = []
        for a in all_actions:
            data = a.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            if not isinstance(data, dict):
                data = {}
            if data.get("type") == "weekly":
                weeklies.append({
                    "id": a["id"],
                    "timestamp": a["timestamp"],
                    "content": data.get("content", a.get("details", "")),
                    "type": "weekly",
                })
            if len(weeklies) >= limit:
                break
        return jsonify({"reviews": weeklies})
    except Exception as exc:
        log.exception("Weekly journal fetch error")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/llm/journal", methods=["POST"])
def api_llm_journal():
    """Generate a daily trading journal entry using LLM."""
    try:
        from trading.llm.engine import generate_journal
        from trading.db.store import get_trades, get_daily_pnl, get_signals, get_action_log
        from trading.execution.router import get_positions_from_aster

        trades = get_trades(limit=15)
        positions = get_positions_from_aster()[:10]
        pnl = get_daily_pnl(limit=7)
        signals = get_signals(limit=10)
        actions = get_action_log(limit=10)

        journal = generate_journal(trades, positions, pnl, signals, actions)
        return jsonify({"journal": journal})
    except Exception as exc:
        log.exception("Journal generation error")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/llm/explain-trade/<int:trade_id>", methods=["GET"])
def api_llm_explain_trade(trade_id):
    """Explain a specific trade using LLM."""
    try:
        from trading.llm.engine import explain_trade
        from trading.db.store import get_trades, get_signals, get_action_log

        trades = get_trades(limit=200)
        trade = next((t for t in trades if t.get("id") == trade_id), None)
        if not trade:
            return jsonify({"error": f"Trade {trade_id} not found"}), 404

        # Gather context: signals and actions around the trade time
        context = {
            "recent_signals": get_signals(limit=10),
            "recent_actions": get_action_log(limit=10),
        }
        explanation = explain_trade(trade, context)
        return jsonify({"explanation": explanation, "trade": trade})
    except Exception as exc:
        log.exception("Trade explanation error")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/llm/analyze", methods=["GET"])
def api_llm_analyze():
    """Analyze overall trading performance using LLM."""
    try:
        from trading.llm.engine import analyze_performance
        from trading.db.store import get_daily_pnl
        from trading.monitor.web import _get_strategies

        pnl = get_daily_pnl(limit=30)
        strategies = _get_strategies()
        analysis = analyze_performance(pnl, strategies)
        return jsonify({"analysis": analysis})
    except Exception as exc:
        log.exception("Performance analysis error")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/llm/weekly-review", methods=["GET"])
def api_llm_weekly_review():
    """Generate a weekly performance synthesis using LLM."""
    try:
        from trading.llm.engine import generate_weekly_synthesis
        from trading.intelligence.action_narrator import get_recent_lessons
        from trading.db.store import get_daily_pnl, get_action_log

        journal_entries = get_action_log(limit=50)
        lessons = get_recent_lessons(limit=15)
        strategies = _get_strategies()
        pnl_hist = get_daily_pnl(limit=7)

        synthesis = generate_weekly_synthesis(journal_entries, lessons, strategies, pnl_hist)
        return jsonify({"review": synthesis})
    except Exception as exc:
        log.exception("Weekly review error")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/mode", methods=["GET", "POST"])
def api_mode():
    """Get or switch trading mode (paper/live). Persists in DB for all workers."""
    import trading.config as cfg
    from trading.db.store import get_setting, set_setting, log_action

    # Always read from DB (shared across gunicorn workers + daemon)
    current = get_setting("trading_mode", cfg.TRADING_MODE)

    if request.method == "GET":
        return jsonify({"mode": current})

    data = request.get_json(silent=True) or {}
    new_mode = data.get("mode", "").lower()
    if new_mode not in ("paper", "live"):
        return jsonify({"error": "mode must be 'paper' or 'live'"}), 400

    if new_mode == "live" and not data.get("confirm"):
        return jsonify({
            "error": "Switching to LIVE mode requires confirmation",
            "confirm_required": True,
            "message": "This will execute real trades with real money. "
                       "Send again with {\"mode\": \"live\", \"confirm\": true} to proceed.",
        }), 400

    old_mode = current
    set_setting("trading_mode", new_mode)
    cfg.TRADING_MODE = new_mode

    log_action("system", "mode_switch", details=f"Switched from {old_mode} to {new_mode}")
    return jsonify({"mode": new_mode, "previous": old_mode})


@app.route("/api/profile", methods=["GET", "POST"])
def api_profile():
    """Get or switch trading profile (mentality). Persists in DB for all workers.

    Profiles control leverage, risk tolerance, position sizing, and confluence thresholds.
    Available: conservative, moderate, aggressive, greedy
    """
    import trading.config as cfg
    from trading.db.store import get_setting, set_setting, log_action

    VALID_PROFILES = ("conservative", "moderate", "aggressive", "greedy")

    # Profile descriptions for the UI
    PROFILE_INFO = {
        "conservative": {
            "label": "Conservative",
            "description": "Minimal leverage (1x), tight stops, maximum cash reserve. Capital preservation first.",
            "icon": "shield",
            "color": "#00d4aa",
            "leverage_default": 1,
            "risk_style": "Low risk, low reward",
        },
        "moderate": {
            "label": "Moderate",
            "description": "Selective leverage (up to 3x on proven strategies), balanced risk/reward.",
            "icon": "scale",
            "color": "#4a9eff",
            "leverage_default": 1,
            "risk_style": "Balanced risk/reward",
        },
        "aggressive": {
            "label": "Aggressive",
            "description": "High leverage (2-5x), lower cash reserve (5%), maximum capital deployment.",
            "icon": "flame",
            "color": "#ffa500",
            "leverage_default": 2,
            "risk_style": "High risk, high reward",
        },
        "greedy": {
            "label": "Greedy",
            "description": "Maximum leverage (3-10x), near-zero cash reserve. Extremely high risk.",
            "icon": "zap",
            "color": "#ff4466",
            "leverage_default": 3,
            "risk_style": "Maximum risk, maximum potential",
        },
    }

    current = get_setting("trading_profile", cfg.LEVERAGE_PROFILE)

    if request.method == "GET":
        return jsonify({
            "profile": current,
            "profiles": PROFILE_INFO,
        })

    data = request.get_json(silent=True) or {}
    new_profile = data.get("profile", "").lower()
    if new_profile not in VALID_PROFILES:
        return jsonify({"error": f"profile must be one of: {', '.join(VALID_PROFILES)}"}), 400

    if new_profile == "greedy" and not data.get("confirm"):
        return jsonify({
            "error": "Switching to GREEDY requires confirmation",
            "confirm_required": True,
            "message": "Greedy mode uses up to 10x leverage and minimal cash reserves. "
                       "This can lead to rapid liquidation. Confirm to proceed.",
        }), 400

    old_profile = current
    set_setting("trading_profile", new_profile)
    cfg.LEVERAGE_PROFILE = new_profile

    log_action("system", "profile_switch",
               details=f"Trading mentality switched from {old_profile} to {new_profile}")
    return jsonify({"profile": new_profile, "previous": old_profile})


@app.route("/api/health")
def api_health():
    """Health check endpoint for monitoring and alerting."""
    now = datetime.now(timezone.utc)
    degraded = False

    # --- Daemon last cycle ---
    daemon_last_cycle = None
    daemon_last_cycle_age_minutes = None
    try:
        with get_read_db() as conn:
            row = conn.execute(
                "SELECT timestamp FROM action_log "
                "WHERE action = 'cycle_complete' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if row:
                daemon_last_cycle = row["timestamp"]
                last_dt = datetime.fromisoformat(daemon_last_cycle)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age = (now - last_dt).total_seconds() / 60.0
                daemon_last_cycle_age_minutes = round(age, 1)
                if age > 360:  # 6 hours
                    degraded = True
            else:
                degraded = True
    except Exception:
        log.exception("Health check: failed to query last cycle")
        degraded = True

    # --- Consecutive recent errors ---
    consecutive_errors = 0
    try:
        with get_read_db() as conn:
            rows = conn.execute(
                "SELECT action FROM action_log "
                "ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()
            for r in rows:
                if r["action"] == "error":
                    consecutive_errors += 1
                else:
                    break
    except Exception:
        log.exception("Health check: failed to query error count")

    # --- Uptime ---
    uptime_seconds = round(time.monotonic() - _START_TIME, 1)

    # --- DB size ---
    db_size_mb = None
    try:
        db_size_mb = round(Path(DB_PATH).stat().st_size / (1024 * 1024), 2)
    except Exception:
        log.exception("Health check: failed to stat database file")

    # --- Positions count ---
    positions_count = 0
    try:
        positions_count = len(get_positions_from_alpaca())
    except Exception:
        log.exception("Health check: failed to count positions")

    status_code = 503 if degraded else 200
    payload = {
        "status": "degraded" if degraded else "ok",
        "daemon_last_cycle": daemon_last_cycle,
        "daemon_last_cycle_age_minutes": daemon_last_cycle_age_minutes,
        "consecutive_errors": consecutive_errors,
        "uptime": uptime_seconds,
        "db_size_mb": db_size_mb,
        "positions_count": positions_count,
    }
    return jsonify(payload), status_code


@app.route("/api/volume/<symbol>")
def api_volume(symbol):
    """Volume analysis for a symbol (e.g. BTCUSDT)."""
    try:
        from trading.risk.volume_gate import full_volume_analysis
        analysis = full_volume_analysis(symbol.upper())
        if analysis is None:
            return jsonify({"error": "No volume data available", "symbol": symbol}), 404
        return jsonify({
            "symbol": symbol.upper(),
            "ratio": analysis.ratio,
            "trend": analysis.trend,
            "spread_bps": analysis.spread_bps,
            "recent_quote_volume": analysis.recent_quote_volume,
            "sizing_multiplier": analysis.sizing_multiplier,
            "thresholds": {
                "min_volume_ratio": RISK.get("min_volume_ratio", 0.30),
                "volume_exit_ratio": RISK.get("volume_exit_ratio", 0.20),
                "max_spread_bps": RISK.get("max_spread_bps", 50),
            },
        })
    except Exception as e:
        log.exception("Volume analysis failed for %s", symbol)
        return jsonify({"error": str(e)}), 500


@app.route("/api/volume")
def api_volume_all():
    """Volume analysis for all open positions."""
    try:
        from trading.risk.volume_gate import full_volume_analysis
        from trading.data.aster import alpaca_to_aster
        positions = get_positions_from_alpaca()
        results = []
        for pos in positions:
            sym = pos.get("symbol", "")
            aster_sym = alpaca_to_aster(sym)
            if not aster_sym:
                continue
            analysis = full_volume_analysis(aster_sym)
            if analysis:
                results.append({
                    "symbol": sym,
                    "aster_symbol": aster_sym,
                    "ratio": analysis.ratio,
                    "trend": analysis.trend,
                    "spread_bps": analysis.spread_bps,
                    "recent_quote_volume": analysis.recent_quote_volume,
                    "sizing_multiplier": analysis.sizing_multiplier,
                })
        return jsonify(results)
    except Exception as e:
        log.exception("Volume analysis failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/funnel")
def api_funnel():
    """Signal-to-execution funnel data from recent cycles."""
    try:
        with get_read_db() as conn:
            rows = conn.execute(
                "SELECT timestamp, data FROM action_log "
                "WHERE category='funnel' ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()
        import json as _json
        funnels = []
        for r in rows:
            entry = {"timestamp": r["timestamp"]}
            if r["data"]:
                try:
                    entry.update(_json.loads(r["data"]))
                except Exception:
                    pass
            funnels.append(entry)
        return jsonify(funnels)
    except Exception as e:
        log.exception("Funnel API failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/time-pnl")
def api_time_pnl():
    """P&L breakdown by hour of day and day of week."""
    try:
        from trading.learning.time_analysis import get_hourly_pnl, get_daily_pnl_by_dow
        return jsonify({
            "hourly": get_hourly_pnl(),
            "daily": get_daily_pnl_by_dow(),
        })
    except Exception as e:
        log.exception("Time P&L API failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/correlation-matrix")
def api_correlation_matrix():
    """Strategy signal correlation matrix and alerts."""
    try:
        from trading.learning.correlation_matrix import (
            compute_correlation_matrix, check_correlation_alerts,
        )
        matrix = compute_correlation_matrix()
        alerts = check_correlation_alerts(matrix)
        return jsonify({"matrix": matrix, "alerts": alerts})
    except Exception as e:
        log.exception("Correlation matrix API failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/fill-analysis")
def api_fill_analysis():
    """Fill quality and slippage data."""
    try:
        with get_read_db() as conn:
            rows = conn.execute(
                "SELECT * FROM fill_quality ORDER BY timestamp DESC LIMIT 100"
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        log.warning(f"Fill analysis API: {e}")
        return jsonify([])


@app.route("/api/attribution")
def api_attribution():
    """Strategy P&L attribution data."""
    try:
        from trading.learning.attribution import get_attribution_summary
        data = get_attribution_summary(days=30)
        return jsonify(data)
    except Exception as e:
        log.warning(f"Attribution API: {e}")
        return jsonify([])


@app.route("/api/margin")
def api_margin():
    """Margin health for all leveraged positions."""
    try:
        positions = get_positions_from_alpaca()
        result = []
        for pos in positions:
            leverage = pos.get("leverage", 1)
            if leverage <= 1:
                continue
            entry = pos.get("entry_price") or pos.get("avg_cost", 0)
            mark = pos.get("current_price", entry)
            side = pos.get("side", "LONG").upper()
            liq_dist = 1.0 / leverage
            if side == "LONG":
                liq_price = entry * (1 - liq_dist)
                margin_dist = (mark - liq_price) / mark if mark else 1.0
            else:
                liq_price = entry * (1 + liq_dist)
                margin_dist = (liq_price - mark) / mark if mark else 1.0
            status = "safe"
            if margin_dist < 0.05:
                status = "critical"
            elif margin_dist < 0.10:
                status = "danger"
            elif margin_dist < 0.20:
                status = "warning"
            result.append({
                "symbol": pos.get("symbol", "?"),
                "leverage": leverage,
                "entry_price": round(entry, 2),
                "mark_price": round(mark, 2),
                "liq_price": round(liq_price, 2),
                "margin_distance": round(margin_dist, 4),
                "status": status,
            })
        return jsonify(result)
    except Exception as e:
        log.warning(f"Margin API: {e}")
        return jsonify([])


@app.route("/api/leverage")
def api_leverage():
    """Leverage per position and aggregate."""
    try:
        account = _safe_account()
        portfolio_value = account.get("portfolio_value", 0)
        from trading.execution.router import get_positions_from_aster
        positions = get_positions_from_aster()
        total_notional = 0
        pos_data = []
        for pos in positions:
            lev = pos.get("leverage", 1) or 1
            mv = abs(pos.get("market_value", 0))
            notional = mv  # market_value already reflects full position notional
            total_notional += notional
            pos_data.append({
                "symbol": pos.get("symbol", "?"),
                "leverage": lev,
                "notional": round(mv, 2),
                "effective_exposure": round(notional, 2),
            })
        return jsonify({
            "total_notional": round(total_notional, 2),
            "portfolio_value": round(portfolio_value, 2),
            "aggregate_leverage": round(total_notional / portfolio_value, 2) if portfolio_value else 0,
            "positions": pos_data,
        })
    except Exception as e:
        log.warning(f"Leverage API: {e}")
        return jsonify({"total_notional": 0, "portfolio_value": 0, "aggregate_leverage": 0, "positions": []})


@app.route("/api/sectors")
def api_sectors():
    """Sector exposure breakdown."""
    try:
        positions = get_positions_from_alpaca()
        account = _safe_account()
        portfolio_value = account.get("portfolio_value", 0)

        sector_map = {
            "l1": {"BTC", "ETH"},
            "alts": {"SOL", "AVAX", "DOT", "LINK", "ADA", "NEAR", "SUI", "APT",
                     "MATIC", "POL", "ATOM", "XRP", "TRX", "TON", "HBAR", "SEI",
                     "TIA", "INJ", "OP", "ARB", "FTM", "ALGO", "XLM", "VET",
                     "ICP", "FIL", "SAND", "MANA", "LTC", "BCH", "ETC", "XMR",
                     "ZEC", "DASH", "THETA", "EOS", "NEO", "QTUM", "ZIL", "ONE",
                     "EGLD", "FLOW", "ROSE", "CELO", "KDA", "KAVA", "CKB", "IOTA"},
            "meme": {"DOGE", "SHIB", "PEPE", "BONK", "WIF", "TRUMP", "FLOKI",
                     "NEIRO", "TURBO", "BRETT", "MOG", "POPCAT", "MEW", "MYRO"},
            "defi": {"UNI", "AAVE", "MKR", "CRV", "COMP", "SNX", "SUSHI",
                     "YFI", "LDO", "RPL", "GMX", "DYDX", "JUP", "RAY",
                     "PENDLE", "ENA", "ONDO", "1INCH", "BAL", "CAKE"},
            "ai": {"FET", "RENDER", "TAO", "WLD", "RNDR", "AGIX", "OCEAN",
                   "AKT", "AR", "GRT", "BITTENSOR"},
            "gaming": {"AXS", "IMX", "GALA", "ENJ", "ILV", "PRIME", "PIXEL",
                       "PORTAL", "RONIN", "BEAM"},
            "stocks": {"AAPL", "TSLA", "NVDA", "GOOGL", "MSFT", "AMZN", "META",
                       "AMD", "INTC", "NFLX", "CRM", "ORCL", "UBER", "COIN",
                       "SQ", "PYPL", "DIS", "BA", "JPM", "GS", "V", "MA"},
            "commodities": {"GOLD", "SILVER", "OIL", "XAU", "XAG"},
        }
        sector_limits = {"l1": 0.50, "alts": 0.20, "defi": 0.25, "meme": 0.10,
                        "ai": 0.15, "gaming": 0.10, "stocks": 0.20, "commodities": 0.15}

        sectors: dict = {}
        for pos in positions:
            sym = pos.get("symbol", "").upper().replace("USDT", "").replace("USD", "").replace("/", "")
            found_sector = "other"
            for sector, symbols in sector_map.items():
                if sym in symbols:
                    found_sector = sector
                    break
            if found_sector not in sectors:
                sectors[found_sector] = {"exposure": 0, "positions": []}
            sectors[found_sector]["exposure"] += abs(pos.get("market_value", 0))
            sectors[found_sector]["positions"].append(pos.get("symbol", "?"))

        result = []
        for sector, data in sectors.items():
            exp_pct = data["exposure"] / portfolio_value if portfolio_value else 0
            result.append({
                "sector": sector,
                "exposure": round(data["exposure"], 2),
                "exposure_pct": round(exp_pct * 100, 1),
                "limit_pct": round(sector_limits.get(sector, 100) * 100, 0),
                "positions": data["positions"],
            })
        return jsonify(sorted(result, key=lambda x: x["exposure"], reverse=True))
    except Exception as e:
        log.warning(f"Sectors API: {e}")
        return jsonify([])


def start_dashboard(host="127.0.0.1", port=None, debug=False):
    """Start the web dashboard server."""
    if port is None:
        port = int(os.environ.get("PORT", 5050))
    init_db()
    app.run(host=host, port=port, debug=debug, use_reloader=False)
