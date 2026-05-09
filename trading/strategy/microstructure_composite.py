"""Microstructure Composite Strategy — order flow + volume imbalance from Bybit microstructure.

Combines three microstructure signals into a single composite score to
detect aggressive directional positioning:

1. Taker Volume Imbalance: ratio of taker buys vs total volume over recent hours
2. Order Book Pressure: bid/ask volume imbalance from top-of-book depth
3. Basis + Funding Divergence: futures premium vs funding rate disagreement

Composite score is weighted across all three signals. Only emits actionable
signals when the composite exceeds a minimum threshold, reducing noise.

NOTE: Despite the original file name ("liquidation_cascade"), this strategy
does NOT detect actual liquidation events. It is a microstructure composite
that uses order-flow and funding data as directional indicators.
"""

import logging

import numpy as np

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mappings
# ---------------------------------------------------------------------------

COINS = ["bitcoin", "ethereum", "solana"]
BYBIT_SYMBOLS = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT"}
ALPACA_SYMBOLS = {"bitcoin": "BTC/USD", "ethereum": "ETH/USD", "solana": "SOL/USD"}

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

TAKER_BUY_BULLISH = 0.58   # taker buy ratio above this is bullish
TAKER_BUY_BEARISH = 0.42   # taker buy ratio below this is bearish
OB_IMBALANCE_BULLISH = 0.60
OB_IMBALANCE_BEARISH = 0.40
MIN_COMPOSITE_SCORE = 0.25

# Signal weights
W_TAKER = 0.40
W_ORDERBOOK = 0.30
W_BASIS_FUNDING = 0.30

# Basis/funding thresholds
BASIS_PREMIUM_THRESHOLD = 0.0003   # 3 bps — significant premium
FUNDING_NEAR_ZERO = 0.0001         # absolute value below this is "near zero"
FUNDING_NEGATIVE_EXTREME = -0.0005 # extreme negative funding


# ---------------------------------------------------------------------------
# Signal computation helpers
# ---------------------------------------------------------------------------

def _taker_volume_signal(taker_ratio: float | None) -> float:
    """Convert taker buy ratio to a signal in [-1, +1].

    Returns 0 (neutral) when ratio is None or in the dead zone.
    """
    if taker_ratio is None:
        return 0.0

    if taker_ratio >= TAKER_BUY_BULLISH:
        # Scale linearly: 0.58 -> 0, 1.0 -> 1.0
        return min((taker_ratio - TAKER_BUY_BULLISH) / (1.0 - TAKER_BUY_BULLISH), 1.0)
    elif taker_ratio <= TAKER_BUY_BEARISH:
        # Scale linearly: 0.42 -> 0, 0.0 -> -1.0
        return max(-(TAKER_BUY_BEARISH - taker_ratio) / TAKER_BUY_BEARISH, -1.0)
    else:
        return 0.0


def _orderbook_signal(imbalance: float | None) -> float:
    """Convert order book bid/ask imbalance to a signal in [-1, +1].

    Returns 0 (neutral) when imbalance is None or in the dead zone.
    """
    if imbalance is None:
        return 0.0

    if imbalance >= OB_IMBALANCE_BULLISH:
        # Scale linearly: 0.60 -> 0, 1.0 -> 1.0
        return min((imbalance - OB_IMBALANCE_BULLISH) / (1.0 - OB_IMBALANCE_BULLISH), 1.0)
    elif imbalance <= OB_IMBALANCE_BEARISH:
        # Scale linearly: 0.40 -> 0, 0.0 -> -1.0
        return max(-(OB_IMBALANCE_BEARISH - imbalance) / OB_IMBALANCE_BEARISH, -1.0)
    else:
        return 0.0


def _basis_funding_signal(basis: float | None, funding: float | None) -> float:
    """Detect divergence between basis spread and funding rate.

    Returns a signal in [-1, +1]:
      - Positive basis + near-zero funding: smart money long -> bullish
      - Negative basis + negative funding: extreme bearishness -> contrarian buy
      - Otherwise neutral
    """
    if basis is None or funding is None:
        return 0.0

    abs_funding = abs(funding)

    # Case 1: Significant premium but funding hasn't caught up — smart money long
    if basis > BASIS_PREMIUM_THRESHOLD and abs_funding < FUNDING_NEAR_ZERO:
        # Strength proportional to how large the premium is
        return min(basis / (BASIS_PREMIUM_THRESHOLD * 5), 1.0)

    # Case 2: Futures discount + extreme negative funding — potential reversal
    if basis < -BASIS_PREMIUM_THRESHOLD and funding < FUNDING_NEGATIVE_EXTREME:
        # Contrarian bullish signal — strength proportional to extremity
        return min(abs(funding) / 0.002, 1.0) * 0.7  # cap at 0.7, it's contrarian

    # Case 3: Large negative basis with near-zero funding — distribution
    if basis < -BASIS_PREMIUM_THRESHOLD and abs_funding < FUNDING_NEAR_ZERO:
        return max(basis / (BASIS_PREMIUM_THRESHOLD * 5), -1.0)

    return 0.0


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

