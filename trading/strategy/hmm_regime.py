"""HMM Regime Strategy — detect bull/bear/sideways regimes via Hidden Markov Models."""

import logging

import numpy as np

from trading.config import CRYPTO_SYMBOLS
from trading.data.crypto import get_ohlc
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

HMM_REGIME = {
    "n_components": 3,
    "training_days": 365,
    "coins": ["bitcoin", "ethereum", "solana"],
    "min_data_points": 100,
}

# Regime labels assigned after sorting states by mean return.
_REGIME_LABELS = {0: "bear", 1: "sideways", 2: "bull"}


def _build_features(close: "pd.Series") -> np.ndarray | None:
    """Return (n, 2) array of [daily_return, rolling_volatility].

    Returns None if there are fewer valid rows than HMM_REGIME['min_data_points'].
    """
    returns = close.pct_change().dropna()
    volatility = returns.rolling(window=20).std().dropna()

    # Align to the shorter series (volatility has 19 fewer rows).
    n = len(volatility)
    if n < HMM_REGIME["min_data_points"]:
        return None

    ret_aligned = returns.iloc[-n:].values
    vol_aligned = volatility.values

    features = np.column_stack([ret_aligned, vol_aligned])

    # Drop any row that still contains NaN/Inf.
    mask = np.isfinite(features).all(axis=1)
    features = features[mask]

    if len(features) < HMM_REGIME["min_data_points"]:
        return None

    return features


def _fit_hmm(features: np.ndarray):
    """Fit a GaussianHMM and return (model, regime_label, regime_probability).

    Returns (None, None, None) on failure.
    """
    try:
        from hmmlearn.hmm import GaussianHMM
    except ImportError:
        log.warning("hmmlearn not installed — HMM regime strategy disabled")
        return None, None, None

    try:
        model = GaussianHMM(
            n_components=HMM_REGIME["n_components"],
            covariance_type="full",
            n_iter=100,
            random_state=42,
        )
        model.fit(features)

        # Predict the state sequence and grab the last state + its probability.
        states = model.predict(features)
        posteriors = model.predict_proba(features)

        current_state = states[-1]
        current_probs = posteriors[-1]

        # Sort states by their mean return (first feature column) so that
        # state 0 = lowest mean return (bear), state 2 = highest (bull).
        mean_returns = model.means_[:, 0]
        sorted_indices = np.argsort(mean_returns)  # ascending
        rank = {state: rank for rank, state in enumerate(sorted_indices)}

        regime_rank = rank[current_state]
        regime_label = _REGIME_LABELS[regime_rank]
        regime_prob = float(current_probs[current_state])

        return model, regime_label, regime_prob

    except Exception as exc:
        log.error("HMM fitting failed: %s", exc)
        return None, None, None


@register
class HMMRegimeStrategy(Strategy):
    """Trade based on HMM-detected market regimes (bull / bear / sideways)."""

    name = "hmm_regime"

    def __init__(self):
        self.coins = HMM_REGIME["coins"]
        self.training_days = HMM_REGIME["training_days"]
        self._last_context = {}

    def generate_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        context_data: dict = {}

        # Train the HMM on BTC data — the regime applies market-wide.
        regime_label, regime_prob = self._detect_regime(context_data)

        for coin_id in self.coins:
            try:
                alpaca_symbol = CRYPTO_SYMBOLS.get(coin_id)
                if not alpaca_symbol:
                    continue

                ohlc = get_ohlc(coin_id, self.training_days)
                if ohlc.empty or len(ohlc) < HMM_REGIME["min_data_points"]:
                    signals.append(Signal(
                        strategy=self.name, symbol=alpaca_symbol, action="hold",
                        strength=0.0,
                        reason=f"{coin_id} insufficient data ({len(ohlc) if not ohlc.empty else 0} rows)",
                    ))
                    continue

                price = round(float(ohlc["close"].iloc[-1]), 2)
                context_data[coin_id] = {"price": price, "regime": regime_label, "regime_prob": regime_prob}

                if regime_label == "bull":
                    strength = min(regime_prob, 1.0)
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="buy",
                        strength=strength,
                        reason=f"{coin_id} HMM bull regime (prob {regime_prob:.0%})",
                        data={"regime": regime_label, "regime_prob": regime_prob, "coin": coin_id},
                    ))
                elif regime_label == "bear":
                    strength = min(regime_prob, 1.0)
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="sell",
                        strength=strength,
                        reason=f"{coin_id} HMM bear regime (prob {regime_prob:.0%})",
                        data={"regime": regime_label, "regime_prob": regime_prob, "coin": coin_id},
                    ))
                else:
                    # sideways or unknown
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="hold",
                        strength=0.0,
                        reason=f"{coin_id} HMM sideways regime (prob {regime_prob:.0%})",
                        data={"regime": regime_label, "regime_prob": regime_prob, "coin": coin_id},
                    ))

            except Exception as e:
                sym = CRYPTO_SYMBOLS.get(coin_id, "BTC/USD")
                signals.append(Signal(
                    strategy=self.name, symbol=sym, action="hold",
                    strength=0.0, reason=f"{coin_id} HMM error: {e}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_regime(self, context_data: dict) -> tuple[str, float]:
        """Train HMM on BTC and return (regime_label, regime_probability).

        Falls back to ("sideways", 0.0) on any failure.
        """
        fallback = ("sideways", 0.0)

        try:
            btc_ohlc = get_ohlc("bitcoin", self.training_days)
            if btc_ohlc.empty or len(btc_ohlc) < HMM_REGIME["min_data_points"]:
                log.warning("BTC data too short for HMM training (%d rows)", len(btc_ohlc) if not btc_ohlc.empty else 0)
                return fallback

            features = _build_features(btc_ohlc["close"])
            if features is None:
                log.warning("Insufficient valid features for HMM training")
                return fallback

            model, regime_label, regime_prob = _fit_hmm(features)
            if model is None:
                return fallback

            context_data["btc_regime"] = {
                "label": regime_label,
                "probability": regime_prob,
                "training_samples": len(features),
            }

            return regime_label, regime_prob

        except Exception as exc:
            log.error("Regime detection failed: %s", exc)
            return fallback
