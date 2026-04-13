"""Multi-Factor Cross-Sectional Ranking Strategy.

Ranks all tracked crypto assets across multiple derivatives-market factors
and goes long the top-ranked, short the bottom-ranked.

Factors (cross-sectionally z-scored):
    1. Momentum (7-day return)    — higher is better
    2. Funding rate               — more negative (oversold) is better for longs
    3. Taker buy ratio            — higher (aggressive buying) is better
    4. Basis spread               — more negative (backwardation) is better for longs

Composite score = weighted sum of factor z-scores.
Long top-N assets (highest composite), short bottom-N (lowest composite).
"""

import logging
import statistics

import numpy as np

from trading.config import (
    ASTER_SYMBOLS,
    ASTER_SYMBOLS,
    CRYPTO_L1,
    CRYPTO_DEFI,
)
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# Lazy imports for data functions — guarded so the module loads even if
# the data layer is unavailable (e.g. during tests or backtest dry-runs).
try:
    from trading.data.aster import (
        get_funding_rates,
        get_taker_volume_ratio,
        get_basis_spread,
        get_aster_ohlcv,
    )
except ImportError:
    get_funding_rates = None
    get_taker_volume_ratio = None
    get_basis_spread = None
    get_aster_ohlcv = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MULTI_FACTOR_RANK = {
    "factor_weights": {
        "momentum": 0.35,
        "funding": 0.25,
        "taker_flow": 0.20,
        "basis": 0.20,
    },
    "top_n": 3,
    "bottom_n": 3,
    "min_assets": 5,
    "rebalance_threshold": 0.5,
    "momentum_days": 7,
    "ohlcv_interval": "4h",
    "ohlcv_limit": 42,  # 7 days of 4h candles
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cross_sectional_zscore(values: dict[str, float]) -> dict[str, float]:
    """Z-score a dict of {key: raw_value} across all entries.

    Uses population std (ddof=0) since we are scoring the entire universe,
    not a sample.
    """
    if len(values) < 2:
        return {k: 0.0 for k in values}

    arr = list(values.values())
    mean = statistics.mean(arr)
    std = statistics.pstdev(arr)  # population std
    if std == 0:
        return {k: 0.0 for k in values}
    return {k: (v - mean) / std for k, v in values.items()}


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

@register
class MultiFactorRankStrategy(Strategy):
    """Cross-sectional multi-factor ranking across crypto assets.

    Uses derivatives-market microstructure factors (funding, taker flow,
    basis) alongside price momentum to rank assets and generate
    long/short signals.
    """

    name = "multi_factor_rank"

    def __init__(self):
        cfg = MULTI_FACTOR_RANK
        self.weights = cfg["factor_weights"]
        self.top_n = cfg["top_n"]
        self.bottom_n = cfg["bottom_n"]
        self.min_assets = cfg["min_assets"]
        self.rebalance_threshold = cfg["rebalance_threshold"]
        self.momentum_days = cfg["momentum_days"]
        self.ohlcv_interval = cfg["ohlcv_interval"]
        self.ohlcv_limit = cfg["ohlcv_limit"]
        self._universe = self._build_universe()
        self._last_context: dict = {}

    # ------------------------------------------------------------------
    # Universe construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_universe() -> list[str]:
        """Build the tradeable universe from L1 + DeFi coins.

        Skips meme coins for ranking stability.  Only includes coins
        that exist in both ASTER_SYMBOLS and ASTER_SYMBOLS.
        """
        universe = []
        for coin_id in CRYPTO_L1 + CRYPTO_DEFI:
            if coin_id in ASTER_SYMBOLS and coin_id in ASTER_SYMBOLS:
                universe.append(coin_id)
        return universe

    # ------------------------------------------------------------------
    # Factor computation
    # ------------------------------------------------------------------

    def _compute_momentum(self, coin_id: str) -> float | None:
        """Compute 7-day return from OHLCV data."""
        if get_aster_ohlcv is None:
            return None

        aster_sym = ASTER_SYMBOLS[coin_id]
        df = get_aster_ohlcv(aster_sym, interval=self.ohlcv_interval,
                             limit=self.ohlcv_limit)
        if df.empty or len(df) < 2:
            return None

        close = df["close"]
        # Use first and last close for period return
        first_close = float(close.iloc[0])
        last_close = float(close.iloc[-1])
        if first_close <= 0:
            return None
        return (last_close / first_close) - 1.0

    def _fetch_funding_rates(self) -> dict[str, float]:
        """Fetch funding rates for all universe coins."""
        if get_funding_rates is None:
            return {}
        rates = get_funding_rates()
        if not rates:
            return {}
        # Filter to universe only
        return {cid: rate for cid, rate in rates.items()
                if cid in self._universe}

    def _fetch_taker_ratio(self, coin_id: str) -> float | None:
        """Fetch taker buy ratio for a single asset."""
        if get_taker_volume_ratio is None:
            return None
        aster_sym = ASTER_SYMBOLS[coin_id]
        result = get_taker_volume_ratio(aster_sym)
        if result is None:
            return None
        return result.get("buy_ratio")

    def _fetch_basis(self, coin_id: str) -> float | None:
        """Fetch basis spread percentage for a single asset."""
        if get_basis_spread is None:
            return None
        aster_sym = ASTER_SYMBOLS[coin_id]
        result = get_basis_spread(aster_sym)
        if result is None or not isinstance(result, dict):
            return None
        basis = result.get("basis_pct")
        if basis is None or basis == 0:
            # basis_pct == 0 could mean missing data
            return None
        return float(basis)

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def generate_signals(self) -> list[Signal]:
        raw_momentum: dict[str, float] = {}
        raw_funding: dict[str, float] = {}
        raw_taker: dict[str, float] = {}
        raw_basis: dict[str, float] = {}

        # Fetch funding rates in bulk (single API call)
        funding_rates = self._fetch_funding_rates()

        # Collect per-asset factor data
        for coin_id in self._universe:
            # Momentum
            mom = self._compute_momentum(coin_id)
            if mom is not None:
                raw_momentum[coin_id] = mom

            # Funding rate
            if coin_id in funding_rates:
                raw_funding[coin_id] = funding_rates[coin_id]

            # Taker buy ratio
            taker = self._fetch_taker_ratio(coin_id)
            if taker is not None:
                raw_taker[coin_id] = taker

            # Basis spread
            basis = self._fetch_basis(coin_id)
            if basis is not None:
                raw_basis[coin_id] = basis

        # Determine which assets have enough factor coverage to rank
        # Require at least momentum + one other factor
        scoreable = set()
        for coin_id in self._universe:
            factor_count = sum([
                coin_id in raw_momentum,
                coin_id in raw_funding,
                coin_id in raw_taker,
                coin_id in raw_basis,
            ])
            if coin_id in raw_momentum and factor_count >= 2:
                scoreable.add(coin_id)

        if len(scoreable) < self.min_assets:
            self._last_context = {
                "error": f"Only {len(scoreable)} assets scoreable, "
                         f"need {self.min_assets}",
            }
            return [Signal(
                strategy=self.name,
                symbol="BTC/USD",
                action="hold",
                strength=0.0,
                reason=(f"Insufficient data: {len(scoreable)} scoreable assets, "
                        f"need {self.min_assets}"),
            )]

        # Cross-sectional z-scores per factor
        z_momentum = _cross_sectional_zscore(
            {c: raw_momentum[c] for c in scoreable if c in raw_momentum}
        )
        # Funding: negate so more negative funding = higher z-score (better for longs)
        z_funding_raw = _cross_sectional_zscore(
            {c: raw_funding[c] for c in scoreable if c in raw_funding}
        )
        z_funding = {c: -v for c, v in z_funding_raw.items()}

        z_taker = _cross_sectional_zscore(
            {c: raw_taker[c] for c in scoreable if c in raw_taker}
        )
        # Basis: negate so more negative basis (backwardation) = higher z-score
        z_basis_raw = _cross_sectional_zscore(
            {c: raw_basis[c] for c in scoreable if c in raw_basis}
        )
        z_basis = {c: -v for c, v in z_basis_raw.items()}

        # Composite score — weighted sum of available factors
        composite: dict[str, float] = {}
        factor_detail: dict[str, dict] = {}

        for coin_id in scoreable:
            weighted_sum = 0.0
            total_weight = 0.0
            factors = {}

            if coin_id in z_momentum:
                w = self.weights["momentum"]
                weighted_sum += w * z_momentum[coin_id]
                total_weight += w
                factors["momentum_z"] = round(z_momentum[coin_id], 4)
                factors["momentum_raw"] = round(raw_momentum[coin_id], 4)

            if coin_id in z_funding:
                w = self.weights["funding"]
                weighted_sum += w * z_funding[coin_id]
                total_weight += w
                factors["funding_z"] = round(z_funding[coin_id], 4)
                factors["funding_raw"] = round(raw_funding[coin_id], 6)

            if coin_id in z_taker:
                w = self.weights["taker_flow"]
                weighted_sum += w * z_taker[coin_id]
                total_weight += w
                factors["taker_z"] = round(z_taker[coin_id], 4)
                factors["taker_raw"] = round(raw_taker[coin_id], 4)

            if coin_id in z_basis:
                w = self.weights["basis"]
                weighted_sum += w * z_basis[coin_id]
                total_weight += w
                factors["basis_z"] = round(z_basis[coin_id], 4)
                factors["basis_raw"] = round(raw_basis[coin_id], 4)

            # Normalize by actual weight used (handles missing factors)
            if total_weight > 0:
                score = weighted_sum / total_weight
            else:
                score = 0.0

            composite[coin_id] = score
            factor_detail[coin_id] = factors

        # Rank by composite score
        ranked = sorted(composite.items(), key=lambda x: x[1], reverse=True)
        n_assets = len(ranked)

        # Identify longs (top-N) and shorts (bottom-N)
        long_set = set()
        short_set = set()
        for i, (coin_id, score) in enumerate(ranked):
            rank = i + 1
            if rank <= self.top_n and score >= self.rebalance_threshold:
                long_set.add(coin_id)
            elif rank > n_assets - self.bottom_n and score <= -self.rebalance_threshold:
                short_set.add(coin_id)

        # Generate signals
        signals: list[Signal] = []
        context_data: dict[str, dict] = {}

        for i, (coin_id, score) in enumerate(ranked):
            rank = i + 1
            symbol = ASTER_SYMBOLS[coin_id]
            strength = min(abs(score) / 2.0, 1.0)

            detail = {
                "composite_score": round(score, 4),
                "rank": rank,
                "total_ranked": n_assets,
                "factor_scores": factor_detail.get(coin_id, {}),
            }
            context_data[coin_id] = detail

            if coin_id in long_set:
                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    action="buy",
                    strength=strength,
                    reason=(f"{coin_id} ranked #{rank}/{n_assets} "
                            f"composite={score:.3f} — long"),
                    data=detail,
                ))
            elif coin_id in short_set:
                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    action="sell",
                    strength=strength,
                    reason=(f"{coin_id} ranked #{rank}/{n_assets} "
                            f"composite={score:.3f} — short"),
                    data=detail,
                ))
            else:
                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    action="hold",
                    strength=0.0,
                    reason=(f"{coin_id} ranked #{rank}/{n_assets} "
                            f"composite={score:.3f} — neutral"),
                    data=detail,
                ))

        self._last_context = context_data
        return signals

    # ------------------------------------------------------------------
    # Market context
    # ------------------------------------------------------------------

    def get_market_context(self) -> dict:
        return {
            "strategy": self.name,
            "universe_size": len(self._universe),
            "factor_weights": self.weights,
            "rankings": self._last_context,
        }
