"""Open Interest - Price Divergence Strategy.

Detects divergence between open interest changes and price movement on
Bybit perpetual futures.  When OI rises but price does not follow (or
vice versa), the imbalance signals a potential reversal or capitulation.

Divergence matrix:
  OI rising  + price flat/falling  -> new shorts entering -> potential short squeeze -> BUY
  OI rising  + price rising        -> trend confirmation, check overleveraging       -> cautious BUY
  OI falling + price rising        -> shorts closing, trend weakening               -> SELL
  OI falling + price falling       -> longs closing, capitulation                   -> contrarian BUY (if extreme)

Data sources:
  - Open interest: Bybit ``/fapi/v1/openInterest`` (public, no auth)
  - Price klines:  ``trading.data.bybit.get_bybit_ohlcv``

Execution: signals emitted with Alpaca symbol for the execution router.
"""

import logging
from typing import Any

from trading.config import BYBIT_SYMBOLS
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mappings
# ---------------------------------------------------------------------------

COINS = ["bitcoin", "ethereum", "solana"]

BYBIT_SYMBOL_MAP = {c: BYBIT_SYMBOLS[c] for c in COINS}

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

OI_CHANGE_THRESHOLD = 0.05       # 5% OI change is significant
PRICE_FLAT_THRESHOLD = 0.01      # 1% price change is "flat"
LOOKBACK_HOURS = 24              # compare current vs 24 h ago
MIN_SIGNAL_STRENGTH = 0.3        # discard signals weaker than this
EXTREME_OI_CHANGE = 0.15         # 15% OI change is extreme
EXTREME_PRICE_CHANGE = 0.05      # 5% price move in 24 h is significant


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _fetch_open_interest(bybit_sym: str) -> float | None:
    """Fetch current open interest from Bybit."""
    try:
        from trading.execution.bybit_client import _public_get
        data = _public_get("/fapi/v1/openInterest", {"symbol": bybit_sym})
        return float(data.get("openInterest", 0))
    except Exception as e:
        log.warning("OI fetch failed for %s: %s", bybit_sym, e)
        return None


def _fetch_klines(bybit_sym: str, hours: int = LOOKBACK_HOURS):
    """Fetch recent hourly klines from Bybit.

    Returns a pandas DataFrame with OHLCV columns, or None on failure.
    """
    try:
        from trading.data.bybit import get_bybit_ohlcv
        df = get_bybit_ohlcv(bybit_sym, interval="1h", limit=hours + 1)
        if df is not None and not df.empty:
            return df
        return None
    except ImportError:
        log.warning("trading.data.bybit not available -- oi_price_divergence disabled")
        return None
    except Exception as e:
        log.warning("Kline fetch failed for %s: %s", bybit_sym, e)
        return None


