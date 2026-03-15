"""Cross-Pair Basis Relative Value Strategy.

Ranks multiple assets by their basis spread (mark vs index price divergence)
and takes relative value positions: long the cheapest basis (backwardation),
short the richest basis (contango).

Signal generation:
1. Fetch basis spread for all tracked AsterDex symbols.
2. Compute cross-sectional z-scores of basis_pct across all assets.
3. Long assets with most negative basis (backwardation = undervalued).
4. Short assets with most positive basis (contango = overvalued).
5. Only trade when spread between richest and cheapest exceeds min_spread_pct.
"""

import logging
from statistics import mean, stdev

from trading.config import ASTER_SYMBOLS, CRYPTO_SYMBOLS
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# Reverse maps for symbol translation
_ASTER_TO_COIN = {v: k for k, v in ASTER_SYMBOLS.items()}

CROSS_BASIS_RV = {
    "min_spread_pct": 0.05,   # Minimum spread between richest and cheapest basis
    "top_n": 2,               # Number of cheapest/richest assets to trade
    "z_threshold": 1.0,       # Minimum cross-sectional z-score for signal
    "min_assets": 3,          # Minimum assets with valid basis data
}


def _aster_to_alpaca(aster_symbol: str) -> str | None:
    """Convert AsterDex symbol to Alpaca-style symbol via coin_id lookup."""
    coin_id = _ASTER_TO_COIN.get(aster_symbol)
    if coin_id is None:
        return None
    return CRYPTO_SYMBOLS.get(coin_id)


