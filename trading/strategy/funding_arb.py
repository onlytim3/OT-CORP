"""Funding Rate Arbitrage Strategy — mean-reversion on perpetual futures funding rates.

Perpetual futures funding rates reflect leveraged positioning. Extreme rates
historically mean-revert, providing a contrarian alpha signal for spot trading.

Signal logic:
- Very positive funding (>0.05%/period): overleveraged longs -> SELL spot
- Very negative funding (<-0.05%/period): overleveraged shorts -> BUY spot
- Neutral funding: no edge -> HOLD

Amplifiers:
1. Funding rate acceleration (z-score of rate change)
2. Basis spread confirmation (mark vs index price divergence)
3. Cross-asset funding divergence (BTC vs altcoin funding skew)

Data source: Bybit API (perpetual futures, no auth required for data).
Execution: Alpaca (spot crypto).
"""

import logging
import statistics
from typing import Any

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mappings
# ---------------------------------------------------------------------------

FUNDING_COINS = ["bitcoin", "ethereum", "solana", "avalanche-2", "chainlink", "litecoin"]

BYBIT_SYMBOL_MAP = {
    "bitcoin": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "solana": "SOLUSDT",
    "avalanche-2": "AVAXUSDT",
    "chainlink": "LINKUSDT",
    "litecoin": "LTCUSDT",
}

BYBIT_SYMBOL_MAP = {
    "bitcoin": "BTC/USD",
    "ethereum": "ETH/USD",
    "solana": "SOL/USD",
    "avalanche-2": "AVAX/USD",
    "chainlink": "LINK/USD",
    "litecoin": "LTC/USD",
}

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

HIGH_FUNDING_THRESHOLD = 0.0005    # 0.05% per period
LOW_FUNDING_THRESHOLD = -0.0005    # -0.05% per period
EXTREME_FUNDING = 0.001            # 0.1% per period (~65% annualized)
BASIS_CONFIRM_THRESHOLD = 0.002    # 0.2% basis spread for confirmation
ZSCORE_STRONG = 1.5
ZSCORE_EXTREME = 2.5


# ---------------------------------------------------------------------------
# Bybit data helpers — graceful degradation
# ---------------------------------------------------------------------------

def _fetch_funding_rates() -> dict[str, float]:
    """Fetch current funding rates from Bybit."""
    try:
        from trading.data.bybit import get_funding_rates
        return get_funding_rates()
    except ImportError:
        log.warning("trading.data.bybit not available — funding_arb returns no signals")
        return {}
    except Exception as e:
        log.error("Failed to fetch Bybit funding rates: %s", e)
        return {}


def _fetch_funding_history(symbol: str) -> list[float]:
    """Fetch historical funding rate series for z-score calculation."""
    try:
        from trading.data.bybit import get_funding_rate_history
        return get_funding_rate_history(symbol)
    except ImportError:
        return []
    except Exception as e:
        log.error("Failed to fetch funding history for %s: %s", symbol, e)
        return []


