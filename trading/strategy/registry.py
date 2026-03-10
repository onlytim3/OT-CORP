"""Strategy registry — auto-discovers and manages all strategy classes."""

import importlib
import pkgutil

from trading.strategy.base import Strategy

_registry: dict[str, type[Strategy]] = {}
_discovered = False


def register(cls: type[Strategy]) -> type[Strategy]:
    """Decorator to register a strategy class."""
    _registry[cls.name] = cls
    return cls


def get_enabled_strategies() -> list[Strategy]:
    """Return instantiated list of all enabled strategies."""
    from trading.config import STRATEGY_ENABLED
    _auto_discover()
    strategies = []
    for name, cls in sorted(_registry.items()):
        if STRATEGY_ENABLED.get(name, False):
            strategies.append(cls())
    return strategies


def get_strategy(name: str) -> Strategy | None:
    """Get a single strategy instance by name."""
    _auto_discover()
    cls = _registry.get(name)
    return cls() if cls else None


def list_registered() -> list[str]:
    """List all registered strategy names."""
    _auto_discover()
    return sorted(_registry.keys())


def _auto_discover():
    """Import all modules in trading.strategy package to trigger @register decorators."""
    global _discovered
    if _discovered:
        return
    _discovered = True
    import trading.strategy as strategy_package
    for _importer, modname, _ispkg in pkgutil.iter_modules(strategy_package.__path__):
        if modname not in ("base", "registry", "indicators", "__init__"):
            try:
                importlib.import_module(f"trading.strategy.{modname}")
            except Exception:
                pass  # Skip broken strategy modules silently