def _price_change_pct(klines) -> float | None:
    """Calculate price change percentage over the kline window.

    Uses the close price of the first and last candle.
    """
    if klines is None or klines.empty or len(klines) < 2:
        return None
    try:
        first_close = float(klines["close"].iloc[0])
        last_close = float(klines["close"].iloc[-1])
        if first_close == 0:
            return None
        return (last_close - first_close) / first_close
    except (KeyError, IndexError, TypeError) as e:
        log.warning("Price change calculation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Divergence detection
# ---------------------------------------------------------------------------

def _classify_divergence(
    oi_change_pct: float,
    price_change_pct: float,
) -> tuple[str, str, float, str]:
    """Classify the OI-price divergence and return (action, divergence_type, strength, reason).

    Returns:
        action: "buy", "sell", or "hold"
        divergence_type: descriptive label
        strength: 0.0 - 1.0 confidence
        reason: human-readable rationale
    """
    oi_rising = oi_change_pct > OI_CHANGE_THRESHOLD
    oi_falling = oi_change_pct < -OI_CHANGE_THRESHOLD
    price_rising = price_change_pct > PRICE_FLAT_THRESHOLD
    price_falling = price_change_pct < -PRICE_FLAT_THRESHOLD
    price_flat = not price_rising and not price_falling

    oi_extreme = abs(oi_change_pct) > EXTREME_OI_CHANGE
    price_extreme = abs(price_change_pct) > EXTREME_PRICE_CHANGE

    # --- Case 1: OI rising + price flat/falling -> short squeeze setup ---
    if oi_rising and (price_flat or price_falling):
        divergence_type = "oi_up_price_down"
        base_strength = 0.45
        reason = (
            f"OI rising {oi_change_pct:+.1%} while price {price_change_pct:+.1%} "
            f"-- new shorts entering, potential short squeeze"
        )
        if oi_extreme:
            base_strength += 0.15
            reason += " (extreme OI buildup)"
        if price_falling and price_extreme:
            base_strength += 0.10
            reason += " (deep price decline amplifies squeeze potential)"
        return "buy", divergence_type, min(base_strength, 0.95), reason

    # --- Case 2: OI rising + price rising -> trend confirmation ---
    if oi_rising and price_rising:
        divergence_type = "oi_up_price_up"
        base_strength = 0.35
        reason = (
            f"OI rising {oi_change_pct:+.1%} with price {price_change_pct:+.1%} "
            f"-- trend confirmed but watch for overleveraging"
        )
        if oi_extreme:
            # Overleveraged longs -- reduce confidence
            base_strength -= 0.10
            reason += " (extreme OI: overleveraged, caution)"
        return "buy", divergence_type, max(min(base_strength, 0.95), 0.1), reason

    # --- Case 3: OI falling + price rising -> shorts closing ---
    if oi_falling and price_rising:
        divergence_type = "oi_down_price_up"
        base_strength = 0.45
        reason = (
            f"OI falling {oi_change_pct:+.1%} while price rising {price_change_pct:+.1%} "
            f"-- shorts closing, trend weakening without new longs"
        )
        if oi_extreme:
            base_strength += 0.15
            reason += " (massive OI unwind)"
        return "sell", divergence_type, min(base_strength, 0.95), reason

    # --- Case 4: OI falling + price falling -> capitulation ---
    if oi_falling and price_falling:
        divergence_type = "oi_down_price_down"
        if oi_extreme and price_extreme:
            # Extreme capitulation -> contrarian buy
            base_strength = 0.50
            reason = (
                f"OI plunging {oi_change_pct:+.1%} with price collapsing {price_change_pct:+.1%} "
                f"-- extreme capitulation, contrarian buy"
            )
            return "buy", divergence_type, min(base_strength, 0.95), reason
        else:
            # Mild capitulation -- not actionable
            base_strength = 0.15
            reason = (
                f"OI falling {oi_change_pct:+.1%} with price declining {price_change_pct:+.1%} "
                f"-- mild capitulation, insufficient conviction"
            )
            return "hold", divergence_type, base_strength, reason

    # --- No significant divergence ---
    return (
        "hold",
        "neutral",
        0.0,
        f"no OI-price divergence (OI {oi_change_pct:+.1%}, price {price_change_pct:+.1%})",
    )


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

@register
class OIPriceDivergenceStrategy(Strategy):
    """Detect divergence between open interest and price to find reversal setups."""

    name = "oi_price_divergence"

    def __init__(self):
        self._last_context: dict[str, Any] = {}
        self._oi_snapshots: dict[str, list[float]] = {}  # rolling OI history per coin

    def generate_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        context_data: dict[str, Any] = {}

        for coin_id in COINS:
            bybit_sym = BYBIT_SYMBOL_MAP.get(coin_id)
            if not bybit_sym:
                continue

            try:
                signal = self._evaluate_coin(coin_id, bybit_sym, context_data)
                signals.append(signal)
            except Exception as exc:
                log.error("oi_price_divergence error for %s: %s", coin_id, exc)
                signals.append(Signal(
                    strategy=self.name,
                    symbol=bybit_sym,
                    action="hold",
                    strength=0.0,
                    reason=f"{coin_id} oi_price_divergence error: {exc}",
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
        context_data: dict,
    ) -> Signal:
        """Evaluate OI-price divergence for a single coin."""

        # --- Fetch current open interest ---
        oi_current = _fetch_open_interest(bybit_sym)
        if oi_current is None:
            return Signal(
                strategy=self.name,
                symbol=bybit_sym,
                action="hold",
                strength=0.0,
                reason=f"{coin_id} OI data unavailable",
                data={"coin": coin_id, "error": "oi_fetch_failed"},
            )

        # --- Fetch klines for price change ---
        klines = _fetch_klines(bybit_sym, hours=LOOKBACK_HOURS)
        price_change = _price_change_pct(klines)
        if price_change is None:
            return Signal(
                strategy=self.name,
                symbol=bybit_sym,
                action="hold",
                strength=0.0,
                reason=f"{coin_id} price data unavailable",
                data={"coin": coin_id, "oi_current": oi_current, "error": "price_fetch_failed"},
            )

        # --- Estimate OI change from rolling snapshots ---
        oi_change_pct = self._compute_oi_change(coin_id, oi_current)

        # --- Build signal data for trade journal ---
        signal_data = {
            "coin": coin_id,
            "oi_current": round(oi_current, 2),
            "oi_change_pct": round(oi_change_pct, 4) if oi_change_pct is not None else None,
            "price_change_pct": round(price_change, 4),
            "divergence_type": "unknown",
        }

        if oi_change_pct is None:
            # First run -- no historical OI to compare, store snapshot and hold
            signal_data["note"] = "first_oi_snapshot_stored"
            context_data[coin_id] = signal_data
            return Signal(
                strategy=self.name,
                symbol=bybit_sym,
                action="hold",
                strength=0.0,
                reason=f"{coin_id} first OI snapshot stored, need history to compare",
                data=signal_data,
            )

        # --- Classify divergence ---
        action, divergence_type, strength, reason = _classify_divergence(
            oi_change_pct, price_change,
        )
        signal_data["divergence_type"] = divergence_type

        # Filter weak signals
        if action != "hold" and strength < MIN_SIGNAL_STRENGTH:
            action = "hold"
            reason = f"{coin_id} {divergence_type} signal too weak ({strength:.2f} < {MIN_SIGNAL_STRENGTH})"

        context_data[coin_id] = signal_data

        return Signal(
            strategy=self.name,
            symbol=bybit_sym,
            action=action,
            strength=round(strength, 2),
            reason=f"{coin_id} {reason}",
            data=signal_data,
        )

    def _compute_oi_change(self, coin_id: str, oi_current: float) -> float | None:
        """Track OI snapshots and compute percentage change.

        Stores the current OI value and compares against the oldest snapshot
        in the rolling window. Returns None if there is no prior snapshot.
        """
        if coin_id not in self._oi_snapshots:
            self._oi_snapshots[coin_id] = []

        history = self._oi_snapshots[coin_id]
        history.append(oi_current)

        # Keep a bounded rolling window (one snapshot per strategy run,
        # sized to cover roughly LOOKBACK_HOURS of runs at typical cadence).
        max_snapshots = max(LOOKBACK_HOURS, 48)
        if len(history) > max_snapshots:
            self._oi_snapshots[coin_id] = history[-max_snapshots:]
            history = self._oi_snapshots[coin_id]

        if len(history) < 2:
            return None

        oldest = history[0]
        if oldest == 0:
            return None
        return (oi_current - oldest) / oldest
