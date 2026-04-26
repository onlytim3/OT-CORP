"""Whale Flow Detection Strategy — detect large-player positioning via order book analysis.

Detects whale activity by analyzing order book depth asymmetry and large trade
clusters across multiple depth levels.

Signal logic:
- Fetch deep order book (50 levels) from AsterDex
- Compute top-of-book imbalance (levels 1-5) — reflects retail/HFT flow
- Compute deep-book imbalance (levels 10-50) — reveals whale positioning
- Compare the two signals for divergence:
  - Top bullish + deep bearish: whales selling into retail buying -> SELL
  - Top bearish + deep bullish: whales accumulating below surface -> BUY
  - Both bullish: strong BUY
  - Both bearish: strong SELL

Amplifiers:
1. Divergence between top and deep imbalance (strongest signal)
2. Spread-normalized large order clusters
3. Absolute magnitude of imbalance

Data source: AsterDex public orderbook API (no auth required).
Execution: AsterDex perpetual futures.
"""

import logging
import time
from typing import Any

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Target coins
# ---------------------------------------------------------------------------

WHALE_COINS = ["bitcoin", "ethereum", "solana"]

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

TOP_LEVELS = 5            # Levels for retail/HFT flow measurement
DEEP_START = 10           # Start of whale zone (skip mid-book noise)
DEEP_END = 50             # End of whale zone
IMBALANCE_THRESHOLD = 0.15  # Minimum imbalance magnitude to be significant (-1 to +1)
DIVERGENCE_BONUS = 0.2    # Extra strength when top vs deep signals diverge


# ---------------------------------------------------------------------------
# Data helpers — lazy imports with graceful degradation
# ---------------------------------------------------------------------------

def _fetch_deep_orderbook(aster_symbol: str) -> dict | None:
    """Fetch 50-level order book from AsterDex.

    Returns dict with 'bids' and 'asks' as lists of [price, qty],
    or None on failure.
    """
    try:
        from trading.execution.aster_client import get_aster_orderbook
        book = get_aster_orderbook(aster_symbol, limit=50)
        if not book or not book.get("bids") or not book.get("asks"):
            log.warning("whale_flow: empty orderbook for %s", aster_symbol)
            return None
        return book
    except ImportError:
        log.warning("trading.execution.aster_client not available — whale_flow skipped")
        return None
    except Exception as e:
        log.error("whale_flow: failed to fetch orderbook for %s: %s", aster_symbol, e)
        return None


def _fetch_top_imbalance(aster_symbol: str) -> dict | None:
    """Fetch top-of-book imbalance from the data layer (top 20 levels).

    Returns dict with bid_volume, ask_volume, imbalance, spread_bps, mid_price,
    or None on failure.
    """
    try:
        from trading.data.aster import get_orderbook_imbalance
        return get_orderbook_imbalance(aster_symbol, depth=20)
    except ImportError:
        return None
    except Exception as e:
        log.error("whale_flow: failed to fetch imbalance for %s: %s", aster_symbol, e)
        return None


# ---------------------------------------------------------------------------
# Order book analysis
# ---------------------------------------------------------------------------

def _compute_level_imbalance(
    bids: list[list[float]],
    asks: list[list[float]],
    start: int,
    end: int,
) -> float:
    """Compute bid/ask volume imbalance for a range of order book levels.

    Returns a value in [-1, +1]:
      +1 = all bid volume, no ask volume (extremely bullish)
      -1 = all ask volume, no bid volume (extremely bearish)
       0 = perfectly balanced

    Args:
        bids: List of [price, quantity] sorted best-to-worst.
        asks: List of [price, quantity] sorted best-to-worst.
        start: Start level index (0-based, inclusive).
        end: End level index (exclusive).
    """
    bid_slice = bids[start:end]
    ask_slice = asks[start:end]

    bid_vol = sum(qty for _, qty in bid_slice)
    ask_vol = sum(qty for _, qty in ask_slice)

    total = bid_vol + ask_vol
    if total < 1e-12:
        return 0.0

    # Normalized imbalance: (bid - ask) / (bid + ask)
    return (bid_vol - ask_vol) / total


def _compute_spread_bps(bids: list[list[float]], asks: list[list[float]]) -> float:
    """Compute the bid-ask spread in basis points.

    Returns 0.0 if the book is empty or prices are zero.
    """
    if not bids or not asks:
        return 0.0

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2.0

    if mid < 1e-12:
        return 0.0

    return ((best_ask - best_bid) / mid) * 10_000


