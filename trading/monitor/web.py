"""Flask web dashboard — autonomous monitoring interface."""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, jsonify, request

from trading.config import TRADING_MODE, RISK, PROJECT_ROOT, DB_PATH
from trading.db.store import (
    init_db, get_db, get_trades, get_positions, get_daily_pnl,
    get_signals, get_action_log, get_action_log_summary,
    get_journal, get_reviews,
)
from trading.execution.router import get_account, get_positions_from_aster as get_positions_from_alpaca

log = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
)

_START_TIME = time.monotonic()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    account = _safe_account()
    positions = _safe_positions()
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

    return render_template(
        "dashboard.html",
        account=account,
        positions=positions,
        summary=summary,
        actions=actions,
        trades=trades,
        signals=signals,
        pnl_history=pnl_history,
        total_pnl=total_pnl,
        positions_value=positions_value,
        mode="PAPER" if TRADING_MODE == "paper" else "LIVE",
        risk=RISK,
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


@app.route("/api/status")
def api_status():
    """JSON endpoint for programmatic access / auto-refresh."""
    account = _safe_account()
    positions = _safe_positions()
    summary = get_action_log_summary()
    return jsonify({
        "account": account,
        "positions": positions,
        "summary": summary,
        "mode": TRADING_MODE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/actions")
def api_actions():
    actions = get_action_log(limit=50)
    return jsonify(actions)


@app.route("/api/trades")
def api_trades():
    trades = get_trades(limit=50)
    return jsonify(trades)


@app.route("/api/position/<symbol>")
def api_position_detail(symbol):
    """Detailed breakdown for a single position — strategies, reasoning, risk."""
    import json as _json
    from trading.config import RISK
    from trading.risk.manager import CORRELATION_GROUPS, LEVERAGE_FACTORS, compute_trade_targets
    from trading.risk.profit_manager import TAKE_PROFIT_PCT, TRAILING_STOP_ACTIVATE, TRAILING_STOP_PCT

    # Normalize: allow both BTCUSD and BTC/USD lookups
    symbol_slash = symbol[:3] + "/" + symbol[3:] if "/" not in symbol and len(symbol) >= 6 else symbol
    symbol_flat = symbol.replace("/", "")
    match_symbols = list({symbol, symbol_slash, symbol_flat})

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
    with get_db() as conn:
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

    with get_db() as conn:
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
            sym_variants = list({sym, sym.replace("/", ""), sym[:3] + "/" + sym[3:] if "/" not in sym and len(sym) >= 6 else sym})
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

    return jsonify({"trade": trade, "journal": journal, "signals": signals})


@app.route("/api/signal/<int:signal_id>")
def api_signal_detail(signal_id):
    """Full detail for a single signal."""
    import json as _json

    with get_db() as conn:
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
            sym_variants = list({sym, sym.replace("/", ""), sym[:3] + "/" + sym[3:] if "/" not in sym and len(sym) >= 6 else sym})
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

    with get_db() as conn:
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

    return jsonify({
        "action": action,
        "context_before": list(reversed(context_before)),
        "context_after": context_after,
    })


@app.route("/api/pnl/<date>")
def api_pnl_detail(date):
    """Full detail for a single day's P&L — all trades, signals, actions."""
    import json as _json

    with get_db() as conn:
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
    trades = get_trades(limit=500)
    for t in trades:
        name = t.get("strategy", "")
        if not name:
            continue
        if name not in strat_data:
            strat_data[name] = {"signals": 0, "buys": 0, "sells": 0, "holds": 0}
        strat_data[name].setdefault("trades", 0)
        strat_data[name]["trades"] = strat_data[name].get("trades", 0) + 1
        if t.get("pnl") is not None:
            strat_data[name].setdefault("total_pnl", 0)
            strat_data[name]["total_pnl"] = strat_data[name].get("total_pnl", 0) + (t["pnl"] or 0)
        if t.get("closed_at"):
            strat_data[name].setdefault("closed", 0)
            strat_data[name]["closed"] = strat_data[name].get("closed", 0) + 1
            if (t.get("pnl") or 0) > 0:
                strat_data[name].setdefault("wins", 0)
                strat_data[name]["wins"] = strat_data[name].get("wins", 0) + 1

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
        from trading.data.crypto import get_fear_greed_index
        fg = get_fear_greed_index()
        if fg and isinstance(fg, list) and fg:
            result["fear_greed"] = fg[0]
    except Exception:
        pass

    # Recent intelligence briefings from action log
    with get_db() as conn:
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
            regime_sigs.append(d)
        result["regime_signals"] = regime_sigs

    return jsonify(result)


@app.route("/api/agents")
def api_agents():
    """Autonomous agent recommendations and activity."""
    from trading.db.store import get_recommendation_history, get_pending_recommendations

    result = {"pending": [], "recent": [], "activity": []}

    try:
        recs = get_recommendation_history(limit=20)
        result["pending"] = [r for r in recs if r.get("status") == "pending"]
        result["recent"] = [r for r in recs if r.get("status") != "pending"][:10]
    except Exception:
        pass

    # Agent-related actions from action log
    with get_db() as conn:
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


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Chat assistant endpoint — answers questions about the trading system."""
    from trading.monitor.chat import handle_chat

    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "No message provided"}), 400

    try:
        answer = handle_chat(message)
        return jsonify({"answer": answer})
    except Exception as exc:
        log.exception("Chat handler error")
        return jsonify({"answer": f"Sorry, I encountered an error: {exc}"}), 200


@app.route("/api/health")
def api_health():
    """Health check endpoint for monitoring and alerting."""
    now = datetime.now(timezone.utc)
    degraded = False

    # --- Daemon last cycle ---
    daemon_last_cycle = None
    daemon_last_cycle_age_minutes = None
    try:
        with get_db() as conn:
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
        with get_db() as conn:
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


def start_dashboard(host="127.0.0.1", port=None, debug=False):
    """Start the web dashboard server."""
    if port is None:
        port = int(os.environ.get("PORT", 5050))
    init_db()
    app.run(host=host, port=port, debug=debug, use_reloader=False)
