"""SQLite database for trades, positions, P&L, journal, knowledge, and reviews."""

import json
import logging
import sqlite3
import threading
import time as _time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps

log = logging.getLogger(__name__)

from trading.config import DB_PATH


# ---------------------------------------------------------------------------
# Connection management — one persistent connection per thread per mode.
# Eliminates the open/PRAGMA/commit/close churn that caused lock pressure.
# ---------------------------------------------------------------------------
_local = threading.local()


def _get_connection(read_only: bool = False) -> sqlite3.Connection:
    """Return a persistent connection for the current thread.

    Read-only connections use SQLite URI mode=ro so they physically cannot
    acquire write locks — safe for gunicorn workers.
    """
    attr = "_ro_conn" if read_only else "_rw_conn"
    conn = getattr(_local, attr, None)

    # Check if the connection is still alive
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            setattr(_local, attr, None)

    _ensure_db()

    if read_only:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=60,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Read-only connections: only set read-safe PRAGMAs
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA foreign_keys=ON")
    else:
        conn = sqlite3.connect(str(DB_PATH), timeout=60,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")

    setattr(_local, attr, conn)
    return conn


def symbol_variants(sym: str) -> list[str]:
    """Generate all plausible symbol variants for matching.

    Handles arbitrary base lengths (e.g. BTC/USD, DYDX/USD, GOOG/USD)
    by stripping known quote suffixes rather than assuming a 3-char base.

    Returns a deduplicated list: [original, flat, slash-form].
    """
    if not sym:
        return []
    flat = sym.replace("/", "")
    # Try to recover the slash form from a flat symbol
    slash = sym  # default: keep as-is
    if "/" not in sym:
        for suffix in ("USDT", "USD"):
            if sym.upper().endswith(suffix):
                base = sym[: len(sym) - len(suffix)]
                quote = sym[len(sym) - len(suffix):]
                slash = f"{base}/{quote}"
                break
    return list({sym, flat, slash})


def normalize_symbol(sym: str) -> str:
    """Canonicalise a symbol to the slash form used by CRYPTO_SYMBOLS.

    e.g. ``"AVAXUSD"`` → ``"AVAX/USD"``, ``"BTCUSD"`` → ``"BTC/USD"``.
    Already-slashed symbols are returned unchanged.
    """
    if not sym or "/" in sym:
        return sym
    for suffix in ("USDT", "USD"):
        if sym.upper().endswith(suffix):
            base = sym[: len(sym) - len(suffix)]
            return f"{base}/{suffix}"
    return sym


def _ensure_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_db():
    """Writable DB connection (daemon process only)."""
    conn = _get_connection(read_only=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def get_read_db():
    """Read-only DB connection — physically cannot write (mode=ro).

    Use this in gunicorn workers / web dashboard to avoid cross-process
    SQLite write lock contention with the trading daemon.
    """
    conn = _get_connection(read_only=True)
    try:
        yield conn
    except Exception:
        raise


def _retry_on_locked(func):
    """Retry a DB operation up to 4 times with exponential backoff on database lock errors."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        last_err = None
        for attempt in range(4):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                last_err = e
                wait = (attempt + 1) * 0.5  # 0.5s, 1s, 1.5s, 2s
                log.warning("DB locked in %s (attempt %d/4), retrying in %.1fs: %s",
                            func.__name__, attempt + 1, wait, e)
                _time.sleep(wait)
        log.error("DB locked in %s after 4 attempts, giving up: %s", func.__name__, last_err)
        raise last_err  # type: ignore[misc]
    return wrapper


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

            CREATE TABLE IF NOT EXISTS agent_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                category TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                reasoning TEXT NOT NULL,
                data JSON,
                status TEXT DEFAULT 'pending',
                resolved_at TEXT,
                resolution TEXT,
                outcome TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS backtest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                days INTEGER,
                sharpe REAL,
                win_rate REAL,
                max_drawdown REAL,
                total_return REAL,
                total_trades INTEGER,
                verdict TEXT,
                data TEXT
            );

            CREATE TABLE IF NOT EXISTS volume_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                hour_of_day INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                volume_ratio REAL NOT NULL,
                quote_volume REAL,
                trade_count INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_vol_profile_symbol
                ON volume_profiles(symbol);
            CREATE INDEX IF NOT EXISTS idx_vol_profile_hour
                ON volume_profiles(hour_of_day);

            CREATE TABLE IF NOT EXISTS fill_quality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                mid_price REAL NOT NULL,
                fill_price REAL NOT NULL,
                slippage_bps REAL NOT NULL,
                notional REAL,
                volume_ratio REAL
            );

            CREATE TABLE IF NOT EXISTS strategy_attribution (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                trade_id INTEGER NOT NULL,
                strategy TEXT NOT NULL,
                attributed_pnl REAL NOT NULL,
                strength_weight REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL,
                schedule TEXT NOT NULL,
                next_run TEXT,
                created_at TEXT NOT NULL,
                active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS trade_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                analysis TEXT NOT NULL,
                market_snapshot TEXT,
                source TEXT DEFAULT 'llm'
            );
            CREATE INDEX IF NOT EXISTS idx_trade_analyses_trade_id
                ON trade_analyses(trade_id);

            CREATE TABLE IF NOT EXISTS action_narratives (
                action_id INTEGER PRIMARY KEY,
                narrative TEXT NOT NULL,
                interpretation TEXT,
                lessons TEXT,
                quality_score REAL,
                generated_at TEXT NOT NULL,
                model TEXT
            );

            CREATE TABLE IF NOT EXISTS counterfactual_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                strength REAL NOT NULL,
                strategy TEXT NOT NULL,
                block_reason TEXT NOT NULL,
                entry_price REAL,
                exit_price REAL,
                hypothetical_pnl_pct REAL,
                data TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_counterfactual_timestamp
                ON counterfactual_signals(timestamp);
            CREATE INDEX IF NOT EXISTS idx_counterfactual_symbol
                ON counterfactual_signals(symbol);

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
            ("leverage", "INTEGER"),
            ("entry_reasoning", "TEXT"),
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

            CREATE INDEX IF NOT EXISTS idx_backtest_strategy ON backtest_results(strategy);
            CREATE INDEX IF NOT EXISTS idx_backtest_timestamp ON backtest_results(timestamp);

            CREATE INDEX IF NOT EXISTS idx_fill_quality_symbol ON fill_quality(symbol);
            CREATE INDEX IF NOT EXISTS idx_fill_quality_timestamp ON fill_quality(timestamp);
        """)


def _now():
    return datetime.now(timezone.utc).isoformat()


# --- Settings (persistent key-value store) ---

def get_setting(key: str, default: str = None) -> str:
    """Get a persistent setting from the database."""
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


@_retry_on_locked
def set_setting(key: str, value: str):
    """Set a persistent setting in the database."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _now()),
        )


# --- Trade Operations ---

@_retry_on_locked
def insert_trade(symbol, side, qty, price, total, strategy, status="pending", alpaca_order_id=None,
                  stop_loss_price=None, take_profit_price=None, trailing_stop_activate=None,
                  risk_reward_ratio=None, leverage=None, entry_reasoning=None):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, total, strategy, status, alpaca_order_id, "
            "stop_loss_price, take_profit_price, trailing_stop_activate, risk_reward_ratio, leverage, entry_reasoning) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), symbol, side, qty, price, total, strategy, status, alpaca_order_id,
             stop_loss_price, take_profit_price, trailing_stop_activate, risk_reward_ratio,
             leverage, entry_reasoning),
        )
        return cur.lastrowid


@_retry_on_locked
def update_trade_status(trade_id, status, alpaca_order_id=None):
    with get_db() as conn:
        if alpaca_order_id:
            conn.execute(
                "UPDATE trades SET status=?, alpaca_order_id=? WHERE id=?",
                (status, alpaca_order_id, trade_id),
            )
        else:
            conn.execute("UPDATE trades SET status=? WHERE id=?", (status, trade_id))


@_retry_on_locked
def close_trade(trade_id, close_price, pnl):
    """Close a trade and record exit price and realized P&L.

    If P&L is zero/None but we have entry price and close price,
    re-derive P&L from the actual price difference to avoid misleading 0.0 P&L.
    """
    with get_db() as conn:
        # If pnl looks wrong (zero when it shouldn't be), try to compute from prices
        if (pnl is None or pnl == 0) and close_price and close_price > 0:
            try:
                row = conn.execute(
                    "SELECT price, qty, side FROM trades WHERE id = ?", (trade_id,)
                ).fetchone()
                if row and row["price"] and row["price"] > 0 and row["qty"] and row["qty"] > 0:
                    entry_price = row["price"]
                    qty = row["qty"]
                    side = (row["side"] or "").lower()
                    if side in ("buy", "long"):
                        pnl = round((close_price - entry_price) * qty, 2)
                    elif side in ("sell", "short"):
                        pnl = round((entry_price - close_price) * qty, 2)
            except Exception:
                pass

        conn.execute(
            "UPDATE trades SET closed_at=?, close_price=?, pnl=?, status='closed' WHERE id=?",
            (_now(), close_price, pnl, trade_id),
        )


@_retry_on_locked
def close_matching_entry_trades(symbol: str, exit_price: float, exit_qty: float, exit_side: str) -> int:
    """Immediately close open entry trades for *symbol* when an exit executes.

    For futures trading, entries can be either buy (long) or sell (short).
    The *exit_side* determines which entries to close:
      - exit_side='sell' closes open buys  (long exit)
      - exit_side='buy'  closes open sells (short cover / buy-to-cover)

    Uses FIFO ordering (oldest entries first) so trade pairing stays
    consistent with pair_trades().

    Returns the number of entry trades closed.
    """
    entry_side = "buy" if exit_side == "sell" else "sell"

    with get_db() as conn:
        # Normalise symbol variants (BTC/USD vs BTCUSD, DYDX/USD vs DYDXUSD)
        variants = symbol_variants(symbol)
        placeholders = ",".join("?" for _ in variants)

        rows = conn.execute(
            f"SELECT id, qty, price FROM trades "
            f"WHERE symbol IN ({placeholders}) AND side=? "
            f"AND status IN ('filled', 'pending') AND closed_at IS NULL "
            f"ORDER BY timestamp ASC",
            variants + [entry_side],
        ).fetchall()

        remaining = exit_qty
        closed = 0
        now = _now()

        for row in rows:
            if remaining <= 0:
                break
            entry_id = row["id"]
            entry_qty = row["qty"] or 0
            entry_price = row["price"] or 0

            if entry_qty <= 0 or entry_price <= 0:
                continue

            matched = min(entry_qty, remaining)
            if entry_side == "buy":
                pnl = (exit_price - entry_price) * matched
            else:
                # Short entry: profit when price drops
                pnl = (entry_price - exit_price) * matched

            if matched >= entry_qty:
                # Fully consumed
                conn.execute(
                    "UPDATE trades SET closed_at=?, close_price=?, pnl=?, status='closed' WHERE id=?",
                    (now, exit_price, pnl, entry_id),
                )
            else:
                # Partially consumed — reduce qty, don't close
                new_qty = entry_qty - matched
                conn.execute(
                    "UPDATE trades SET qty=?, total=? WHERE id=?",
                    (new_qty, new_qty * entry_price, entry_id),
                )

            remaining -= matched
            closed += 1

        return closed


def close_matching_buy_trades(symbol: str, sell_price: float, sell_qty: float) -> int:
    """Backward-compatible wrapper: close open buys when a sell executes."""
    return close_matching_entry_trades(symbol, sell_price, sell_qty, exit_side="sell")


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

@_retry_on_locked
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


@_retry_on_locked
def remove_position(symbol):
    with get_db() as conn:
        conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))


def get_positions():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        return [dict(r) for r in rows]


# --- Daily P&L ---

@_retry_on_locked
def record_daily_pnl(portfolio_value, cash, positions_value, daily_return, cumulative_return):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_pnl (date, portfolio_value, cash, positions_value, daily_return, cumulative_return) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (today, portfolio_value, cash, positions_value, daily_return, cumulative_return),
        )
    log.info("record_daily_pnl: saved %s pv=$%.2f", today, portfolio_value)


def get_daily_pnl(limit=30):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Signal Operations ---

@_retry_on_locked
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

@_retry_on_locked
def insert_journal(trade_id, rationale, market_context, tags=""):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO journal (trade_id, timestamp, rationale, market_context, tags) VALUES (?, ?, ?, ?, ?)",
            (trade_id, _now(), rationale, json.dumps(market_context) if market_context else None, tags),
        )


@_retry_on_locked
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

@_retry_on_locked
def insert_knowledge(title, source, category, content, key_rules=""):
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO knowledge (title, source, category, content, key_rules, ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
            (title, source, category, content, key_rules, _now()),
        )
        row_id = cursor.lastrowid
    # Mirror into vector store for semantic search (non-blocking)
    try:
        from trading.learning.vector_store import insert_knowledge_embedding
        insert_knowledge_embedding(row_id, title, content, category)
    except Exception:
        pass


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a user message for safe use in an SQLite FTS5 MATCH clause.

    FTS5 treats characters like ?, !, *, -, (, ), ", ^ as operators.
    We strip punctuation, split into tokens, and wrap each in double-quotes
    so every word is treated as a literal phrase prefix search.
    Returns an empty string if no valid tokens remain.
    """
    import re
    # Remove all non-alphanumeric characters except whitespace
    cleaned = re.sub(r"[^\w\s]", " ", query, flags=re.UNICODE)
    tokens = [t.strip() for t in cleaned.split() if len(t.strip()) >= 2]
    if not tokens:
        return ""
    # Wrap each token in double-quotes for exact-token matching
    return " OR ".join(f'"{t}"' for t in tokens[:10])  # Cap at 10 tokens


def search_knowledge(query, limit=10):
    """Search the knowledge base using FTS5 full-text search.

    Sanitizes the query before passing to SQLite so raw user messages
    with punctuation (?, !, etc.) don't cause fts5 syntax errors.
    Falls back to a simple LIKE search if FTS returns nothing.
    """
    safe_query = _sanitize_fts_query(query)
    with get_db() as conn:
        if safe_query:
            try:
                rows = conn.execute(
                    "SELECT k.* FROM knowledge k "
                    "JOIN knowledge_fts f ON k.id = f.rowid "
                    "WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?",
                    (safe_query, limit),
                ).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except Exception:
                pass  # FTS failed — fall through to LIKE search
        # Fallback: broad LIKE search across title + content
        like = f"%{query[:50]}%"
        rows = conn.execute(
            "SELECT * FROM knowledge WHERE title LIKE ? OR content LIKE ? LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_knowledge(limit=20):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, source, category, key_rules, ingested_at FROM knowledge ORDER BY ingested_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Counterfactual Signals ---

import json as _json


@_retry_on_locked
def insert_counterfactual(
    symbol: str,
    action: str,
    strength: float,
    strategy: str,
    block_reason: str,
    entry_price: float | None = None,
    data: dict | None = None,
) -> int:
    """Record a signal that was blocked, for later counterfactual PnL analysis."""
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO counterfactual_signals "
            "(timestamp, symbol, action, strength, strategy, block_reason, entry_price, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(), symbol, action, strength, strategy, block_reason,
                entry_price, _json.dumps(data) if data else None,
            ),
        )
        return cursor.lastrowid