def _fetch_basis_spreads() -> dict[str, float]:
    """Fetch basis spread for all tracked symbols.

    Returns mapping of coin_id -> basis_pct.
    """
    try:
        from trading.data.bybit import get_basis_spread
        data = get_basis_spread()
        if not isinstance(data, list):
            return {}
        from trading.config import BYBIT_SYMBOLS
        aster_to_coin = {v: k for k, v in BYBIT_SYMBOLS.items()}
        result = {}
        for entry in data:
            coin_id = aster_to_coin.get(entry.get("symbol", ""))
            if coin_id:
                result[coin_id] = entry.get("basis_pct", 0.0) / 100.0  # convert pct to ratio
        return result
    except ImportError:
        return {}
    except Exception as e:
        log.error("Failed to fetch basis spreads: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _compute_zscore(values: list[float]) -> float:
    """Z-score of most recent value relative to series."""
    if len(values) < 5:
        return 0.0
    mean = statistics.mean(values)
    stdev = statistics.stdev(values)
    if stdev < 1e-10:
        return 0.0
    return (values[-1] - mean) / stdev


def _compute_rate_change_zscore(history: list[float]) -> float:
    """Z-score of rate-of-change (acceleration) of funding rates."""
    if len(history) < 6:
        return 0.0
    changes = [history[i] - history[i - 1] for i in range(1, len(history))]
    return _compute_zscore(changes)


# ---------------------------------------------------------------------------
# Signal scoring
# ---------------------------------------------------------------------------

def _score_signal(
    funding_rate: float,
    basis_spread: float,
    rate_zscore: float,
    accel_zscore: float,
) -> tuple[str, float, str]:
    """Determine action, strength, and reason from funding metrics.

    Z-score of funding rate is the primary gate. Absolute funding thresholds
    serve as a fallback only for extreme values when z-score is below threshold.
    """
    reason_parts: list[str] = []

    # Primary gate: z-score of funding rate
    if abs(rate_zscore) < ZSCORE_STRONG:
        # Allow absolute thresholds as fallback only for extreme values
        if abs(funding_rate) < EXTREME_FUNDING:
            return "hold", 0.0, f"funding z={rate_zscore:.1f} below threshold, rate={funding_rate:.4%}"

    # Direction from funding rate sign
    if funding_rate > 0 or rate_zscore > 0:
        action = "sell"
    else:
        action = "buy"

    # Base strength from z-score magnitude
    if abs(rate_zscore) >= ZSCORE_EXTREME:
        base_strength = 0.6
        reason_parts.append(f"extreme funding z={rate_zscore:.1f} (rate={funding_rate:.4%})")
    elif abs(rate_zscore) >= ZSCORE_STRONG:
        base_strength = 0.4
        reason_parts.append(f"strong funding z={rate_zscore:.1f} (rate={funding_rate:.4%})")
    else:
        base_strength = 0.3  # fallback for extreme absolute funding
        reason_parts.append(f"extreme absolute funding {funding_rate:.4%} (z={rate_zscore:.1f})")

    # Amplifier: absolute funding threshold confirmation
    if abs(funding_rate) > EXTREME_FUNDING:
        base_strength = min(base_strength + 0.15, 0.95)
        reason_parts.append(f"extreme rate confirms ({funding_rate:.4%})")
    elif abs(funding_rate) > HIGH_FUNDING_THRESHOLD:
        base_strength = min(base_strength + 0.10, 0.95)
        reason_parts.append(f"high rate confirms ({funding_rate:.4%})")

    # Basis spread confirmation
    if action == "sell" and basis_spread > BASIS_CONFIRM_THRESHOLD:
        base_strength = min(base_strength + 0.15, 0.95)
        reason_parts.append(f"basis confirms ({basis_spread:.4%})")
    elif action == "buy" and basis_spread < -BASIS_CONFIRM_THRESHOLD:
        base_strength = min(base_strength + 0.15, 0.95)
        reason_parts.append(f"negative basis confirms ({basis_spread:.4%})")

    # Funding rate acceleration
    if abs(accel_zscore) > ZSCORE_EXTREME:
        base_strength = min(base_strength + 0.15, 0.95)
        reason_parts.append(f"rapid acceleration (z={accel_zscore:.1f})")
    elif abs(accel_zscore) > ZSCORE_STRONG:
        base_strength = min(base_strength + 0.10, 0.95)
        reason_parts.append(f"moderate acceleration (z={accel_zscore:.1f})")

    reason = " | ".join(reason_parts)
    return action, round(base_strength, 2), reason


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

@register
class FundingArbStrategy(Strategy):
    """Funding rate mean-reversion: fade extreme perp funding via spot positions."""

    name = "funding_arb"

    def __init__(self):
        self._last_context: dict[str, Any] = {}

    def generate_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        context_data: dict[str, Any] = {}

        funding_rates = _fetch_funding_rates()
        if not funding_rates:
            log.info("funding_arb: no funding rate data available, skipping")
            return signals

        basis_spreads = _fetch_basis_spreads()
        btc_funding = funding_rates.get("bitcoin", 0.0)

        for coin_id in FUNDING_COINS:
            bybit_symbol = BYBIT_SYMBOL_MAP.get(coin_id)
            bybit_symbol = BYBIT_SYMBOL_MAP.get(coin_id)
            if not bybit_symbol or not bybit_symbol:
                continue

            try:
                funding_rate = funding_rates.get(coin_id)
                if funding_rate is None:
                    continue

                basis_spread = basis_spreads.get(coin_id, 0.0)

                # Historical z-scores
                history = _fetch_funding_history(bybit_symbol)
                rate_zscore = _compute_zscore(history) if history else 0.0
                accel_zscore = _compute_rate_change_zscore(history) if history else 0.0

                action, strength, reason = _score_signal(
                    funding_rate, basis_spread, rate_zscore, accel_zscore,
                )

                # Cross-asset divergence: BTC funding negative but altcoin positive -> sell
                if coin_id != "bitcoin" and action == "sell":
                    if btc_funding < LOW_FUNDING_THRESHOLD and funding_rate > HIGH_FUNDING_THRESHOLD:
                        strength = min(strength + 0.1, 0.95)
                        reason += " | BTC funding negative divergence"

                signal_data = {
                    "coin": coin_id,
                    "funding_rate": round(funding_rate, 6),
                    "basis_spread": round(basis_spread, 6),
                    "rate_zscore": round(rate_zscore, 2),
                    "accel_zscore": round(accel_zscore, 2),
                    "btc_funding": round(btc_funding, 6),
                }
                context_data[coin_id] = signal_data

                signals.append(Signal(
                    strategy=self.name,
                    symbol=bybit_symbol,
                    action=action,
                    strength=strength,
                    reason=f"{coin_id} {reason}",
                    data=signal_data,
                ))

            except Exception as e:
                log.error("funding_arb error for %s: %s", coin_id, e)
                signals.append(Signal(
                    strategy=self.name,
                    symbol=bybit_symbol,
                    action="hold",
                    strength=0.0,
                    reason=f"{coin_id} funding_arb error: {e}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
