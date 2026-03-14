"""SQLite database for trades, positions, P&L, journal, knowledge, and reviews."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from trading.config import DB_PATH


def _ensure_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_db():
    _ensure_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL,
                total REAL,
                strategy TEXT,
                status TEXT DEFAULT 'pending',
                alpaca_order_id TEXT,
                closed_at TEXT,
                close_price REAL,
                pnl REAL,
                stop_loss_price REAL,
                take_profit_price REAL,
                trailing_stop_activate REAL,
                risk_reward_ratio REAL
            );

            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                qty REAL NOT NULL,
                avg_cost REAL NOT NULL,
                current_price REAL,
                unrealized_pnl REAL,
                strategy TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                date TEXT PRIMARY KEY,
                portfolio_value REAL NOT NULL,
                cash REAL NOT NULL,
                positions_value REAL NOT NULL,
                daily_return REAL,
                cumulative_return REAL
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                signal TEXT NOT NULL,
                strength REAL,
                data JSON
            );

            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER REFERENCES trades(id),
                timestamp TEXT NOT NULL,
                rationale TEXT,
                market_context JSON,
                outcome TEXT,
                pnl REAL,
                lesson TEXT,
                tags TEXT
            );

            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source TEXT,
                category TEXT,
                content TEXT NOT NULL,
                key_rules TEXT,
                ingested_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS param_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL,
                param_name TEXT NOT NULL,
                old_value REAL,
                new_value REAL,
                reason TEXT,
                approved INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                total_trades INTEGER,
                win_rate REAL,
                total_pnl REAL,
                sharpe_ratio REAL,
                max_drawdown REAL,
                best_strategy TEXT,
                worst_strategy TEXT,
                summary TEXT,
                file_path TEXT
            );

            CREATE TABLE IF NOT EXISTS action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                category TEXT NOT NULL,
                action TEXT NOT NULL,
                symbol TEXT,
                details TEXT,
                result TEXT,
                data JSON
            );

            CREATE TABLE IF NOT EXISTS watermarks (
                symbol TEXT PRIMARY KEY,
                high_price REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deferred_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                strength REAL,
                strategy TEXT,
                reason TEXT,
                expires_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                title, content, key_rules, category,
                content='knowledge',
                content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
                INSERT INTO knowledge_fts(rowid, title, content, key_rules, category)
                VALUES (new.id, new.title, new.content, new.key_rules, new.category);
            END;

            CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
                INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, key_rules, category)
                VALUES ('delete', old.id, old.title, old.content, old.key_rules, old.category);
            END;
        """)

        # --- Migrate existing tables (add SL/TP columns) ---
        for col, coltype in [
            ("stop_loss_price", "REAL"),
            ("take_profit_price", "REAL"),
            ("trailing_stop_activate", "REAL"),
            ("risk_reward_ratio", "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # --- Performance Indexes ---
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);

            CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
            CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy);
            CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

            CREATE INDEX IF NOT EXISTS idx_journal_trade_id ON journal(trade_id);
            CREATE INDEX IF NOT EXISTS idx_journal_timestamp ON journal(timestamp);

            CREATE INDEX IF NOT EXISTS idx_action_log_timestamp ON action_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_action_log_category ON action_log(category);
            CREATE INDEX IF NOT EXISTS idx_action_log_cat_ts ON action_log(category, timestamp);

            CREATE INDEX IF NOT EXISTS idx_daily_pnl_date ON daily_pnl(date);

            CREATE INDEX IF NOT EXISTS idx_param_history_strategy ON param_history(strategy);
            CREATE INDEX IF NOT EXISTS idx_param_history_approved ON param_history(approved);

            CREATE INDEX IF NOT EXISTS idx_reviews_period ON reviews(period);
            CREATE INDEX IF NOT EXISTS idx_reviews_end_date ON reviews(end_date);
        """)


def _now():
    return datetime.now(timezone.utc).isoformat()


# --- Trade Operations ---

def insert_trade(symbol, side, qty, price, total, strategy, status="pending", alpaca_order_id=None,
                  stop_loss_price=None, take_profit_price=None, trailing_stop_activate=None,
                  risk_reward_ratio=None):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, total, strategy, status, alpaca_order_id, "
            "stop_loss_price, take_profit_price, trailing_stop_activate, risk_reward_ratio) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), symbol, side, qty, price, total, strategy, status, alpaca_order_id,
             stop_loss_price, take_profit_price, trailing_stop_activate, risk_reward_ratio),
        )
        return cur.lastrowid


def update_trade_status(trade_id, status, alpaca_order_id=None):
    with get_db() as conn:
        if alpaca_order_id:
            conn.execute(
                "UPDATE trades SET status=?, alpaca_order_id=? WHERE id=?",
                (status, alpaca_order_id, trade_id),
            )
        else:
            conn.execute("UPDATE trades SET status=? WHERE id=?", (status, trade_id))


def close_trade(trade_id, close_price, pnl):
    with get_db() as conn:
        conn.execute(
            "UPDATE trades SET closed_at=?, close_price=?, pnl=?, status='closed' WHERE id=?",
            (_now(), close_price, pnl, trade_id),
        )


def get_trades(limit=50, strategy=None):
    with get_db() as conn:
        if strategy:
            rows = conn.execute(
                "SELECT * FROM trades WHERE strategy=? ORDER BY timestamp DESC LIMIT ?",
                (strategy, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_open_trades():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status IN ('filled', 'pending') AND closed_at IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]


# --- Position Operations ---

def upsert_position(symbol, qty, avg_cost, current_price, strategy):
    unrealized_pnl = (current_price - avg_cost) * qty if current_price and avg_cost else 0
    with get_db() as conn:
        conn.execute(
            "INSERT INTO positions (symbol, qty, avg_cost, current_price, unrealized_pnl, strategy, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET qty=?, avg_cost=?, current_price=?, unrealized_pnl=?, strategy=?, updated_at=?",
            (symbol, qty, avg_cost, current_price, unrealized_pnl, strategy, _now(),
             qty, avg_cost, current_price, unrealized_pnl, strategy, _now()),
        )


def remove_position(symbol):
    with get_db() as conn:
        conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))


def get_positions():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        return [dict(r) for r in rows]


# --- Daily P&L ---

def record_daily_pnl(portfolio_value, cash, positions_value, daily_return, cumulative_return):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_pnl (date, portfolio_value, cash, positions_value, daily_return, cumulative_return) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (today, portfolio_value, cash, positions_value, daily_return, cumulative_return),
        )


def get_daily_pnl(limit=30):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Signal Operations ---

def insert_signal(strategy, symbol, signal, strength, data=None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO signals (timestamp, strategy, symbol, signal, strength, data) VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), strategy, symbol, signal, strength, json.dumps(data) if data else None),
        )


def get_signals(limit=20):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Journal Operations ---

def insert_journal(trade_id, rationale, market_context, tags=""):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO journal (trade_id, timestamp, rationale, market_context, tags) VALUES (?, ?, ?, ?, ?)",
            (trade_id, _now(), rationale, json.dumps(market_context) if market_context else None, tags),
        )


def update_journal_outcome(trade_id, outcome, pnl, lesson):
    with get_db() as conn:
        conn.execute(
            "UPDATE journal SET outcome=?, pnl=?, lesson=? WHERE trade_id=?",
            (outcome, pnl, lesson, trade_id),
        )


def get_journal(limit=20):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT j.*, t.symbol, t.side, t.qty, t.price, t.strategy "
            "FROM journal j LEFT JOIN trades t ON j.trade_id = t.id "
            "ORDER BY j.timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Knowledge Operations ---

def insert_knowledge(title, source, category, content, key_rules=""):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO knowledge (title, source, category, content, key_rules, ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
            (title, source, category, content, key_rules, _now()),
        )


def search_knowledge(query, limit=10):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT k.* FROM knowledge k "
            "JOIN knowledge_fts f ON k.id = f.rowid "
            "WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_knowledge(limit=20):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, source, category, key_rules, ingested_at FROM knowledge ORDER BY ingested_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Param History ---

def insert_param_change(strategy, param_name, old_value, new_value, reason):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO param_history (timestamp, strategy, param_name, old_value, new_value, reason) VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), strategy, param_name, old_value, new_value, reason),
        )


def get_pending_adaptations():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM param_history WHERE approved=0 ORDER BY timestamp DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def approve_adaptation(param_id):
    with get_db() as conn:
        conn.execute("UPDATE param_history SET approved=1 WHERE id=?", (param_id,))


# --- Review Operations ---

def insert_review(period, start_date, end_date, total_trades, win_rate, total_pnl,
                  sharpe_ratio, max_drawdown, best_strategy, worst_strategy, summary, file_path):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO reviews (period, start_date, end_date, total_trades, win_rate, total_pnl, "
            "sharpe_ratio, max_drawdown, best_strategy, worst_strategy, summary, file_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (period, start_date, end_date, total_trades, win_rate, total_pnl,
             sharpe_ratio, max_drawdown, best_strategy, worst_strategy, summary, file_path),
        )


def get_reviews(limit=10):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM reviews ORDER BY end_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Action Log ---

def log_action(category, action, symbol=None, details=None, result=None, data=None):
    """Log a system action for the dashboard.

    Categories: strategy_run, signal, trade, risk_block, stop_loss,
                error, review, scheduler, system
    """
    with get_db() as conn:
        conn.execute(
            "INSERT INTO action_log (timestamp, category, action, symbol, details, result, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), category, action, symbol, details, result,
             json.dumps(data) if data else None),
        )


def get_action_log(limit=50, category=None):
    with get_db() as conn:
        if category:
            rows = conn.execute(
                "SELECT * FROM action_log WHERE category=? ORDER BY timestamp DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM action_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_action_log_summary():
    """Get summary stats for dashboard header."""
    with get_db() as conn:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        total_trades_today = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE category='trade' AND timestamp LIKE ?",
            (f"{today}%",)
        ).fetchone()[0]

        total_signals_today = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE category='signal' AND timestamp LIKE ?",
            (f"{today}%",)
        ).fetchone()[0]

        risk_blocks_today = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE category='risk_block' AND timestamp LIKE ?",
            (f"{today}%",)
        ).fetchone()[0]

        errors_today = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE category='error' AND timestamp LIKE ?",
            (f"{today}%",)
        ).fetchone()[0]

        last_run = conn.execute(
            "SELECT timestamp FROM action_log WHERE category='strategy_run' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()

        return {
            "trades_today": total_trades_today,
            "signals_today": total_signals_today,
            "risk_blocks_today": risk_blocks_today,
            "errors_today": errors_today,
            "last_run": last_run[0] if last_run else None,
        }


# --- Watermarks (ProfitTracker persistence) ---

def save_watermark(symbol: str, high_price: float):
    """Persist a high watermark for trailing stop tracking."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO watermarks (symbol, high_price, updated_at) VALUES (?, ?, ?)",
            (symbol, high_price, _now()),
        )


def load_watermarks() -> dict:
    """Load all persisted high watermarks. Returns {symbol: high_price}."""
    with get_db() as conn:
        rows = conn.execute("SELECT symbol, high_price FROM watermarks").fetchall()
        return {r["symbol"]: r["high_price"] for r in rows}


def delete_watermark(symbol: str):
    """Remove watermark when position is fully closed."""
    with get_db() as conn:
        conn.execute("DELETE FROM watermarks WHERE symbol=?", (symbol,))


# --- Deferred Signals (ETF signal queue) ---

def save_deferred_signal(symbol: str, action: str, strength: float,
                         strategy: str, reason: str, expires_at: str):
    """Queue an ETF signal for execution when market opens."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO deferred_signals (timestamp, symbol, action, strength, strategy, reason, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), symbol, action, strength, strategy, reason, expires_at),
        )


def get_deferred_signals() -> list:
    """Get all pending deferred signals that haven't expired."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM deferred_signals WHERE expires_at > ? ORDER BY timestamp",
            (_now(),),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_deferred_signal(signal_id: int):
    """Remove a deferred signal after execution."""
    with get_db() as conn:
        conn.execute("DELETE FROM deferred_signals WHERE id=?", (signal_id,))
