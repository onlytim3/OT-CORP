"""Basis Z-Score Mean Reversion Strategy — trade mean-reversion of the futures
basis spread (mark price vs index price) using z-score extremes.

Signal logic:
- Compute basis spread (mark - index) / index for each symbol.
- Build a rolling history of basis values from hourly klines.
- Calculate z-score of current basis relative to its rolling history.
- Z-score > 2.0 (extreme contango): SELL — expect basis to compress.
- Z-score < -2.0 (extreme backwardation): BUY — expect basis to expand.
- Neutral zone (|z| < 0.5): no edge.

Strength scales linearly with abs(z-score) from z_entry to z_entry + 2,
capped at 1.0.

Data source: AsterDex API (perpetual futures public endpoints, no auth).
"""

import logging
import statistics
from typing import Any

import pandas as pd

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mappings
# ---------------------------------------------------------------------------

BASIS_COINS = ["bitcoin", "ethereum", "solana", "avalanche-2", "chainlink", "litecoin"]

ASTER_SYMBOL_MAP = {
    "bitcoin": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "solana": "SOLUSDT",
    "avalanche-2": "AVAXUSDT",
    "chainlink": "LINKUSDT",
    "litecoin": "LTCUSDT",
}

ALPACA_SYMBOL_MAP = {
    "bitcoin": "BTC/USD",
    "ethereum": "ETH/USD",
    "solana": "SOL/USD",
    "avalanche-2": "AVAX/USD",
    "chainlink": "LINK/USD",
    "litecoin": "LTC/USD",
}

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

Z_ENTRY = 2.0           # Enter when |z-score| exceeds this
Z_EXIT = 0.5            # Neutral zone — no edge below this
ROLLING_WINDOW = 48     # Hours of basis history for z-score calculation
MIN_BASIS_DATA = 20     # Minimum data points needed for a valid z-score


# ---------------------------------------------------------------------------
# Data helpers — lazy imports for graceful degradation
# ---------------------------------------------------------------------------

def _fetch_basis_spread(symbol: str) -> dict | None:
    """Fetch current basis spread for a single AsterDex symbol."""
    try:
        from trading.data.aster import get_basis_spread
        data = get_basis_spread(symbol=symbol)
        if not isinstance(data, dict) or data.get("indexPrice", 0) == 0:
            return None
        return data
    except ImportError:
        log.warning("trading.data.aster not available — basis_zscore degraded")
        return None
    except Exception as e:
        log.error("Failed to fetch basis spread for %s: %s", symbol, e)
        return None


def _fetch_basis_history(symbol: str) -> list[float]:
    """Build a historical basis series from hourly klines.

    Uses close prices as a proxy for mark price, combined with VWAP-like
    approximation. Returns list of basis_pct values (oldest first).
    """
    try:
        from trading.data.aster import get_aster_ohlcv
        df = get_aster_ohlcv(symbol, interval="1h", limit=ROLLING_WINDOW)
        if df.empty or len(df) < MIN_BASIS_DATA:
            return []

        # Use high-low midpoint vs close as a basis proxy:
        # close approximates mark, (high+low)/2 approximates index.
        # This captures intra-candle basis divergence dynamics.
        mid = (df["high"] + df["low"]) / 2.0
        close = df["close"]
        basis_series = ((close - mid) / mid * 100).dropna().tolist()
        return basis_series

    except ImportError:
        log.warning("trading.data.aster not available — no basis history")
        return []
    except Exception as e:
        log.error("Failed to fetch basis history for %s: %s", symbol, e)
        return []


# ---------------------------------------------------------------------------
# Z-score calculation
# ---------------------------------------------------------------------------