def _score_whale_signal(
    top_imbalance: float,
    deep_imbalance: float,
) -> tuple[str, float, str]:
    """Determine action, strength, and reason from top vs deep imbalance.

    The core insight: when top-of-book and deep-book disagree, whales
    are positioning against retail flow. This divergence is the strongest signal.

    Returns:
        (action, strength, reason) tuple.
    """
    top_bullish = top_imbalance > IMBALANCE_THRESHOLD
    top_bearish = top_imbalance < -IMBALANCE_THRESHOLD
    deep_bullish = deep_imbalance > IMBALANCE_THRESHOLD
    deep_bearish = deep_imbalance < -IMBALANCE_THRESHOLD

    divergence_detected = False

    # -- Divergence cases (strongest signals) --

    if top_bullish and deep_bearish:
        # Retail buying, whales selling deep — fade the retail
        action = "sell"
        base_strength = 0.45
        divergence_detected = True
        reason = (
            f"whale selling: top bullish ({top_imbalance:+.3f}) "
            f"but deep bearish ({deep_imbalance:+.3f})"
        )

    elif top_bearish and deep_bullish:
        # Retail selling, whales accumulating deep — follow the whales
        action = "buy"
        base_strength = 0.45
        divergence_detected = True
        reason = (
            f"whale accumulation: top bearish ({top_imbalance:+.3f}) "
            f"but deep bullish ({deep_imbalance:+.3f})"
        )

    # -- Agreement cases (moderate signals) --

    elif top_bullish and deep_bullish:
        action = "buy"
        base_strength = 0.35
        reason = (
            f"broad buying pressure: top ({top_imbalance:+.3f}) "
            f"and deep ({deep_imbalance:+.3f}) both bullish"
        )

    elif top_bearish and deep_bearish:
        action = "sell"
        base_strength = 0.35
        reason = (
            f"broad selling pressure: top ({top_imbalance:+.3f}) "
            f"and deep ({deep_imbalance:+.3f}) both bearish"
        )

    # -- No clear signal --

    else:
        return "hold", 0.0, (
            f"no whale signal: top={top_imbalance:+.3f} deep={deep_imbalance:+.3f} "
            f"(threshold={IMBALANCE_THRESHOLD})"
        )

    # Apply divergence bonus
    if divergence_detected:
        base_strength = min(base_strength + DIVERGENCE_BONUS, 0.95)

    # Scale by magnitude of the deeper imbalance (whale conviction)
    magnitude_boost = min(abs(deep_imbalance) * 0.3, 0.15)
    final_strength = min(base_strength + magnitude_boost, 0.95)

    return action, round(final_strength, 2), reason


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

@register
class WhaleFlowStrategy(Strategy):
    """Whale flow detection: trade with large-player order book positioning."""

    name = "whale_flow"

    def __init__(self):
        self._last_context: dict[str, Any] = {}

    def generate_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        context_data: dict[str, Any] = {}

        try:
            from trading.config import ASTER_SYMBOLS
        except ImportError:
            log.warning("whale_flow: trading.config not available, skipping")
            return signals

        for coin_id in WHALE_COINS:
            aster_symbol = ASTER_SYMBOLS.get(coin_id)
            if not aster_symbol:
                log.warning("whale_flow: no AsterDex symbol for %s", coin_id)
                continue

            try:
                # Fetch deep order book (50 levels)
                book = _fetch_deep_orderbook(aster_symbol)
                if book is None:
                    # Abstain rather than veto — other strategies can still act on this symbol
                    log.warning("whale_flow: skipping %s — no orderbook data", coin_id)
                    continue

                bids = book["bids"]
                asks = book["asks"]

                # Collect multiple snapshots for time-weighted averaging
                snapshots = []
                top_imb = _compute_level_imbalance(bids, asks, 0, TOP_LEVELS)
                deep_imb = _compute_level_imbalance(bids, asks, DEEP_START, DEEP_END)
                snapshots.append((top_imb, deep_imb))

                # Try to get 2 more snapshots with short delays
                for _ in range(2):
                    time.sleep(2)  # 2-second delay between snapshots
                    book2 = _fetch_deep_orderbook(aster_symbol)
                    if book2:
                        bids2 = book2["bids"]
                        asks2 = book2["asks"]
                        snapshots.append((
                            _compute_level_imbalance(bids2, asks2, 0, TOP_LEVELS),
                            _compute_level_imbalance(bids2, asks2, DEEP_START, DEEP_END),
                        ))

                # Average across snapshots
                top_imbalance = sum(s[0] for s in snapshots) / len(snapshots)
                deep_imbalance = sum(s[1] for s in snapshots) / len(snapshots)

                # Compute spread
                spread_bps = _compute_spread_bps(bids, asks)

                # Also try to get the data-layer imbalance for cross-reference
                data_imbalance = _fetch_top_imbalance(aster_symbol)

                # Use data-layer spread if available (more accurate)
                if data_imbalance and "spread_bps" in data_imbalance:
                    spread_bps = data_imbalance["spread_bps"]

                # Score the signal
                action, strength, reason = _score_whale_signal(
                    top_imbalance, deep_imbalance,
                )

                divergence_detected = (
                    (top_imbalance > IMBALANCE_THRESHOLD and deep_imbalance < -IMBALANCE_THRESHOLD)
                    or (top_imbalance < -IMBALANCE_THRESHOLD and deep_imbalance > IMBALANCE_THRESHOLD)
                )

                signal_data = {
                    "coin": coin_id,
                    "top_imbalance": round(top_imbalance, 4),
                    "deep_imbalance": round(deep_imbalance, 4),
                    "spread_bps": round(spread_bps, 2),
                    "divergence_detected": divergence_detected,
                    "bid_levels": len(bids),
                    "ask_levels": len(asks),
                    "num_snapshots": len(snapshots),
                }
                context_data[coin_id] = signal_data

                signals.append(Signal(
                    strategy=self.name,
                    symbol=ASTER_SYMBOLS.get(coin_id, aster_symbol),
                    action=action,
                    strength=strength,
                    reason=f"{coin_id} {reason}",
                    data=signal_data,
                ))

            except Exception as e:
                log.error("whale_flow error for %s: %s", coin_id, e)
                signals.append(Signal(
                    strategy=self.name,
                    symbol=ASTER_SYMBOLS.get(coin_id, aster_symbol),
                    action="hold",
                    strength=0.0,
                    reason=f"{coin_id} whale_flow error: {e}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