@_retry_on_locked
def fill_counterfactual_exits(current_prices: dict[str, float]) -> int:
    """Fill exit_price and hypothetical_pnl_pct for open counterfactuals older than 4h.

    Args:
        current_prices: {symbol: current_price} mapping.

    Returns number of records updated.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    updated = 0
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, symbol, action, entry_price FROM counterfactual_signals "
            "WHERE exit_price IS NULL AND entry_price IS NOT NULL AND timestamp <= ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            sym = row["symbol"]
            exit_p = current_prices.get(sym)
            if exit_p and row["entry_price"] and row["entry_price"] > 0:
                direction = 1 if row["action"] == "buy" else -1
                pnl_pct = direction * (exit_p - row["entry_price"]) / row["entry_price"]
                conn.execute(
                    "UPDATE counterfactual_signals SET exit_price=?, hypothetical_pnl_pct=? WHERE id=?",
                    (exit_p, round(pnl_pct, 6), row["id"]),
                )
                updated += 1
    return updated


def get_counterfactual_summary(days: int = 30) -> dict:
    """Summarise counterfactual outcomes: how often was blocking the right call?"""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT block_reason, action, hypothetical_pnl_pct "
            "FROM counterfactual_signals "
            "WHERE exit_price IS NOT NULL AND timestamp >= ?",
            (cutoff,),
        ).fetchall()

    if not rows:
        return {"total": 0, "by_reason": {}}

    by_reason: dict[str, dict] = {}
    for r in rows:
        key = r["block_reason"]
        if key not in by_reason:
            by_reason[key] = {"count": 0, "avg_pnl": 0.0, "positive": 0}
        by_reason[key]["count"] += 1
        pnl = r["hypothetical_pnl_pct"] or 0.0
        by_reason[key]["avg_pnl"] += pnl
        if pnl > 0:
            by_reason[key]["positive"] += 1

    for v in by_reason.values():
        if v["count"]:
            v["avg_pnl"] = round(v["avg_pnl"] / v["count"], 5)

    return {"total": len(rows), "by_reason": by_reason}


# --- Param History ---

@_retry_on_locked
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


@_retry_on_locked
def approve_adaptation(param_id):
    with get_db() as conn:
        conn.execute("UPDATE param_history SET approved=1 WHERE id=?", (param_id,))


# --- Review Operations ---

@_retry_on_locked
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

@_retry_on_locked
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

        open_positions = conn.execute(
            "SELECT COUNT(*) FROM positions"
        ).fetchone()[0]

        from trading.config import STRATEGY_ENABLED
        active_strategies = len([k for k, v in STRATEGY_ENABLED.items() if v])

        return {
            "trades_today": total_trades_today,
            "signals_today": total_signals_today,
            "risk_blocks_today": risk_blocks_today,
            "errors_today": errors_today,
            "last_run": last_run[0] if last_run else None,
            "active_strategies": active_strategies,
            "open_positions": open_positions,
        }


# --- Watermarks (ProfitTracker persistence) ---

@_retry_on_locked
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


@_retry_on_locked
def delete_watermark(symbol: str):
    """Remove watermark when position is fully closed."""
    with get_db() as conn:
        conn.execute("DELETE FROM watermarks WHERE symbol=?", (symbol,))


# --- Deferred Signals (ETF signal queue) ---

@_retry_on_locked
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


@_retry_on_locked
def clear_deferred_signal(signal_id: int):
    """Remove a deferred signal after execution."""
    with get_db() as conn:
        conn.execute("DELETE FROM deferred_signals WHERE id=?", (signal_id,))


# --- Agent Recommendations (agent-to-agent communication) ---

@_retry_on_locked
def insert_recommendation(from_agent: str, to_agent: str, category: str,
                          action: str, target: str, reasoning: str, data: dict = None) -> int:
    """Log an agent recommendation for another agent to act on.

    Categories: enable_strategy, disable_strategy, adjust_param,
                shift_allocation, add_strategy, remove_strategy,
                change_regime, performance_alert, research_finding
    """
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO agent_recommendations "
            "(timestamp, from_agent, to_agent, category, action, target, reasoning, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), from_agent, to_agent, category, action, target, reasoning,
             json.dumps(data) if data else None),
        )
        return cur.lastrowid


def get_pending_recommendations(to_agent: str = None, category: str = None) -> list:
    """Get pending recommendations, optionally filtered by recipient or category."""
    with get_db() as conn:
        query = "SELECT * FROM agent_recommendations WHERE status='pending'"
        params = []
        if to_agent:
            query += " AND to_agent=?"
            params.append(to_agent)
        if category:
            query += " AND category=?"
            params.append(category)
        query += " ORDER BY timestamp DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


@_retry_on_locked
def resolve_recommendation(rec_id: int, resolution: str, outcome: str = None):
    """Mark a recommendation as resolved (applied, rejected, deferred)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_recommendations SET status='resolved', resolved_at=?, "
            "resolution=?, outcome=? WHERE id=?",
            (_now(), resolution, outcome, rec_id),
        )


