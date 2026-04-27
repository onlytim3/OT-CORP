"""Funding Rate Term Structure Strategy — momentum and shape analysis of funding rate history.

Unlike the simpler funding_arb strategy (which fades extreme current rates),
this strategy analyzes the *dynamics* of funding rate history to detect
positioning shifts before they manifest in price.

Signal logic:
- Compute short-term avg (3 periods) vs long-term avg (12 periods) of funding rates
- Detect "funding momentum": short-term crossing above/below long-term
- Compute z-score of current funding rate vs its full history
- Overleveraged longs (short avg >> long avg, z > 1.5) -> SELL
- Overleveraged shorts (short avg << long avg, z < -1.5) -> BUY
- Cross-asset divergence: compare BTC funding dynamics vs altcoin for relative signals

Data source: Bybit API (perpetual futures, public endpoints).
"""

import logging
import statistics
from typing import Any

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Target coins and symbol mappings
# ---------------------------------------------------------------------------

TERM_STRUCTURE_COINS = [
    "bitcoin", "ethereum", "solana", "avalanche-2", "chainlink", "litecoin",
]

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

SHORT_WINDOW = 3          # Periods for short-term funding average
LONG_WINDOW = 12          # Periods for long-term funding average
MOMENTUM_THRESHOLD = 0.0003   # Min spread (short - long) to be significant
Z_THRESHOLD = 1.5         # Z-score threshold for extreme funding


# ---------------------------------------------------------------------------
# Data helpers — lazy imports with graceful degradation
# ---------------------------------------------------------------------------

def _fetch_funding_history(symbol: str) -> list[float]:
    """Fetch historical funding rate series from Bybit."""
    try:
        from trading.data.bybit import get_funding_rate_history
        return get_funding_rate_history(symbol)
    except ImportError:
        log.warning("trading.data.bybit not available — funding_term_structure disabled")
        return []
    except Exception as e:
        log.error("Failed to fetch funding history for %s: %s", symbol, e)
        return []