@register
class MicrostructureCompositeStrategy(Strategy):
    """Trade based on Bybit order flow imbalance and microstructure signals."""

    name = "microstructure_composite"

    def __init__(self):
        self.coins = COINS
        self._last_context: dict = {}

    def generate_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        context_data: dict = {}

        # Import data functions here so missing bybit module doesn't crash registry
        try:
            from trading.data.bybit import (
                get_taker_volume_ratio,
                get_orderbook_imbalance,
                get_basis_spread,
                get_funding_rates,
            )
        except ImportError:
            log.warning("trading.data.bybit not available — microstructure_composite disabled")
            self._last_context = {}
            return signals

        # Fetch funding rates once (shared across all symbols)
        funding_rates = get_funding_rates()

        for coin_id in self.coins:
            bybit_sym = BYBIT_SYMBOLS.get(coin_id)
            if not bybit_sym:
                continue

            try:
                signal = self._evaluate_coin(
                    coin_id, bybit_sym, funding_rates,
                    get_taker_volume_ratio, get_orderbook_imbalance, get_basis_spread,
                    context_data,
                )
                signals.append(signal)

            except Exception as exc:
                log.error("microstructure_composite error for %s: %s", coin_id, exc)
                signals.append(Signal(
                    strategy=self.name,
                    symbol=bybit_sym,
                    action="hold",
                    strength=0.0,
                    reason=f"{coin_id} microstructure error: {exc}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evaluate_coin(
        self,
        coin_id: str,
        bybit_sym: str,
        funding_rates: dict,
        get_taker_volume_ratio,
        get_orderbook_imbalance,
        get_basis_spread,
        context_data: dict,
    ) -> Signal:
        """Compute composite microstructure signal for a single coin."""

        # --- Fetch raw data (returns dicts — extract scalars) ---
        taker_data = get_taker_volume_ratio(bybit_sym, interval="1h", limit=6)
        ob_data = get_orderbook_imbalance(bybit_sym, depth=20)
        basis_data = get_basis_spread(bybit_sym)
        funding = funding_rates.get(coin_id)  # keyed by coin_id, not bybit_sym

        taker_ratio = taker_data.get("buy_ratio") if isinstance(taker_data, dict) else None
        ob_imbalance = ob_data.get("imbalance") if isinstance(ob_data, dict) else None
        basis = basis_data.get("basis_pct", 0) / 100.0 if isinstance(basis_data, dict) else None

        # --- Compute component signals ---
        sig_taker = _taker_volume_signal(taker_ratio)
        sig_ob = _orderbook_signal(ob_imbalance)
        sig_bf = _basis_funding_signal(basis, funding)

        # --- Composite score ---
        composite = sig_taker * W_TAKER + sig_ob * W_ORDERBOOK + sig_bf * W_BASIS_FUNDING

        # --- Store context ---
        context_data[coin_id] = {
            "taker_ratio": round(taker_ratio, 4) if taker_ratio is not None else None,
            "ob_imbalance": round(ob_imbalance, 4) if ob_imbalance is not None else None,
            "basis_bps": round(basis * 10000, 2) if basis is not None else None,
            "funding_rate": round(funding, 6) if funding is not None else None,
            "signal_taker": round(sig_taker, 4),
            "signal_orderbook": round(sig_ob, 4),
            "signal_basis_funding": round(sig_bf, 4),
            "composite": round(composite, 4),
        }

        # --- Build reason string ---
        components = []
        if taker_ratio is not None:
            components.append(f"taker={taker_ratio:.1%}")
        if ob_imbalance is not None:
            components.append(f"ob={ob_imbalance:.1%}")
        if basis is not None:
            components.append(f"basis={basis * 10000:.1f}bps")
        if funding is not None:
            components.append(f"fund={funding:.4%}")
        detail = ", ".join(components) if components else "no data"

        # --- Determine action ---
        if composite > MIN_COMPOSITE_SCORE:
            action = "buy"
            strength = min(abs(composite), 1.0)
            reason = f"{coin_id} microstructure bullish (score {composite:+.3f}): {detail}"
        elif composite < -MIN_COMPOSITE_SCORE:
            action = "sell"
            strength = min(abs(composite), 1.0)
            reason = f"{coin_id} microstructure bearish (score {composite:+.3f}): {detail}"
        else:
            action = "hold"
            strength = 0.0
            reason = f"{coin_id} microstructure neutral (score {composite:+.3f}): {detail}"

        return Signal(
            strategy=self.name,
            symbol=bybit_sym,
            action=action,
            strength=strength,
            reason=reason,
            data=context_data[coin_id],
        )