@_retry_on_locked
def update_recommendation_outcome(rec_id: int, outcome: str):
    """Update a recommendation's outcome after observing results (positive/negative)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_recommendations SET outcome=? WHERE id=?",
            (outcome, rec_id),
        )


def get_recently_applied_recommendations(hours: int = 48) -> list:
    """Get recommendations applied in the last N hours for outcome evaluation."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_recommendations WHERE status='resolved' "
            "AND resolution='applied' AND resolved_at >= ? AND outcome IS NULL "
            "ORDER BY resolved_at DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recommendation_history(limit: int = 50) -> list:
    """Get recent recommendation history for learning."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_recommendations ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Backtest Results ---

@_retry_on_locked
def insert_backtest_result(strategy: str, days: int, metrics: dict, verdict: str) -> int:
    """Record a backtest result for a strategy."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO backtest_results "
            "(strategy, timestamp, days, sharpe, win_rate, max_drawdown, total_return, total_trades, verdict, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (strategy, _now(), days,
             metrics.get("sharpe_ratio", 0),
             metrics.get("win_rate", 0),
             metrics.get("max_drawdown", 0),
             metrics.get("total_pnl", 0),
             metrics.get("total_trades", 0),
             verdict,
             json.dumps(metrics)),
        )
        return cur.lastrowid


