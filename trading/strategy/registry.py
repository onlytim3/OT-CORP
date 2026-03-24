"""Strategy registry — auto-discovers and manages all strategy classes."""

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field

from trading.strategy.base import Strategy

log = logging.getLogger(__name__)

_registry: dict[str, type[Strategy]] = {}
_load_errors: dict[str, str] = {}
_discovered = False


@dataclass
class PreflightResult:
    """Result of strategy deployment pre-flight check."""
    passed: bool
    loaded: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


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
            except Exception as e:
                _load_errors[modname] = str(e)
                log.error("Failed to load strategy module %s: %s", modname, e)


def preflight_check() -> PreflightResult:
    """Validate all enabled strategies loaded successfully.

    Returns a PreflightResult indicating which strategies loaded, which are
    missing (no module file), and which failed to import.
    """
    from trading.config import STRATEGY_ENABLED
    _auto_discover()
    enabled = [name for name, on in STRATEGY_ENABLED.items() if on]
    loaded = [name for name in enabled if name in _registry]
    missing = [name for name in enabled if name not in _registry and name not in _load_errors]
    failed = {name: _load_errors[name] for name in enabled if name in _load_errors}
    passed = not missing and not failed
    return PreflightResult(passed=passed, loaded=loaded, missing=missing, failed=failed)
