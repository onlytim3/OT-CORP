"""Volatility Regime Strategy — detect vol regime shifts and position accordingly.

Instead of predicting direction, this strategy identifies volatility regime
transitions and adjusts positioning:

- **Vol compression** (short vol < 0.5 * long vol): Bollinger squeeze / accumulation
  phase — breakout imminent, enter long.
- **Vol expansion** (short vol > 2.0 * long vol): Distribution / panic — reversion
  coming, reduce or go short.
- **Vol normalization** (short vol ~ long vol): No edge, hold.

Additional cross-asset signal: when altcoin realized vol significantly exceeds
BTC vol, a rotation out of alts is likely.

Uses hourly klines from AsterDex (7 days = 168 candles).
Realized volatility = annualized standard deviation of log returns.
"""

import logging

import numpy as np

from trading.config import ASTER_SYMBOLS, CRYPTO_SYMBOLS
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

try:
    from trading.data.aster import get_aster_ohlcv
except ImportError:
    get_aster_ohlcv = None

log = logging.getLogger(__name__)

# --- Strategy parameters ---------------------------------------------------

VOLATILITY_REGIME = {
    "coins": ["bitcoin", "ethereum", "solana", "avalanche-2", "polkadot"],
    "short_window": 24,          # hours (1 day)
    "long_window": 168,          # hours (7 days)
    "compression_ratio": 0.5,    # short/long vol below this = compression
    "expansion_ratio": 2.0,      # short/long vol above this = expansion
    "min_data_points": 100,      # minimum candles required
    "hours_per_year": 8760,      # for annualization
}


# ---------------------------------------------------------------------------
# Volatility helpers
# ---------------------------------------------------------------------------

def _compute_realized_vol(closes: np.ndarray, window: int,
                          hours_per_year: int = 8760) -> float:
    """Compute annualized realized volatility from hourly close prices.

    Realized vol = std(log returns over *window*) * sqrt(hours_per_year).
    Returns 0.0 if insufficient data.
    """
    if len(closes) < window + 1:
        return 0.0

    # Use only the last *window* returns
    prices = closes[-(window + 1):]
    log_returns = np.diff(np.log(prices))

    if len(log_returns) == 0:
        return 0.0

    return float(np.std(log_returns, ddof=1) * np.sqrt(hours_per_year))


def _classify_regime(vol_ratio: float, compression: float,
                     expansion: float) -> str:
    """Classify the volatility regime from the short/long vol ratio."""
    if vol_ratio <= compression:
        return "compression"
    elif vol_ratio >= expansion:
        return "expansion"
    return "normal"


def _regime_to_action(regime: str) -> str:
    """Map a volatility regime to a trading action."""
    if regime == "compression":
        return "buy"
    elif regime == "expansion":
        return "sell"
    return "hold"


def _get_directional_confirmation(coin_id: str) -> str | None:
    """Get directional bias from other strategy signals if available."""
    try:
        from trading.db.store import get_setting
        import json
        # Check HMM regime state
        hmm_params = get_setting("hmm_model_params")
        if hmm_params:
            data = json.loads(hmm_params)
            regime = data.get("last_regime", "")
            if regime == "bull":
                return "buy"
            elif regime == "bear":
                return "sell"
        # Check Kalman slope
        kalman_positions = get_setting("kalman_positions")
        if kalman_positions:
            positions = json.loads(kalman_positions)
            if coin_id in positions:
                return "buy" if positions[coin_id].get("direction") == "long" else "sell"
    except Exception:
        pass
    return None


def _regime_strength(vol_ratio: float, regime: str,
                     compression: float, expansion: float) -> float:
    """Compute signal strength (0.0 – 1.0) based on how extreme the ratio is.

    Deeper compression or wilder expansion yields higher confidence.
    """
    if regime == "compression":
        # Ratio approaching 0 → stronger signal.  At threshold (0.5) → 0.3.
        return round(min(max((compression - vol_ratio) / compression, 0.0), 1.0) * 0.7 + 0.3, 2)
    elif regime == "expansion":
        # Ratio growing beyond threshold → stronger signal.
        excess = (vol_ratio - expansion) / expansion
        return round(min(excess * 0.5 + 0.4, 1.0), 2)
    return 0.0


# ---------------------------------------------------------------------------
# Cross-asset rotation signal
# ---------------------------------------------------------------------------

def _check_rotation_signal(vol_data: dict[str, dict],
                           btc_coin: str = "bitcoin") -> dict | None:
    """Detect rotation signal: altcoin vol >> BTC vol suggests flee-to-safety.

    Returns a dict describing the rotation or None if BTC data is unavailable.
    """
    btc_info = vol_data.get(btc_coin)
    if not btc_info or btc_info["short_vol"] == 0:
        return None

    btc_short_vol = btc_info["short_vol"]
    alts_elevated = []

    for coin, info in vol_data.items():
        if coin == btc_coin:
            continue
        if info["short_vol"] <= 0:
            continue
        ratio = info["short_vol"] / btc_short_vol
        if ratio > 2.0:
            alts_elevated.append((coin, round(ratio, 2)))

    if not alts_elevated:
        return None

    return {
        "btc_short_vol": round(btc_short_vol, 4),
        "elevated_alts": alts_elevated,
        "interpretation": "Altcoin vol elevated vs BTC — rotation risk",
    }


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