@register
class CrossBasisRVStrategy(Strategy):
    """Cross-pair basis relative value: long cheap basis, short rich basis.

    Uses cross-sectional z-scores of mark-index basis spreads to identify
    relative mispricing across perpetual futures contracts.
    """

    name = "cross_basis_rv"

    def __init__(self):
        self._config = CROSS_BASIS_RV
        self._last_context = {}

    def generate_signals(self) -> list[Signal]:
        # Lazy import of data functions
        try:
            from trading.data.aster import get_basis_spread
        except ImportError:
            log.error("Cannot import trading.data.aster — strategy disabled")
            return [Signal(
                strategy=self.name,
                symbol="BTC/USD",
                action="hold",
                strength=0.0,
                reason="Data module unavailable",
            )]

        # Fetch basis spread for all tracked symbols
        try:
            basis_data = get_basis_spread()
        except Exception as e:
            log.warning("Failed to fetch basis spread data: %s", e)
            return [Signal(
                strategy=self.name,
                symbol="BTC/USD",
                action="hold",
                strength=0.0,
                reason=f"Basis data fetch failed: {e}",
            )]

        if not isinstance(basis_data, list) or not basis_data:
            return [Signal(
                strategy=self.name,
                symbol="BTC/USD",
                action="hold",
                strength=0.0,
                reason="No basis spread data available",
            )]

        # Filter to entries with valid prices and translatable symbols
        valid = []
        for entry in basis_data:
            index_price = entry.get("indexPrice", 0)
            if index_price <= 0:
                continue
            alpaca_sym = _aster_to_alpaca(entry.get("symbol", ""))
            if alpaca_sym is None:
                continue
            valid.append({
                "aster_symbol": entry["symbol"],
                "alpaca_symbol": alpaca_sym,
                "basis_pct": float(entry.get("basis_pct", 0)),
                "mark_price": float(entry.get("markPrice", 0)),
                "index_price": float(index_price),
                "funding_rate": float(entry.get("fundingRate", 0)),
            })

        min_assets = self._config["min_assets"]
        if len(valid) < min_assets:
            return [Signal(
                strategy=self.name,
                symbol="BTC/USD",
                action="hold",
                strength=0.0,
                reason=f"Insufficient assets with valid basis data ({len(valid)}/{min_assets})",
            )]

        # Rank by basis_pct (ascending: most negative first)
        valid.sort(key=lambda x: x["basis_pct"])

        cheapest_basis = valid[0]["basis_pct"]
        richest_basis = valid[-1]["basis_pct"]
        total_spread = richest_basis - cheapest_basis

        # Check minimum spread threshold
        min_spread = self._config["min_spread_pct"]
        if total_spread < min_spread:
            self._last_context = {
                "total_spread": round(total_spread, 4),
                "min_spread": min_spread,
                "num_assets": len(valid),
                "reason": "spread_below_threshold",
            }
            return [Signal(
                strategy=self.name,
                symbol="BTC/USD",
                action="hold",
                strength=0.0,
                reason=(
                    f"Basis spread too narrow ({total_spread:.4f}% < {min_spread}% threshold) "
                    f"across {len(valid)} assets"
                ),
            )]

        # Compute cross-sectional statistics
        basis_values = [a["basis_pct"] for a in valid]
        cross_mean = mean(basis_values)
        cross_std = stdev(basis_values) if len(basis_values) > 1 else 0.0

        # Compute z-scores for each asset
        for asset in valid:
            if cross_std > 0:
                asset["z_score"] = (asset["basis_pct"] - cross_mean) / cross_std
            else:
                asset["z_score"] = 0.0

        top_n = self._config["top_n"]
        z_threshold = self._config["z_threshold"]

        # Cheapest basis assets (most negative z-score) -> long candidates
        cheap_candidates = [a for a in valid if a["z_score"] <= -z_threshold]
        cheap_candidates.sort(key=lambda x: x["z_score"])
        cheap_selected = cheap_candidates[:top_n]

        # Richest basis assets (most positive z-score) -> short candidates
        rich_candidates = [a for a in valid if a["z_score"] >= z_threshold]
        rich_candidates.sort(key=lambda x: x["z_score"], reverse=True)
        rich_selected = rich_candidates[:top_n]

        # Update context before generating signals
        self._last_context = {
            "total_spread": round(total_spread, 4),
            "cross_section_mean": round(cross_mean, 4),
            "cross_section_std": round(cross_std, 4),
            "num_assets": len(valid),
            "cheapest": valid[0]["aster_symbol"],
            "cheapest_basis": round(cheapest_basis, 4),
            "richest": valid[-1]["aster_symbol"],
            "richest_basis": round(richest_basis, 4),
            "long_candidates": len(cheap_candidates),
            "short_candidates": len(rich_candidates),
        }

        signals = []

        # Long signals: assets in backwardation (cheap basis)
        for rank, asset in enumerate(cheap_selected):
            # Strength scales with z-score magnitude, capped at 1.0
            raw_strength = min(abs(asset["z_score"]) / 3.0, 1.0)
            strength = round(max(raw_strength, 0.1), 2)

            signals.append(Signal(
                strategy=self.name,
                symbol=asset["alpaca_symbol"],
                action="buy",
                strength=strength,
                reason=(
                    f"Rank #{rank + 1} cheapest basis | "
                    f"basis={asset['basis_pct']:.4f}% | "
                    f"z={asset['z_score']:.2f} | "
                    f"spread={total_spread:.4f}%"
                ),
                data={
                    "basis_pct": round(asset["basis_pct"], 4),
                    "cross_section_mean": round(cross_mean, 4),
                    "cross_section_z": round(asset["z_score"], 4),
                    "rank": rank + 1,
                    "funding_rate": asset["funding_rate"],
                    "mark_price": asset["mark_price"],
                    "index_price": asset["index_price"],
                    "total_spread": round(total_spread, 4),
                },
            ))

        # Short signals: assets in contango (rich basis)
        for rank, asset in enumerate(rich_selected):
            raw_strength = min(abs(asset["z_score"]) / 3.0, 1.0)
            strength = round(max(raw_strength, 0.1), 2)

            signals.append(Signal(
                strategy=self.name,
                symbol=asset["alpaca_symbol"],
                action="sell",
                strength=strength,
                reason=(
                    f"Rank #{rank + 1} richest basis | "
                    f"basis={asset['basis_pct']:.4f}% | "
                    f"z={asset['z_score']:.2f} | "
                    f"spread={total_spread:.4f}%"
                ),
                data={
                    "basis_pct": round(asset["basis_pct"], 4),
                    "cross_section_mean": round(cross_mean, 4),
                    "cross_section_z": round(asset["z_score"], 4),
                    "rank": rank + 1,
                    "funding_rate": asset["funding_rate"],
                    "mark_price": asset["mark_price"],
                    "index_price": asset["index_price"],
                    "total_spread": round(total_spread, 4),
                },
            ))

        # If no assets passed z-score threshold, hold
        if not signals:
            return [Signal(
                strategy=self.name,
                symbol="BTC/USD",
                action="hold",
                strength=0.0,
                reason=(
                    f"No assets exceed z-score threshold ({z_threshold}) | "
                    f"spread={total_spread:.4f}% across {len(valid)} assets"
                ),
            )]

        return signals

    def get_market_context(self) -> dict:
        return {
            "strategy": self.name,
            **self._last_context,
        }
