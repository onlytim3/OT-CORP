"""Abstract base class for trading strategies."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Signal:
    """A trading signal produced by a strategy."""
    strategy: str       # Strategy name
    symbol: str         # Trading symbol (e.g., 'BTC/USD', 'GLD')
    action: str         # 'buy', 'sell', or 'hold'
    strength: float     # 0.0 to 1.0 confidence
    reason: str         # Human-readable rationale
    data: dict = None   # Raw signal data for logging

    @property
    def is_actionable(self):
        return self.action in ("buy", "sell")


class Strategy(ABC):
    """Base class for all trading strategies."""

    name: str = "base"

    @abstractmethod
    def generate_signals(self) -> list[Signal]:
        """Analyze market data and return a list of trading signals.

        Each signal contains the symbol, action (buy/sell/hold),
        confidence strength, and rationale.
        """
        ...

    @abstractmethod
    def get_market_context(self) -> dict:
        """Return current market context for journal entries.

        Should include relevant metrics the strategy uses for decisions.
        """
        ...

    def __repr__(self):
        return f"<Strategy: {self.name}>"