def _fetch_current_funding_rates() -> dict[str, float]:
    """Fetch current funding rates keyed by coin_id."""
    try:
        from trading.data.bybit import get_funding_rates
        return get_funding_rates()
    except ImportError:
        return {}
    except Exception as e:
        log.error("Failed to fetch current funding rates: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _moving_avg(values: list[float], window: int) -> float | None:
    """Average of the last `window` values. Returns None if insufficient data."""
    if len(values) < window:
        return None
    return statistics.mean(values[-window:])


def _z_score(values: list[float]) -> float:
    """Z-score of the most recent value relative to the full series."""
    if len(values) < 5:
        return 0.0
    mean = statistics.mean(values)
    stdev = statistics.stdev(values)
    if stdev < 1e-10:
        return 0.0
    return (values[-1] - mean) / stdev


# ---------------------------------------------------------------------------
# Signal scoring
# ---------------------------------------------------------------------------

def _score_funding_momentum(
    short_avg: float,
    long_avg: float,
    z: float,
    current_rate: float,
) -> tuple[str, float, str]:
    """Determine action, strength, and reason from term structure metrics.

    Returns (action, strength, reason).
    """
    momentum = short_avg - long_avg

    # Not enough spread to be meaningful
    if abs(momentum) < MOMENTUM_THRESHOLD:
        return "hold", 0.0, (
            f"funding momentum flat (spread {momentum:.6f}), "
            f"z={z:.2f}, current={current_rate:.6f}"
        )

    # Short-term funding rising sharply above long-term
    if momentum > MOMENTUM_THRESHOLD and z > Z_THRESHOLD:
        # Overleveraged longs — contrarian sell
        base_strength = min(0.3 + abs(z - Z_THRESHOLD) * 0.15, 0.90)
        reason = (
            f"overleveraged longs: short_avg {short_avg:.6f} > long_avg {long_avg:.6f}, "
            f"z={z:.2f}, rate={current_rate:.6f}"
        )
        return "sell", round(base_strength, 2), reason

    # Short-term funding dropping sharply below long-term
    if momentum < -MOMENTUM_THRESHOLD and z < -Z_THRESHOLD:
        # Overleveraged shorts — contrarian buy
        base_strength = min(0.3 + abs(z + Z_THRESHOLD) * 0.15, 0.90)
        reason = (
            f"overleveraged shorts: short_avg {short_avg:.6f} < long_avg {long_avg:.6f}, "
            f"z={z:.2f}, rate={current_rate:.6f}"
        )
        return "buy", round(base_strength, 2), reason

    # Momentum present but z-score not extreme enough
    direction = "rising" if momentum > 0 else "falling"
    return "hold", 0.0, (
        f"funding {direction} (spread {momentum:.6f}) but z={z:.2f} "
        f"below threshold {Z_THRESHOLD}"
    )


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

@register
class FundingTermStructureStrategy(Strategy):
    """Analyzes funding rate term structure momentum to detect positioning shifts."""

    name = "funding_term_structure"

    def __init__(self):
        self._last_context: dict[str, Any] = {}

    def generate_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        context_data: dict[str, Any] = {}

        try:
            from trading.config import BYBIT_SYMBOLS
        except ImportError:
            log.error("Cannot import symbol config — funding_term_structure disabled")
            return signals

        current_rates = _fetch_current_funding_rates()

        # Collect per-coin term structure analysis for cross-asset comparison
        coin_analyses: dict[str, dict] = {}

        for coin_id in TERM_STRUCTURE_COINS:
            bybit_symbol = BYBIT_SYMBOLS.get(coin_id)
            trade_symbol = BYBIT_SYMBOLS.get(coin_id)
            if not bybit_symbol or not trade_symbol:
                continue

            history = _fetch_funding_history(bybit_symbol)
            if len(history) < LONG_WINDOW:
                log.debug(
                    "funding_term_structure: insufficient history for %s (%d periods)",
                    coin_id, len(history),
                )
                continue

            short_avg = _moving_avg(history, SHORT_WINDOW)
            long_avg = _moving_avg(history, LONG_WINDOW)
            if short_avg is None or long_avg is None:
                continue

            z = _z_score(history)
            current_rate = current_rates.get(coin_id, history[-1])
            momentum = short_avg - long_avg

            coin_analyses[coin_id] = {
                "short_avg": short_avg,
                "long_avg": long_avg,
                "momentum": momentum,
                "z_score": z,
                "current_rate": current_rate,
                "bybit_symbol": bybit_symbol,
                "trade_symbol": trade_symbol,
            }

        # BTC analysis for cross-asset divergence
        btc_analysis = coin_analyses.get("bitcoin")

        for coin_id, analysis in coin_analyses.items():
            try:
                action, strength, reason = _score_funding_momentum(
                    analysis["short_avg"],
                    analysis["long_avg"],
                    analysis["z_score"],
                    analysis["current_rate"],
                )

                # Cross-asset divergence: if BTC funding is moving opposite to
                # this altcoin, the altcoin signal is amplified.
                if coin_id != "bitcoin" and btc_analysis and action != "hold":
                    btc_momentum = btc_analysis["momentum"]
                    alt_momentum = analysis["momentum"]

                    # BTC funding falling while altcoin funding rising -> stronger sell
                    if action == "sell" and btc_momentum < -MOMENTUM_THRESHOLD:
                        strength = min(strength + 0.10, 0.95)
                        reason += " | BTC funding divergence (falling)"

                    # BTC funding rising while altcoin funding falling -> stronger buy
                    if action == "buy" and btc_momentum > MOMENTUM_THRESHOLD:
                        strength = min(strength + 0.10, 0.95)
                        reason += " | BTC funding divergence (rising)"

                signal_data = {
                    "coin": coin_id,
                    "short_avg": round(analysis["short_avg"], 8),
                    "long_avg": round(analysis["long_avg"], 8),
                    "momentum": round(analysis["momentum"], 8),
                    "z_score": round(analysis["z_score"], 4),
                    "current_rate": round(analysis["current_rate"], 8),
                }
                context_data[coin_id] = signal_data

                signals.append(Signal(
                    strategy=self.name,
                    symbol=analysis["trade_symbol"],
                    action=action,
                    strength=strength,
                    reason=f"{coin_id} {reason}",
                    data=signal_data,
                ))

            except Exception as e:
                log.error("funding_term_structure error for %s: %s", coin_id, e)
                signals.append(Signal(
                    strategy=self.name,
                    symbol=analysis["trade_symbol"],
                    action="hold",
                    strength=0.0,
                    reason=f"{coin_id} funding_term_structure error: {e}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