def get_last_backtest(strategy: str) -> dict | None:
    """Get the most recent backtest result for a strategy."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM backtest_results WHERE strategy=? ORDER BY timestamp DESC LIMIT 1",
            (strategy,),
        ).fetchone()
        return dict(row) if row else None


def get_strategies_needing_backtest(cooldown_days: int = 7) -> list[str]:
    """Get strategies that haven't been backtested within the cooldown period."""
    from trading.config import STRATEGY_ENABLED
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
    with get_db() as conn:
        # Get strategies with recent backtests
        rows = conn.execute(
            "SELECT DISTINCT strategy FROM backtest_results WHERE timestamp > ?",
            (cutoff,),
        ).fetchall()
        recently_tested = {r["strategy"] for r in rows}

    # All known strategies minus recently tested ones
    return [s for s in STRATEGY_ENABLED if s not in recently_tested]


# ---------------------------------------------------------------------------
# Volume profiles — learning volume patterns by hour/day
# ---------------------------------------------------------------------------

@_retry_on_locked
def insert_volume_profile(
    symbol: str,
    hour_of_day: int,
    day_of_week: int,
    volume_ratio: float,
    quote_volume: float = 0,
    trade_count: int = 0,
) -> None:
    """Record a volume snapshot for learning."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO volume_profiles
               (symbol, timestamp, hour_of_day, day_of_week, volume_ratio, quote_volume, trade_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (symbol, datetime.now(timezone.utc).isoformat(),
             hour_of_day, day_of_week, volume_ratio, quote_volume, trade_count),
        )


def get_volume_profile(symbol: str, days: int = 30) -> list[dict]:
    """Get average volume ratio by hour-of-day for a symbol.

    Returns list of dicts: [{hour: 0, avg_ratio: 1.2, samples: 30}, ...]
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT hour_of_day as hour,
                      AVG(volume_ratio) as avg_ratio,
                      COUNT(*) as samples
               FROM volume_profiles
               WHERE symbol = ? AND timestamp > ?
               GROUP BY hour_of_day
               ORDER BY hour_of_day""",
            (symbol, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def get_volume_profile_by_day(symbol: str, days: int = 30) -> list[dict]:
    """Get average volume ratio by day-of-week for a symbol.

    Returns list of dicts: [{day: 0, avg_ratio: 1.1, samples: 4}, ...]
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT day_of_week as day,
                      AVG(volume_ratio) as avg_ratio,
                      COUNT(*) as samples
               FROM volume_profiles
               WHERE symbol = ? AND timestamp > ?
               GROUP BY day_of_week
               ORDER BY day_of_week""",
            (symbol, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


@_retry_on_locked
def cleanup_old_volume_profiles(keep_days: int = 90) -> int:
    """Delete volume profiles older than keep_days. Returns rows deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM volume_profiles WHERE timestamp < ?", (cutoff,)
        )
        return cursor.rowcount


