"""In-memory TTL cache for API data — prevents redundant API calls across strategies."""

import time
from functools import wraps

_cache: dict[str, tuple[float, any]] = {}
DEFAULT_TTL = 300  # 5 minutes


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
    """Clear all cached data. Called at the start of each trading cycle."""
    _cache.clear()


def cache_stats() -> dict:
    """Return cache size and age info for debugging."""
    now = time.time()
    return {
        "entries": len(_cache),
        "keys": list(_cache.keys())[:20],  # Limit for readability
        "ages_seconds": {k: round(now - v[0], 1) for k, v in _cache.items()},
    }