@register
class VolatilityRegimeStrategy(Strategy):
    """Detect volatility regime shifts and position accordingly.

    Buy during low-vol accumulation (compression), sell during high-vol
    distribution (expansion), and hold when vol is normalised.
    """

    name = "volatility_regime"

    def __init__(self):
        self.config = VOLATILITY_REGIME
        self._last_context: dict = {}

    def generate_signals(self) -> list[Signal]:
        if get_aster_ohlcv is None:
            log.error("volatility_regime: trading.data.aster not available")
            return []

        signals: list[Signal] = []
        vol_data: dict[str, dict] = {}

        short_w = self.config["short_window"]
        long_w = self.config["long_window"]
        comp_ratio = self.config["compression_ratio"]
        exp_ratio = self.config["expansion_ratio"]
        min_pts = self.config["min_data_points"]
        hpy = self.config["hours_per_year"]

        for coin in self.config["coins"]:
            symbol = CRYPTO_SYMBOLS.get(coin)
            aster_sym = ASTER_SYMBOLS.get(coin)

            if not symbol or not aster_sym:
                log.warning("volatility_regime: no symbol mapping for %s", coin)
                continue

            try:
                df = get_aster_ohlcv(aster_sym, interval="1h", limit=long_w)

                if df is None or df.empty or len(df) < min_pts:
                    reason = (
                        f"{coin}: insufficient data ({len(df) if df is not None and not df.empty else 0}"
                        f"/{min_pts} candles)"
                    )
                    signals.append(Signal(
                        strategy=self.name, symbol=symbol, action="hold",
                        strength=0.0, reason=reason,
                    ))
                    continue

                closes = df["close"].values.astype(float)

                short_vol = _compute_realized_vol(closes, short_w, hpy)
                long_vol = _compute_realized_vol(closes, long_w, hpy)

                if long_vol == 0:
                    signals.append(Signal(
                        strategy=self.name, symbol=symbol, action="hold",
                        strength=0.0,
                        reason=f"{coin}: long-window vol is zero — no regime signal",
                    ))
                    continue

                vol_ratio = short_vol / long_vol
                regime = _classify_regime(vol_ratio, comp_ratio, exp_ratio)
                action = _regime_to_action(regime)
                strength = _regime_strength(vol_ratio, regime, comp_ratio, exp_ratio)

                # Store for cross-asset and context
                vol_data[coin] = {
                    "short_vol": round(short_vol, 4),
                    "long_vol": round(long_vol, 4),
                    "vol_ratio": round(vol_ratio, 4),
                    "regime": regime,
                }

                reason = (
                    f"{coin} vol regime={regime}: "
                    f"short_vol={short_vol:.4f}, long_vol={long_vol:.4f}, "
                    f"ratio={vol_ratio:.2f} "
                    f"(compression<{comp_ratio}, expansion>{exp_ratio})"
                )

                # Check directional confirmation
                directional_action = _get_directional_confirmation(coin)
                if action != "hold" and directional_action is not None:
                    if action != directional_action:
                        # Vol signal and direction disagree — hold
                        reason += f" [HELD: vol says {action} but direction says {directional_action}]"
                        action = "hold"
                        strength = 0.0

                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    action=action,
                    strength=strength,
                    reason=reason,
                    data={
                        "short_vol": round(short_vol, 4),
                        "long_vol": round(long_vol, 4),
                        "vol_ratio": round(vol_ratio, 4),
                        "regime": regime,
                    },
                ))

            except Exception as e:
                log.error("volatility_regime: error processing %s: %s", coin, e)
                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol if symbol else f"{coin}/USD",
                    action="hold",
                    strength=0.0,
                    reason=f"{coin}: analysis failed — {e}",
                ))

        # Cross-asset rotation check
        rotation = _check_rotation_signal(vol_data)
        if rotation:
            for alt_coin, alt_ratio in rotation["elevated_alts"]:
                alt_sym = CRYPTO_SYMBOLS.get(alt_coin)
                if not alt_sym:
                    continue
                # Downgrade any existing buy signal for this alt to hold
                for sig in signals:
                    if sig.symbol == alt_sym and sig.action == "buy":
                        sig.action = "hold"
                        sig.reason += (
                            f" [DOWNGRADED: altcoin vol {alt_ratio:.1f}x BTC — rotation risk]"
                        )
                        sig.strength = 0.0

        self._last_context = {
            "vol_data": vol_data,
            "rotation": rotation,
        }

        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, **self._last_context}
