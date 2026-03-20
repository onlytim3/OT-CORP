"""In-memory TTL cache for API data — prevents redundant API calls across strategies.

Cache entries expire based on TTL. At cycle start, only expired entries are pruned
rather than wiping everything — this lets strategies within the same cycle share
data (e.g., 22 strategies all needing BTC OHLCV won't hit the API 22 times).
"""

import logging
import time
from functools import wraps

_cache: dict[str, tuple[float, any]] = {}
DEFAULT_TTL = 300  # 5 minutes

log = logging.getLogger(__name__)


def cached(ttl: int = DEFAULT_TTL):
    """Decorator: cache function results by arguments for ttl seconds."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            key = f"{func.__module__}.{func.__name__}:{args}:{sorted(kwargs.items())}"
            now = time.time()
            if key in _cache:
                cached_time, cached_result = _cache[key]
                if now - cached_time < ttl:
                    return cached_result
            result = func(*args, **kwargs)
            _cache[key] = (now, result)
            return result
        return wrapper
    return decorator


def clear_cache():
    """Prune expired entries from the cache.

    Called at the start of each trading cycle. Instead of wiping everything,
    only removes entries whose TTL has passed. This means data fetched in the
    current cycle (within TTL) is shared across strategies — preventing
    redundant API calls when 22 strategies all need the same OHLCV data.
    """
    now = time.time()
    expired = [k for k, (ts, _) in _cache.items() if now - ts >= DEFAULT_TTL]
    for k in expired:
        del _cache[k]
    remaining = len(_cache)
    if expired:
        log.debug("Cache pruned: %d expired, %d retained", len(expired), remaining)


def force_clear_cache():
    """Force-clear all cached data. Use only when you need a completely fresh state."""
    _cache.clear()


def cache_stats() -> dict:
    """Return cache size and age info for debugging."""
    now = time.time()
    return {
        "entries": len(_cache),
        "keys": list(_cache.keys())[:20],  # Limit for readability
        "ages_seconds": {k: round(now - v[0], 1) for k, v in _cache.items()},
    }
