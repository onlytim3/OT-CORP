"""Flask web dashboard — autonomous monitoring interface."""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, jsonify

from trading.config import TRADING_MODE, RISK, PROJECT_ROOT, DB_PATH
from trading.db.store import (
    init_db, get_db, get_trades, get_positions, get_daily_pnl,
    get_signals, get_action_log, get_action_log_summary,
    get_journal, get_reviews,
)
from trading.execution.alpaca_client import get_account, get_positions_from_alpaca

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
