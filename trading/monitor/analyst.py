"""AI Analyst for post-trade critique and weekly tear sheets.

Connects to the database to examine recent trades, sends them to the
configured LLM engine (Groq/Gemini), and saves a Markdown Tear Sheet.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any

from trading.db.store import get_conn
from trading.llm.engine import ask_llm
from trading.config import KNOWLEDGE_DIR, JOURNALS_DIR

log = logging.getLogger(__name__)

REVIEWS_DIR = KNOWLEDGE_DIR / "reviews"

def get_recent_trades(days: int = 7) -> List[Dict[str, Any]]:
    """Fetch closed trades from the past N days."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    try:
        with get_conn() as conn:
            conn.row_factory = lambda cursor, row: {
                col[0]: row[idx] for idx, col in enumerate(cursor.description)
            }
            c = conn.cursor()
            c.execute("""
                SELECT id, symbol, side, qty, price, total, leverage, strategy,
                       pnl, pnl_pct, timestamp, closed_at, entry_reasoning
                FROM trades
                WHERE closed_at IS NOT NULL
                  AND closed_at >= ?
                ORDER BY closed_at DESC
            """, (cutoff,))
            return c.fetchall()
    except Exception as e:
        log.error("Failed to fetch recent trades: %s", e)
        return []

def generate_tear_sheet(days: int = 7) -> str:
    """Generate a weekly post-trade AI critique."""
    trades = get_recent_trades(days)
    
    if not trades:
        return "No closed trades in the specified period to analyze."

    # Compute quick stats
    total_trades = len(trades)
    winning_trades = [t for t in trades if t.get('pnl', 0) >= 0]
    losing_trades = [t for t in trades if t.get('pnl', 0) < 0]
    
    total_pnl = sum([t.get('pnl', 0) for t in trades])
    win_rate = len(winning_trades) / total_trades * 100 if total_trades > 0 else 0
    
    # Strip heavy fields to save context window, keep it dense
    dense_trades = []
    for t in trades[:30]: # Max 30 to fit into context securely
        dense_trades.append({
            "symbol": t["symbol"],
            "side": t["side"],
            "leverage": t["leverage"],
            "pnl": round(t["pnl"] or 0, 2),
            "pnl_pct": round(t["pnl_pct"] or 0, 2),
            "strategy": t["strategy"],
            "entry_reasoning": t.get("entry_reasoning", "")[:200] # truncate
        })
    
    prompt = f"""You are OT-CORP's elite quantitative analyst.
Generate a highly analytical 'Weekly Tear Sheet' for the trader based on these recent trades.
The timeframe is the last {days} days.

STATISTICS:
Total Trades: {total_trades}
Win Rate: {win_rate:.1f}%
Net P&L: ${total_pnl:.2f}
Wins: {len(winning_trades)}, Losses: {len(losing_trades)}

TRADE LOG (Top 30 recent):
{json.dumps(dense_trades, indent=2)}

REQUIREMENTS:
1. Provide a succinct overview of performance.
2. Identify strategic behavioral patterns (e.g. over-leveraging on meme coins, cutting winners early).
3. Call out highest-performing vs worst-performing strategies based on the logs.
4. Provide 3 actionable, strict directives to adjust algorithms or trader behavior for next week.
Use GitHub-style Markdown. Highlight key metrics in bold. 
Do not be overly polite. Be clinical, ruthless, and precise like a Wall Street risk manager.
"""
    
    try:
        report_md = ask_llm("You are OT-CORP's elite quantitative analyst.", prompt, call_type="journal")
    except Exception as e:
        log.error("LLM failed to generate tear sheet: %s", e)
        return f"Error generating report: {str(e)}"
    
    # Save report
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    report_id = datetime.utcnow().strftime('report_%Y%m%d_%H%M%S')
    report_path = REVIEWS_DIR / f"{report_id}.md"
    
    with open(report_path, "w") as f:
        f.write(report_md)
        
    return report_md

def get_all_reports() -> List[Dict[str, str]]:
    """List all generated tear sheets."""
    if not REVIEWS_DIR.exists():
        return []
        
    reports = []
    for f in REVIEWS_DIR.glob("report_*.md"):
        try:
            # Parse timestamp from filename: report_YYYYMMDD_HHMMSS.md
            parts = f.stem.split('_')
            if len(parts) == 3:
                ts_str = f"{parts[1]}_{parts[2]}"
                dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            else:
                dt = datetime.fromtimestamp(f.stat().st_mtime)
                
            with open(f, "r") as file:
                content = file.read()
                
            reports.append({
                "id": f.stem,
                "timestamp": dt.isoformat() + "Z",
                "preview": content[:200] + "...",
                "content": content
            })
        except Exception as e:
            log.error("Failed to read report %s: %s", f, e)
            
    # Sort newest first
    return sorted(reports, key=lambda x: x["timestamp"], reverse=True)
