"""Structured logging configuration for the trading system.

Call setup_logging() once at startup (e.g., in scheduler.start_daemon()).
All modules then use:
    import logging
    log = logging.getLogger(__name__)
"""

import logging
import sys
from pathlib import Path

from trading.config import PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "trading.log"


def setup_logging(level: str = "INFO", console: bool = True, file: bool = True):
    """Configure structured logging for the entire trading package.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR).
        console: Whether to log to stderr.
        file: Whether to log to LOG_FILE.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("trading")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Avoid duplicate handlers on re-init
    root.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-7s] %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    if file:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(
            str(LOG_FILE),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Quiet noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("alpaca").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("schedule").setLevel(logging.WARNING)

    root.info("Logging initialized — level=%s, console=%s, file=%s", level, console, file)
