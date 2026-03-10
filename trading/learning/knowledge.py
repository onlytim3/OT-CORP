"""Knowledge base — ingest trading books, articles, and strategies."""

from pathlib import Path

from trading.config import STRATEGIES_DIR
from trading.db.store import insert_knowledge, search_knowledge, get_knowledge


def ingest_file(filepath: str, category: str = "general") -> dict:
    """Ingest a text/markdown file into the knowledge base.

    Args:
        filepath: Path to the file
        category: momentum, mean_reversion, risk, macro, crypto, commodities, general

    Returns dict with title and id.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    content = path.read_text(encoding="utf-8", errors="replace")
    title = path.stem.replace("-", " ").replace("_", " ").title()

    # Extract key rules (lines starting with - or * or numbered)
    lines = content.split("\n")
    rules = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("- ", "* ", "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
            if len(stripped) > 10:  # Skip very short bullets
                rules.append(stripped)

    key_rules = "\n".join(rules[:50])  # Cap at 50 rules

    insert_knowledge(
        title=title,
        source=str(path.absolute()),
        category=category,
        content=content,
        key_rules=key_rules,
    )

    # Also save a copy to strategies dir
    STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
    dest = STRATEGIES_DIR / path.name
    if not dest.exists():
        dest.write_text(content)

    return {"title": title, "source": str(path), "category": category, "rules_extracted": len(rules)}


def ingest_text(title: str, content: str, category: str = "general", source: str = "manual") -> dict:
    """Ingest raw text into the knowledge base."""
    lines = content.split("\n")
    rules = [l.strip() for l in lines if l.strip().startswith(("- ", "* ")) and len(l.strip()) > 10]
    key_rules = "\n".join(rules[:50])

    insert_knowledge(
        title=title,
        source=source,
        category=category,
        content=content,
        key_rules=key_rules,
    )
    return {"title": title, "source": source, "category": category, "rules_extracted": len(rules)}


def query(search_term: str, limit: int = 5) -> list[dict]:
    """Search the knowledge base using full-text search."""
    return search_knowledge(search_term, limit=limit)


def list_knowledge(limit: int = 20) -> list[dict]:
    """List all ingested knowledge documents."""
    return get_knowledge(limit=limit)
