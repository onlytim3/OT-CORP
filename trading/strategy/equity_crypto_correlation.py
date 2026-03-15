"""Equity-Crypto Correlation Strategy — exploits correlation regime shifts
between equities (NVDA, TSLA, SPX) and crypto (BTC, ETH, SOL) to generate
directional signals on crypto.

Regime detection:
  - Correlation > 0.7:  High correlation — crypto follows equities.
      Use equity momentum to lead crypto signals.
  - Correlation < 0.2:  Decorrelation — crypto independent.
      No signal from this strategy.
  - Correlation negative: Divergence — unusual, potential crisis or rotation.

Signal logic:
  - High correlation + equities rising  -> BUY crypto (will follow)
  - High correlation + equities falling -> SELL crypto (will follow)
  - Low correlation                     -> no signal (crypto decoupled)
"""

import logging

import numpy as np

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Asset groups
# ---------------------------------------------------------------------------
EQUITY_IDS = ["nvidia", "tesla", "sp500"]
CRYPTO_IDS = ["bitcoin", "ethereum", "solana"]

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
CORRELATION_WINDOW = 14           # Rolling window in days
HIGH_CORR_THRESHOLD = 0.7        # Above this = high correlation regime
LOW_CORR_THRESHOLD = 0.2         # Below this = decorrelated regime
MIN_DATA_POINTS = 10             # Minimum days of data needed for correlation
EQUITY_MOMENTUM_THRESHOLD = 0.02  # 2% move considered significant


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_daily_closes(aster_symbol: str, days: int) -> np.ndarray | None:
    """Fetch daily klines from AsterDex and return close prices as numpy array.

    Returns array of close prices (oldest first) or None on failure.
    """
    try:
        from trading.execution.aster_client import get_aster_klines
    except ImportError:
        log.error("Cannot import get_aster_klines — aster_client unavailable")
        return None

    try:
        df = get_aster_klines(aster_symbol, interval="1d", limit=days + 5)
    except Exception as exc:
        log.warning("Failed to fetch daily klines for %s: %s", aster_symbol, exc)
        return None

    if df is None or df.empty or len(df) < MIN_DATA_POINTS:
        log.debug("Insufficient daily data for %s: got %d, need %d",
                  aster_symbol, len(df) if df is not None else 0, MIN_DATA_POINTS)
        return None

    return df["close"].values.astype(float)


def _compute_returns(prices: np.ndarray) -> np.ndarray:
    """Compute simple daily returns from a price array."""
    return np.diff(prices) / prices[:-1]


def _compute_basket_returns(
    asset_ids: list[str],
    aster_symbols: dict,
    days: int,
) -> tuple[np.ndarray | None, list[str]]:
    """Fetch daily closes for a basket and compute equal-weighted basket returns.

    Returns (basket_returns_array, list_of_ids_with_data) or (None, []) on failure.
    """
    all_returns: list[np.ndarray] = []
    ids_with_data: list[str] = []
    min_length = None

    for asset_id in asset_ids:
        aster_sym = aster_symbols.get(asset_id)
        if not aster_sym:
            log.debug("No AsterDex symbol for %s — skipping", asset_id)
            continue

        closes = _fetch_daily_closes(aster_sym, days)
        if closes is None or len(closes) < MIN_DATA_POINTS:
            continue

        returns = _compute_returns(closes)
        all_returns.append(returns)
        ids_with_data.append(asset_id)

        if min_length is None or len(returns) < min_length:
            min_length = len(returns)

    if not all_returns or min_length is None or min_length < MIN_DATA_POINTS - 1:
        return None, []

    # Trim all to same length (align from the right / most recent)
    aligned = np.array([r[-min_length:] for r in all_returns])

    # Equal-weighted basket return
    basket_returns = np.mean(aligned, axis=0)
    return basket_returns, ids_with_data