def _compute_zscore(values: list[float], current: float) -> float:
    """Compute z-score of current value relative to the historical series.

    Uses the indicators module if available, falls back to inline computation.
    """
    if len(values) < MIN_BASIS_DATA:
        return 0.0

    try:
        from trading.strategy.indicators import z_score as compute_zscore
        series = pd.Series(values + [current])
        z = compute_zscore(series, period=len(values))
        result = z.iloc[-1]
        if pd.isna(result):
            return 0.0
        return float(result)
    except (ImportError, Exception):
        pass

    # Inline fallback
    mean = statistics.mean(values)
    stdev = statistics.stdev(values)
    if stdev < 1e-10:
        return 0.0
    return (current - mean) / stdev


# ---------------------------------------------------------------------------
# Signal scoring
# ---------------------------------------------------------------------------

def _score_signal(z: float) -> tuple[str, float, str]:
    """Determine action, strength, and reason from basis z-score."""
    abs_z = abs(z)

    if abs_z < Z_EXIT:
        return "hold", 0.0, f"basis z-score {z:.2f} in neutral zone"

    if abs_z < Z_ENTRY:
        return "hold", 0.0, f"basis z-score {z:.2f} below entry threshold"

    # Scale strength: z_entry -> 0.3, z_entry+2 -> 1.0
    strength = min(0.3 + (abs_z - Z_ENTRY) * 0.35, 1.0)
    strength = round(strength, 2)

    if z > Z_ENTRY:
        action = "sell"
        reason = f"extreme contango (basis z={z:.2f}) — expect compression"
    else:
        action = "buy"
        reason = f"extreme backwardation (basis z={z:.2f}) — expect expansion"

    return action, strength, reason


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

@register
class BasisZScoreStrategy(Strategy):
    """Mean-reversion on perpetual futures basis spread using z-score extremes."""

    name = "basis_zscore"

    def __init__(self):
        self._last_context: dict[str, Any] = {}

    def generate_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        context_data: dict[str, Any] = {}

        for coin_id in BASIS_COINS:
            alpaca_symbol = ALPACA_SYMBOL_MAP.get(coin_id)
            aster_symbol = ASTER_SYMBOL_MAP.get(coin_id)
            if not alpaca_symbol or not aster_symbol:
                continue

            try:
                # Fetch current basis spread
                basis_data = _fetch_basis_spread(aster_symbol)
                if basis_data is None:
                    log.debug("basis_zscore: no basis data for %s, skipping", coin_id)
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="hold",
                        strength=0.0,
                        reason=f"{coin_id} no basis data available",
                    ))
                    continue

                current_basis_pct = float(basis_data.get("basis_pct", 0.0))
                mark_price = float(basis_data.get("markPrice", 0.0))
                index_price = float(basis_data.get("indexPrice", 0.0))
                funding_rate = float(basis_data.get("fundingRate", 0.0))

                # Fetch historical basis for z-score
                history = _fetch_basis_history(aster_symbol)
                if len(history) < MIN_BASIS_DATA:
                    log.debug(
                        "basis_zscore: insufficient history for %s (%d/%d), skipping",
                        coin_id, len(history), MIN_BASIS_DATA,
                    )
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="hold",
                        strength=0.0,
                        reason=f"{coin_id} insufficient basis history ({len(history)}/{MIN_BASIS_DATA})",
                    ))
                    continue

                # Compute z-score
                z = _compute_zscore(history, current_basis_pct)
                action, strength, reason = _score_signal(z)

                signal_data = {
                    "coin": coin_id,
                    "basis_pct": round(current_basis_pct, 4),
                    "z_score": round(z, 2),
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "funding_rate": round(funding_rate, 6),
                    "history_length": len(history),
                }
                context_data[coin_id] = signal_data

                signals.append(Signal(
                    strategy=self.name,
                    symbol=alpaca_symbol,
                    action=action,
                    strength=strength,
                    reason=f"{coin_id} {reason}",
                    data=signal_data,
                ))

            except Exception as e:
                log.error("basis_zscore error for %s: %s", coin_id, e)
                signals.append(Signal(
                    strategy=self.name,
                    symbol=alpaca_symbol,
                    action="hold",
                    strength=0.0,
                    reason=f"{coin_id} basis_zscore error: {e}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