# --- Trade Analyses ---

@_retry_on_locked
def insert_trade_analysis(trade_id: int, analysis: str, market_snapshot: dict = None,
                          source: str = "llm") -> int:
    """Insert a trade analysis entry."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO trade_analyses (trade_id, timestamp, analysis, market_snapshot, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (trade_id, _now(), analysis,
             json.dumps(market_snapshot) if market_snapshot else None, source),
        )
        return cur.lastrowid


def get_trade_analyses(trade_id: int, limit: int = 20) -> list:
    """Get analysis entries for a trade, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trade_analyses WHERE trade_id = ? ORDER BY timestamp DESC LIMIT ?",
            (trade_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Action Narratives ---

@_retry_on_locked
def insert_action_narrative(action_id: int, narrative: str, interpretation: str = None,
                            lessons: str = None, quality_score: float = None,
                            model: str = None) -> None:
    """Insert or update an action narrative."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO action_narratives "
            "(action_id, narrative, interpretation, lessons, quality_score, generated_at, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (action_id, narrative, interpretation, lessons, quality_score, _now(), model),
        )


def get_action_narrative(action_id: int) -> dict | None:
    """Get cached narrative for an action."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM action_narratives WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        return dict(row) if row else None


def get_recent_action_lessons(limit: int = 20) -> list:
    """Get recent lessons from action narratives for agent context."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT lessons FROM action_narratives "
            "WHERE lessons IS NOT NULL AND lessons != '[]' "
            "ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    all_lessons = []
    for row in rows:
        try:
            lessons = json.loads(row["lessons"])
            all_lessons.extend(lessons)
        except Exception:
            pass
    return list(dict.fromkeys(all_lessons))[:limit]
