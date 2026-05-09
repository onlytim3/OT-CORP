"""Taker Volume Divergence Strategy — detect mismatch between aggressive order
flow and price movement on Bybit perpetual futures.

When taker buy volume dominates but price stays flat or declines, aggressive
buyers are being absorbed by passive sellers — a breakout to the upside is
likely.  The reverse (taker sell dominance + flat/rising price) signals a
pending downside move.

Signal logic:
- Bullish divergence: taker buy ratio > 0.55 but price flat/down  -> BUY
- Bearish divergence: taker buy ratio < 0.45 but price flat/up    -> SELL
- No divergence: taker flow aligns with price direction            -> HOLD

Amplifiers:
1. Taker ratio momentum (short-term vs long-term taker ratio trend)
2. Divergence strength (magnitude of ratio-price mismatch)
3. Cross-asset: BTC taker ratio vs altcoin for relative positioning

Data source: Bybit klines with taker buy volume (no auth required).
Execution: Bybit perpetual futures.
"""

import logging
from typing import Any

import numpy as np

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mappings
# ---------------------------------------------------------------------------

TAKER_COINS = [
    "bitcoin", "ethereum", "solana", "avalanche-2", "chainlink", "litecoin",
]

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

BULLISH_TAKER_THRESHOLD = 0.55       # Buy ratio above this = buyer aggression
BEARISH_TAKER_THRESHOLD = 0.45       # Buy ratio below this = seller aggression
PRICE_FLAT_THRESHOLD = 0.005         # 0.5% — price change smaller than this is "flat"
SHORT_PERIOD = 6                     # Hours for short-term taker ratio
LONG_PERIOD = 24                     # Hours for long-term taker ratio
MIN_DIVERGENCE_STRENGTH = 0.3        # Minimum signal strength to emit


# ---------------------------------------------------------------------------
# Data helpers — lazy imports with graceful degradation
# ---------------------------------------------------------------------------

def _fetch_ohlcv(symbol: str, interval: str = "1h", limit: int = 24):
    """Fetch OHLCV DataFrame from Bybit."""
    try:
        from trading.data.bybit import get_bybit_ohlcv
        return get_bybit_ohlcv(symbol, interval=interval, limit=limit)
    except ImportError:
        log.warning("trading.data.bybit not available — taker_divergence returns no signals")
        return None
    except Exception as e:
        log.error("Failed to fetch Bybit OHLCV for %s: %s", symbol, e)
        return None


def _fetch_taker_ratio(symbol: str, limit: int = 24) -> dict | None:
    """Fetch aggregated taker volume ratio."""
    try:
        from trading.data.bybit import get_taker_volume_ratio
        return get_taker_volume_ratio(symbol, interval="1h", limit=limit)
    except ImportError:
        return None
    except Exception as e:
        log.error("Failed to fetch taker volume ratio for %s: %s", symbol, e)
        return None


# ---------------------------------------------------------------------------
# Computation helpers
# ---------------------------------------------------------------------------

def _compute_taker_ratios(df, short_period: int, long_period: int) -> tuple[float, float]:
    """Compute short-term and long-term taker buy ratios from OHLCV DataFrame.

    Returns (short_ratio, long_ratio).  Both are 0.0 on failure.
    """
    if df is None or df.empty:
        return 0.0, 0.0

    required = {"volume", "taker_buy_base_vol"}
    if not required.issubset(df.columns):
        return 0.0, 0.0

    # Long-term ratio (full DataFrame up to long_period rows)
    long_df = df.tail(long_period)
    long_vol = long_df["volume"].sum()
    long_buy = long_df["taker_buy_base_vol"].sum()
    long_ratio = float(long_buy / long_vol) if long_vol > 0 else 0.0

    # Short-term ratio (last short_period rows)
    short_df = df.tail(short_period)
    short_vol = short_df["volume"].sum()
    short_buy = short_df["taker_buy_base_vol"].sum()
    short_ratio = float(short_buy / short_vol) if short_vol > 0 else 0.0

    return short_ratio, long_ratio


def _compute_price_change(df, periods: int) -> float:
    """Compute percentage price change over the last N rows.

    Returns 0.0 on failure.
    """
    if df is None or df.empty or "close" not in df.columns:
        return 0.0

    subset = df.tail(periods)
    if len(subset) < 2:
        return 0.0

    close_start = float(subset.iloc[0]["close"])
    close_end = float(subset.iloc[-1]["close"])

    if close_start <= 0:
        return 0.0

    return (close_end - close_start) / close_start


def _detect_divergence(
    taker_ratio_short: float,
    price_change_pct: float,
) -> tuple[str, str]:
    """Detect divergence between taker buy ratio and price movement.

    Returns (action, divergence_type).
    """
    buyers_aggressive = taker_ratio_short > BULLISH_TAKER_THRESHOLD
    sellers_aggressive = taker_ratio_short < BEARISH_TAKER_THRESHOLD
    price_flat_or_down = price_change_pct <= PRICE_FLAT_THRESHOLD
    price_flat_or_up = price_change_pct >= -PRICE_FLAT_THRESHOLD

    if buyers_aggressive and price_flat_or_down:
        return "buy", "bullish"
    if sellers_aggressive and price_flat_or_up:
        return "sell", "bearish"

    return "hold", "none"