def _rolling_correlation(x: np.ndarray, y: np.ndarray, window: int) -> float | None:
    """Compute the most recent rolling correlation between two return series.

    Uses the last `window` observations. Returns None if insufficient data.
    """
    if len(x) < window or len(y) < window:
        return None

    x_window = x[-window:]
    y_window = y[-window:]

    # Guard against zero-variance series
    if np.std(x_window) < 1e-10 or np.std(y_window) < 1e-10:
        return None

    corr = np.corrcoef(x_window, y_window)[0, 1]
    return float(corr) if np.isfinite(corr) else None


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

@register
class EquityCryptoCorrelationStrategy(Strategy):
    """Exploit correlation regime shifts between equities and crypto."""

    name = "equity_crypto_correlation"

    def __init__(self):
        self._last_context: dict = {}

    def generate_signals(self) -> list[Signal]:
        try:
            from trading.config import ASTER_SYMBOLS, CRYPTO_SYMBOLS
        except ImportError:
            log.error("Cannot import symbol mappings from trading.config")
            return [self._hold("Config import failed")]

        # Fetch basket returns
        days_needed = CORRELATION_WINDOW + 5  # extra buffer
        equity_returns, equity_ids = _compute_basket_returns(
            EQUITY_IDS, ASTER_SYMBOLS, days_needed,
        )
        crypto_returns, crypto_ids = _compute_basket_returns(
            CRYPTO_IDS, ASTER_SYMBOLS, days_needed,
        )

        if equity_returns is None or crypto_returns is None:
            self._last_context = {
                "equity_ids": equity_ids,
                "crypto_ids": crypto_ids,
                "error": "Insufficient data for correlation calculation",
            }
            return [self._hold("Insufficient data for equity-crypto correlation")]

        # Align lengths
        min_len = min(len(equity_returns), len(crypto_returns))
        equity_returns = equity_returns[-min_len:]
        crypto_returns = crypto_returns[-min_len:]

        if min_len < MIN_DATA_POINTS - 1:
            return [self._hold(f"Only {min_len} overlapping data points, need {MIN_DATA_POINTS - 1}")]

        # Compute rolling correlation
        correlation = _rolling_correlation(
            equity_returns, crypto_returns, CORRELATION_WINDOW,
        )

        if correlation is None:
            self._last_context = {
                "equity_ids": equity_ids,
                "crypto_ids": crypto_ids,
                "error": "Correlation calculation failed (zero variance or insufficient data)",
            }
            return [self._hold("Cannot compute correlation — zero variance or insufficient data")]

        # Compute recent equity momentum (sum of last 5 days of returns)
        lookback = min(5, len(equity_returns))
        equity_momentum = float(np.sum(equity_returns[-lookback:]))

        # Compute recent crypto momentum for context
        crypto_momentum = float(np.sum(crypto_returns[-lookback:]))

        # Store context
        self._last_context = {
            "correlation": round(correlation, 4),
            "correlation_window": CORRELATION_WINDOW,
            "equity_momentum_5d": round(equity_momentum, 4),
            "crypto_momentum_5d": round(crypto_momentum, 4),
            "equity_ids": equity_ids,
            "crypto_ids": crypto_ids,
            "regime": (
                "high_correlation" if correlation > HIGH_CORR_THRESHOLD
                else "decorrelated" if correlation < LOW_CORR_THRESHOLD
                else "negative_divergence" if correlation < 0
                else "moderate_correlation"
            ),
        }

        signals: list[Signal] = []

        # ----- High correlation regime -----
        if correlation > HIGH_CORR_THRESHOLD:
            if equity_momentum > EQUITY_MOMENTUM_THRESHOLD:
                # Equities rising + high correlation -> BUY crypto
                strength = min(
                    0.4
                    + (correlation - HIGH_CORR_THRESHOLD) * 2
                    + (equity_momentum - EQUITY_MOMENTUM_THRESHOLD) * 3,
                    0.85,
                )
                for cid in crypto_ids:
                    sym = CRYPTO_SYMBOLS.get(cid)
                    if sym:
                        signals.append(Signal(
                            strategy=self.name,
                            symbol=sym,
                            action="buy",
                            strength=round(strength, 2),
                            reason=(
                                f"High eq-crypto corr ({correlation:.2f}): equities "
                                f"+{equity_momentum:.1%} (5d) — crypto should follow"
                            ),
                            data={
                                "signal_type": "high_corr_equity_up",
                                "correlation": round(correlation, 4),
                                "equity_momentum_5d": round(equity_momentum, 4),
                                "crypto_momentum_5d": round(crypto_momentum, 4),
                                "coin_id": cid,
                            },
                        ))

            elif equity_momentum < -EQUITY_MOMENTUM_THRESHOLD:
                # Equities falling + high correlation -> SELL crypto
                strength = min(
                    0.4
                    + (correlation - HIGH_CORR_THRESHOLD) * 2
                    + (abs(equity_momentum) - EQUITY_MOMENTUM_THRESHOLD) * 3,
                    0.85,
                )
                for cid in crypto_ids:
                    sym = CRYPTO_SYMBOLS.get(cid)
                    if sym:
                        signals.append(Signal(
                            strategy=self.name,
                            symbol=sym,
                            action="sell",
                            strength=round(strength, 2),
                            reason=(
                                f"High eq-crypto corr ({correlation:.2f}): equities "
                                f"{equity_momentum:.1%} (5d) — crypto will follow down"
                            ),
                            data={
                                "signal_type": "high_corr_equity_down",
                                "correlation": round(correlation, 4),
                                "equity_momentum_5d": round(equity_momentum, 4),
                                "crypto_momentum_5d": round(crypto_momentum, 4),
                                "coin_id": cid,
                            },
                        ))

        # ----- Negative correlation (divergence) — unusual regime -----
        elif correlation < 0:
            # Negative correlation is rare and often signals a structural shift.
            # If equities are rising but crypto falling (or vice versa), flag it
            # but with low strength — this is informational.
            if abs(equity_momentum) > EQUITY_MOMENTUM_THRESHOLD:
                # Crypto tends to mean-revert to correlation, so if equities
                # are rising and crypto is falling under negative corr,
                # crypto may snap back.
                if equity_momentum > EQUITY_MOMENTUM_THRESHOLD and crypto_momentum < 0:
                    strength = min(0.3 + abs(correlation) * 0.5, 0.55)
                    for cid in crypto_ids:
                        sym = CRYPTO_SYMBOLS.get(cid)
                        if sym:
                            signals.append(Signal(
                                strategy=self.name,
                                symbol=sym,
                                action="buy",
                                strength=round(strength, 2),
                                reason=(
                                    f"Negative eq-crypto corr ({correlation:.2f}): equities "
                                    f"+{equity_momentum:.1%} but crypto {crypto_momentum:.1%} "
                                    f"— divergence may revert"
                                ),
                                data={
                                    "signal_type": "negative_corr_divergence",
                                    "correlation": round(correlation, 4),
                                    "equity_momentum_5d": round(equity_momentum, 4),
                                    "crypto_momentum_5d": round(crypto_momentum, 4),
                                    "coin_id": cid,
                                },
                            ))

        # ----- Low correlation / decorrelated — no signal -----
        # When correlation is between 0 and LOW_CORR_THRESHOLD, or between
        # LOW_CORR_THRESHOLD and HIGH_CORR_THRESHOLD, this strategy has
        # no edge. Return hold.

        if not signals:
            regime = self._last_context.get("regime", "unknown")
            return [self._hold(
                f"No signal — correlation {correlation:.2f} "
                f"(regime: {regime}, equity 5d: {equity_momentum:.1%})"
            )]

        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, **self._last_context}

    # ---- internals --------------------------------------------------------

    def _hold(self, reason: str) -> Signal:
        return Signal(
            strategy=self.name,
            symbol="BTC/USD",
            action="hold",
            strength=0.0,
            reason=reason,
        )
