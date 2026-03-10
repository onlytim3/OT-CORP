"""Flask web dashboard — autonomous monitoring interface."""

import json
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, jsonify

from trading.config import TRADING_MODE, RISK, PROJECT_ROOT
from trading.db.store import (
    init_db, get_trades, get_positions, get_daily_pnl,
    get_signals, get_action_log, get_action_log_summary,
    get_journal, get_reviews,
)

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_account():
    """Get account info without crashing the dashboard."""
    try:
        if TRADING_MODE == "paper":
            from trading.execution.paper import get_paper_account
            return get_paper_account()
        else:
            from trading.execution.alpaca_client import get_account
            return get_account()
    except Exception as e:
        return {
            "portfolio_value": 0,
            "cash": 0,
            "buying_power": 0,
            "equity": 0,
            "status": f"Error: {e}",
            "paper": TRADING_MODE == "paper",
        }


def _safe_positions():
    try:
        if TRADING_MODE == "paper":
            from trading.execution.paper import get_paper_positions
            return get_paper_positions()
        else:
            from trading.execution.alpaca_client import get_positions_from_alpaca
            return get_positions_from_alpaca()
    except Exception:
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
        mode="PAPER" if account.get("paper") or TRADING_MODE == "paper" else "LIVE",
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


def start_dashboard(host="0.0.0.0", port=None, debug=False):
    """Start the web dashboard server."""
    import os
    if port is None:
        port = int(os.environ.get("PORT", 5050))
    init_db()
    app.run(host=host, port=port, debug=debug, use_reloader=False)
