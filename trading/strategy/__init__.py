"""Strategy package — auto-discovering strategy registry."""

from trading.strategy.registry import get_enabled_strategies, get_strategy, list_registered

__all__ = ["get_enabled_strategies", "get_strategy", "list_registered"]
