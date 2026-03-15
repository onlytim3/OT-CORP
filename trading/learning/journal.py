"""Trade journaling — records rationale, market context, outcomes, and lessons."""

import json
from datetime import datetime, timezone
from pathlib import Path

from trading.config import JOURNALS_DIR
from trading.db.store import insert_journal, update_journal_outcome, get_journal
from trading.strategy.base import Signal


def create_journal_entry(trade_id: int, signal: Signal, market_context: dict,
                         narration: str = "") -> int:
    """Create a journal entry when a trade is executed.

    Args:
        trade_id: The database ID of the trade
        signal: The signal that triggered the trade
        market_context: Current market conditions from the strategy
        narration: Human-readable pre-trade narration explaining the trade

    Returns the journal entry ID.
    """
    # Combine narration with signal reason for a complete rationale
    if narration:
        rationale = f"{narration}\n\n---\nSignal reason: {signal.reason}"
    else:
        rationale = signal.reason
    tags = ",".join(filter(None, [signal.strategy, signal.symbol.replace("/", "-")]))

    insert_journal(
        trade_id=trade_id,
        rationale=rationale,
        market_context=market_context,
        tags=tags,
    )
    return trade_id


def record_outcome(trade_id: int, pnl: float, exit_price: float, entry_price: float):
    """Record the outcome of a trade in the journal."""
    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0
    if pnl > 0:
        outcome = f"WIN: +${pnl:.2f} ({pnl_pct:+.1f}%)"
        lesson = f"Entry at ${entry_price:.2f}, exit at ${exit_price:.2f} — profitable trade"
    else:
        outcome = f"LOSS: -${abs(pnl):.2f} ({pnl_pct:+.1f}%)"
        lesson = f"Entry at ${entry_price:.2f}, exit at ${exit_price:.2f} — review entry conditions"

    update_journal_outcome(trade_id, outcome, pnl, lesson)


def export_journal_markdown(limit: int = 20) -> str:
    """Export recent journal entries as markdown.

    Also saves to JOURNALS_DIR for persistent record.
    """
    entries = get_journal(limit=limit)
    if not entries:
        return "# Trade Journal\n\nNo entries yet.\n"

    lines = ["# Trade Journal\n"]
    for entry in entries:
        lines.append(f"## {entry.get('timestamp', 'Unknown date')}")
        lines.append(f"- **Symbol**: {entry.get('symbol', 'N/A')}")
        lines.append(f"- **Side**: {entry.get('side', 'N/A')}")
        lines.append(f"- **Strategy**: {entry.get('strategy', 'N/A')}")
        lines.append(f"- **Rationale**: {entry.get('rationale', 'N/A')}")

        if entry.get("market_context"):
            try:
                ctx = json.loads(entry["market_context"]) if isinstance(entry["market_context"], str) else entry["market_context"]
                lines.append(f"- **Market Context**: {json.dumps(ctx, indent=2)}")
            except (json.JSONDecodeError, TypeError):
                pass

        if entry.get("outcome"):
            lines.append(f"- **Outcome**: {entry['outcome']}")
            lines.append(f"- **P&L**: ${entry.get('pnl', 0):.2f}")
            lines.append(f"- **Lesson**: {entry.get('lesson', 'N/A')}")

        lines.append(f"- **Tags**: {entry.get('tags', '')}")
        lines.append("")

    content = "\n".join(lines)

    # Save to file
    JOURNALS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = JOURNALS_DIR / f"journal-{today}.md"
    filepath.write_text(content)

    return content
