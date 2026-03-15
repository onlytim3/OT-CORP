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

from trading.config import TRADING_MODE, RISK, PROJECT_ROOT, DB_PATH, DISPLAY_TIMEZONE
from trading.db.store import (
    init_db, get_db, get_trades, get_positions, get_daily_pnl,
    get_signals, get_action_log, get_action_log_summary,
    get_journal, get_reviews, get_setting,
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


def _add_position_ages(positions: list) -> list:
    """Add human-readable age to each position based on earliest trade."""
    if not positions:
        return positions
    try:
        now = datetime.now(timezone.utc)
        with get_db() as conn:
            for p in positions:
                sym = p["symbol"]
                sym_slash = sym[:3] + "/" + sym[3:] if "/" not in sym and len(sym) >= 6 else sym
                sym_flat = sym.replace("/", "")
                placeholders = ",".join("?" for _ in {sym, sym_slash, sym_flat})
                row = conn.execute(
                    f"SELECT MIN(timestamp) as opened FROM trades WHERE symbol IN ({placeholders}) AND side='buy' AND status != 'closed'",
                    list({sym, sym_slash, sym_flat}),
                ).fetchone()
                if row and row["opened"]:
                    opened = datetime.fromisoformat(row["opened"].replace("Z", "+00:00"))
                    if opened.tzinfo is None:
                        from datetime import timezone as _tz
                        opened = opened.replace(tzinfo=_tz.utc)
                    delta = now - opened
                    mins = int(delta.total_seconds() / 60)
                    if mins < 60:
                        p["age"] = f"{mins}m"
                    elif mins < 1440:
                        p["age"] = f"{mins // 60}h"
                    else:
                        p["age"] = f"{mins // 1440}d"
                else:
                    p["age"] = ""
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
    positions = _add_position_ages(sorted(_safe_positions(), key=lambda p: p.get("unrealized_pnl", 0) or 0, reverse=True))
    summary = get_action_log_summary()
    return jsonify({
        "account": account,
        "positions": positions,
        "summary": summary,
        "mode": get_setting("trading_mode", TRADING_MODE),
        "timestamp": datetime.now(timezone.utc).isoformat(),
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

    result = {"pending": [], "recent": [], "activity": [], "agent_stats": []}

    try:
        recs = get_recommendation_history(limit=100)
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
            if status == "pending":
                s["pending"] += 1
            elif status in ("applied", "accepted"):
                s["applied"] += 1
            elif status in ("rejected",):
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
    with get_db() as conn:
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

    with get_db() as conn:
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


def start_dashboard(host="127.0.0.1", port=None, debug=False):
    """Start the web dashboard server."""
    if port is None:
        port = int(os.environ.get("PORT", 5050))
    init_db()
    app.run(host=host, port=port, debug=debug, use_reloader=False)