def _compute_signal_strength(
    taker_ratio_short: float,
    taker_ratio_long: float,
    price_change_pct: float,
    divergence_type: str,
) -> float:
    """Score signal strength from 0.0 to 1.0 based on divergence magnitude."""
    if divergence_type == "none":
        return 0.0

    # Base: how far taker ratio deviates from neutral (0.5)
    ratio_deviation = abs(taker_ratio_short - 0.5)
    base_strength = min(ratio_deviation / 0.15, 1.0) * 0.5  # max 0.5 from ratio alone

    # Bonus: price moving against taker pressure amplifies the signal
    price_against = abs(price_change_pct) if price_change_pct != 0 else 0.0
    if divergence_type == "bullish" and price_change_pct < 0:
        base_strength += min(price_against / 0.03, 1.0) * 0.2
    elif divergence_type == "bearish" and price_change_pct > 0:
        base_strength += min(price_against / 0.03, 1.0) * 0.2

    # Bonus: short-term ratio momentum (short vs long) confirms building pressure
    if divergence_type == "bullish":
        momentum = taker_ratio_short - taker_ratio_long
    else:
        momentum = taker_ratio_long - taker_ratio_short

    if momentum > 0:
        base_strength += min(momentum / 0.10, 1.0) * 0.15

    return round(min(base_strength, 0.95), 2)


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

@register
class TakerDivergenceStrategy(Strategy):
    """Taker volume divergence: fade absorbed aggression for breakout entries."""

    name = "taker_divergence"

    def __init__(self):
        self._last_context: dict[str, Any] = {}

    def generate_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        context_data: dict[str, Any] = {}

        # Fetch BTC taker ratio for cross-asset comparison
        btc_taker = _fetch_taker_ratio(BYBIT_SYMBOL_MAP["bitcoin"], limit=SHORT_PERIOD)
        btc_buy_ratio = btc_taker["buy_ratio"] if btc_taker else 0.5

        for coin_id in TAKER_COINS:
            bybit_symbol = BYBIT_SYMBOL_MAP.get(coin_id)
            bybit_symbol = BYBIT_SYMBOL_MAP.get(coin_id)
            if not bybit_symbol or not bybit_symbol:
                continue

            try:
                # Fetch full kline history (24h of 1h candles)
                df = _fetch_ohlcv(bybit_symbol, interval="1h", limit=LONG_PERIOD)
                if df is None or df.empty:
                    continue

                # Compute taker ratios
                taker_short, taker_long = _compute_taker_ratios(
                    df, SHORT_PERIOD, LONG_PERIOD,
                )

                # Compute price change over short period
                price_change = _compute_price_change(df, SHORT_PERIOD)

                # Detect divergence
                action, div_type = _detect_divergence(taker_short, price_change)

                # Compute signal strength
                strength = _compute_signal_strength(
                    taker_short, taker_long, price_change, div_type,
                )

                # Build reason string
                reason_parts = []
                if div_type == "bullish":
                    reason_parts.append(
                        f"bullish divergence: taker buy ratio {taker_short:.3f} "
                        f"but price {price_change:+.2%}"
                    )
                elif div_type == "bearish":
                    reason_parts.append(
                        f"bearish divergence: taker buy ratio {taker_short:.3f} "
                        f"but price {price_change:+.2%}"
                    )
                else:
                    reason_parts.append(
                        f"no divergence: taker {taker_short:.3f}, price {price_change:+.2%}"
                    )

                # Taker ratio momentum
                ratio_momentum = taker_short - taker_long
                if abs(ratio_momentum) > 0.02:
                    direction = "rising" if ratio_momentum > 0 else "falling"
                    reason_parts.append(
                        f"taker momentum {direction} ({ratio_momentum:+.3f})"
                    )

                # Cross-asset: compare vs BTC taker ratio for altcoins
                if coin_id != "bitcoin" and action != "hold":
                    relative_diff = taker_short - btc_buy_ratio
                    if abs(relative_diff) > 0.03:
                        if (action == "buy" and relative_diff > 0) or \
                           (action == "sell" and relative_diff < 0):
                            strength = min(strength + 0.1, 0.95)
                            reason_parts.append(
                                f"BTC taker divergence confirms "
                                f"(alt-BTC diff {relative_diff:+.3f})"
                            )

                # Skip weak signals below minimum threshold
                if action != "hold" and strength < MIN_DIVERGENCE_STRENGTH:
                    action = "hold"
                    reason_parts.append(
                        f"strength {strength:.2f} below threshold "
                        f"{MIN_DIVERGENCE_STRENGTH}"
                    )

                signal_data = {
                    "coin": coin_id,
                    "taker_ratio_short": round(taker_short, 4),
                    "taker_ratio_long": round(taker_long, 4),
                    "price_change_pct": round(price_change, 6),
                    "divergence_type": div_type,
                    "ratio_momentum": round(ratio_momentum, 4),
                    "btc_buy_ratio": round(btc_buy_ratio, 4),
                }
                context_data[coin_id] = signal_data

                signals.append(Signal(
                    strategy=self.name,
                    symbol=bybit_symbol,
                    action=action,
                    strength=strength,
                    reason=f"{coin_id} {' | '.join(reason_parts)}",
                    data=signal_data,
                ))

            except Exception as e:
                log.error("taker_divergence error for %s: %s", coin_id, e)
                signals.append(Signal(
                    strategy=self.name,
                    symbol=bybit_symbol,
                    action="hold",
                    strength=0.0,
                    reason=f"{coin_id} taker_divergence error: {e}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
