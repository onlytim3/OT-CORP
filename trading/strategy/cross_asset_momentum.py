"""Cross-Asset Momentum Strategy — detects momentum divergences across crypto,
stocks, commodities, and indices, all traded as AsterDex perpetual futures.

Signal logic:
  1. Gold rising + BTC falling          -> risk-off, sell crypto
  2. SPX/QQQ rising + crypto lagging    -> rotation into crypto likely, buy
  3. All risk assets falling + gold up   -> flight to safety, sell all risk
  4. Crypto outperforming stocks heavily -> potential overextension, reduce
  5. Gold + silver + copper all rising   -> commodity super-cycle, buy BTC + gold
"""

import logging

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Asset groups (internal IDs that map into ASTER_SYMBOLS and ASTER_SYMBOLS)
# ---------------------------------------------------------------------------
CRYPTO_IDS = ["bitcoin", "ethereum", "solana"]
STOCK_IDS = ["apple", "nvidia", "tesla", "sp500"]
COMMODITY_IDS = ["gold", "silver", "copper"]
INDEX_IDS = ["sp500", "nasdaq100"]

# All unique IDs across every group (sp500 appears in both stocks and indices)
_ALL_IDS = list(dict.fromkeys(CRYPTO_IDS + STOCK_IDS + COMMODITY_IDS + INDEX_IDS))

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
MOMENTUM_24H_THRESHOLD = 0.02   # 2% daily move is significant
MOMENTUM_7D_THRESHOLD = 0.05    # 5% weekly move is significant
DIVERGENCE_THRESHOLD = 0.03     # 3% divergence between asset classes
LOOKBACK_DAYS = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_momentum(aster_symbol: str) -> dict | None:
    """Fetch klines from AsterDex and compute 24h and 7d momentum.

    Returns dict with keys: price, mom_24h, mom_7d  — or None on failure.
    """
    try:
        from trading.execution.aster_client import get_aster_klines
    except ImportError:
        log.error("Cannot import get_aster_klines — aster_client unavailable")
        return None

    try:
        df = get_aster_klines(aster_symbol, interval="1d", limit=30)
    except Exception as exc:
        log.warning("Failed to fetch klines for %s: %s", aster_symbol, exc)
        return None

    if df is None or df.empty:
        log.warning("Empty klines for %s", aster_symbol)
        return None

    closes = df["close"]
    if len(closes) < 2:
        return None

    current = closes.iloc[-1]

    # 24h momentum (last vs previous close)
    mom_24h = (closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2] if len(closes) >= 2 else 0.0

    # 7d momentum (last vs close 7 days ago)
    if len(closes) >= 8:
        mom_7d = (closes.iloc[-1] - closes.iloc[-8]) / closes.iloc[-8]
    else:
        # Use whatever history is available
        mom_7d = (closes.iloc[-1] - closes.iloc[0]) / closes.iloc[0]

    return {"price": float(current), "mom_24h": float(mom_24h), "mom_7d": float(mom_7d)}


def _avg_momentum(momenta: list[dict], key: str) -> float:
    """Average a momentum field across a list of momentum dicts."""
    vals = [m[key] for m in momenta if m is not None]
    return sum(vals) / len(vals) if vals else 0.0


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

@register
class CrossAssetMomentumStrategy(Strategy):
    """Detect momentum divergences between crypto, stocks, commodities, and
    indices — all sourced from AsterDex perpetual futures klines.
    """

    name = "cross_asset_momentum"

    def __init__(self):
        self._last_context: dict = {}

    # ---- public API -------------------------------------------------------

    def generate_signals(self) -> list[Signal]:
        try:
            from trading.config import ASTER_SYMBOLS
        except ImportError:
            log.error("Cannot import symbol mappings from trading.config")
            return [self._hold("Symbol config unavailable")]

        # -- Fetch momentum for every asset group ---------------------------
        crypto_mom: dict[str, dict] = {}
        stock_mom: dict[str, dict] = {}
        commodity_mom: dict[str, dict] = {}
        index_mom: dict[str, dict] = {}

        for asset_id in _ALL_IDS:
            aster_sym = ASTER_SYMBOLS.get(asset_id)
            if not aster_sym:
                log.debug("No AsterDex symbol for %s — skipping", asset_id)
                continue
            data = _fetch_momentum(aster_sym)
            if data is None:
                continue

            if asset_id in CRYPTO_IDS:
                crypto_mom[asset_id] = data
            if asset_id in STOCK_IDS:
                stock_mom[asset_id] = data
            if asset_id in COMMODITY_IDS:
                commodity_mom[asset_id] = data
            if asset_id in INDEX_IDS:
                index_mom[asset_id] = data

        # Guard: need at least some data from two groups
        groups_with_data = sum(1 for g in (crypto_mom, stock_mom, commodity_mom, index_mom) if g)
        if groups_with_data < 2:
            return [self._hold("Insufficient cross-asset data (need >= 2 groups)")]

        # -- Compute group averages -----------------------------------------
        avg_crypto_24h = _avg_momentum(list(crypto_mom.values()), "mom_24h")
        avg_crypto_7d = _avg_momentum(list(crypto_mom.values()), "mom_7d")
        avg_stock_24h = _avg_momentum(list(stock_mom.values()), "mom_24h")
        avg_stock_7d = _avg_momentum(list(stock_mom.values()), "mom_7d")
        avg_commodity_24h = _avg_momentum(list(commodity_mom.values()), "mom_24h")
        avg_commodity_7d = _avg_momentum(list(commodity_mom.values()), "mom_7d")
        avg_index_24h = _avg_momentum(list(index_mom.values()), "mom_24h")
        avg_index_7d = _avg_momentum(list(index_mom.values()), "mom_7d")

        gold_mom = commodity_mom.get("gold")
        silver_mom = commodity_mom.get("silver")
        copper_mom = commodity_mom.get("copper")
        btc_mom = crypto_mom.get("bitcoin")

        # -- Store context for get_market_context() -------------------------
        self._last_context = {
            "crypto_24h": round(avg_crypto_24h, 4),
            "crypto_7d": round(avg_crypto_7d, 4),
            "stocks_24h": round(avg_stock_24h, 4),
            "stocks_7d": round(avg_stock_7d, 4),
            "commodities_24h": round(avg_commodity_24h, 4),
            "commodities_7d": round(avg_commodity_7d, 4),
            "indices_24h": round(avg_index_24h, 4),
            "indices_7d": round(avg_index_7d, 4),
            "assets_fetched": {
                "crypto": list(crypto_mom.keys()),
                "stocks": list(stock_mom.keys()),
                "commodities": list(commodity_mom.keys()),
                "indices": list(index_mom.keys()),
            },
        }

        signals: list[Signal] = []

        # ------------------------------------------------------------------
        # Signal 1: Gold rising + BTC falling -> risk-off, sell crypto
        # ------------------------------------------------------------------
        if gold_mom and btc_mom:
            if (gold_mom["mom_24h"] > MOMENTUM_24H_THRESHOLD
                    and btc_mom["mom_24h"] < -MOMENTUM_24H_THRESHOLD):
                divergence = gold_mom["mom_24h"] - btc_mom["mom_24h"]
                strength = min(abs(divergence) / DIVERGENCE_THRESHOLD * 0.3, 0.85)
                for cid in crypto_mom:
                    sym = ASTER_SYMBOLS.get(cid)
                    if sym:
                        signals.append(Signal(
                            strategy=self.name,
                            symbol=sym,
                            action="sell",
                            strength=round(strength, 2),
                            reason=(
                                f"Risk-off: gold +{gold_mom['mom_24h']:.1%} vs "
                                f"BTC {btc_mom['mom_24h']:.1%} (24h divergence)"
                            ),
                            data={
                                "signal_type": "risk_off_gold_btc",
                                "gold_24h": round(gold_mom["mom_24h"], 4),
                                "btc_24h": round(btc_mom["mom_24h"], 4),
                                "divergence": round(divergence, 4),
                            },
                        ))

        # ------------------------------------------------------------------
        # Signal 2: SPX/QQQ rising + crypto lagging -> rotation buy crypto
        # ------------------------------------------------------------------
        if index_mom and crypto_mom:
            if (avg_index_7d > MOMENTUM_7D_THRESHOLD
                    and avg_crypto_7d < avg_index_7d - DIVERGENCE_THRESHOLD):
                lag = avg_index_7d - avg_crypto_7d
                strength = min(lag / DIVERGENCE_THRESHOLD * 0.25, 0.80)
                for cid in crypto_mom:
                    sym = ASTER_SYMBOLS.get(cid)
                    if sym:
                        signals.append(Signal(
                            strategy=self.name,
                            symbol=sym,
                            action="buy",
                            strength=round(strength, 2),
                            reason=(
                                f"Rotation: indices +{avg_index_7d:.1%} (7d) vs "
                                f"crypto {avg_crypto_7d:.1%} — catch-up likely"
                            ),
                            data={
                                "signal_type": "equity_crypto_rotation",
                                "index_7d": round(avg_index_7d, 4),
                                "crypto_7d": round(avg_crypto_7d, 4),
                                "lag": round(lag, 4),
                            },
                        ))

        # ------------------------------------------------------------------
        # Signal 3: All risk assets falling + gold rising -> flight to safety
        # ------------------------------------------------------------------
        if gold_mom and crypto_mom and stock_mom:
            risk_falling = (
                avg_crypto_24h < -MOMENTUM_24H_THRESHOLD
                and avg_stock_24h < -MOMENTUM_24H_THRESHOLD
                and gold_mom["mom_24h"] > MOMENTUM_24H_THRESHOLD
            )
            if risk_falling:
                strength = min(
                    (abs(avg_crypto_24h) + abs(avg_stock_24h)) / (2 * DIVERGENCE_THRESHOLD) * 0.3,
                    0.90,
                )
                # Sell all crypto
                for cid in crypto_mom:
                    sym = ASTER_SYMBOLS.get(cid)
                    if sym:
                        signals.append(Signal(
                            strategy=self.name,
                            symbol=sym,
                            action="sell",
                            strength=round(strength, 2),
                            reason=(
                                f"Flight to safety: crypto {avg_crypto_24h:.1%}, "
                                f"stocks {avg_stock_24h:.1%}, gold +{gold_mom['mom_24h']:.1%} (24h)"
                            ),
                            data={
                                "signal_type": "flight_to_safety",
                                "crypto_24h": round(avg_crypto_24h, 4),
                                "stocks_24h": round(avg_stock_24h, 4),
                                "gold_24h": round(gold_mom["mom_24h"], 4),
                            },
                        ))

        # ------------------------------------------------------------------
        # Signal 4: Crypto outperforming stocks significantly -> reduce
        # ------------------------------------------------------------------
        if crypto_mom and stock_mom:
            outperformance = avg_crypto_7d - avg_stock_7d
            if outperformance > DIVERGENCE_THRESHOLD * 2:
                strength = min(outperformance / (DIVERGENCE_THRESHOLD * 3) * 0.4, 0.70)
                for cid in crypto_mom:
                    sym = ASTER_SYMBOLS.get(cid)
                    if sym:
                        signals.append(Signal(
                            strategy=self.name,
                            symbol=sym,
                            action="sell",
                            strength=round(strength, 2),
                            reason=(
                                f"Overextension: crypto +{avg_crypto_7d:.1%} vs "
                                f"stocks {avg_stock_7d:.1%} (7d) — reduce exposure"
                            ),
                            data={
                                "signal_type": "crypto_overextension",
                                "crypto_7d": round(avg_crypto_7d, 4),
                                "stocks_7d": round(avg_stock_7d, 4),
                                "outperformance": round(outperformance, 4),
                            },
                        ))

        # ------------------------------------------------------------------
        # Signal 5: Commodity super-cycle — gold + silver + copper all rising
        # ------------------------------------------------------------------
        if gold_mom and silver_mom and copper_mom:
            all_rising = (
                gold_mom["mom_7d"] > MOMENTUM_7D_THRESHOLD
                and silver_mom["mom_7d"] > MOMENTUM_7D_THRESHOLD
                and copper_mom["mom_7d"] > MOMENTUM_7D_THRESHOLD
            )
            if all_rising:
                avg_comm_7d = (
                    gold_mom["mom_7d"] + silver_mom["mom_7d"] + copper_mom["mom_7d"]
                ) / 3.0
                strength = min(avg_comm_7d / MOMENTUM_7D_THRESHOLD * 0.3, 0.80)

                # Buy BTC as inflation hedge
                btc_sym = ASTER_SYMBOLS.get("bitcoin")
                if btc_sym:
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=btc_sym,
                        action="buy",
                        strength=round(strength, 2),
                        reason=(
                            f"Commodity super-cycle: gold +{gold_mom['mom_7d']:.1%}, "
                            f"silver +{silver_mom['mom_7d']:.1%}, "
                            f"copper +{copper_mom['mom_7d']:.1%} (7d) — buy inflation hedges"
                        ),
                        data={
                            "signal_type": "commodity_super_cycle",
                            "gold_7d": round(gold_mom["mom_7d"], 4),
                            "silver_7d": round(silver_mom["mom_7d"], 4),
                            "copper_7d": round(copper_mom["mom_7d"], 4),
                        },
                    ))

                # Buy gold perp itself
                gold_sym = ASTER_SYMBOLS.get("gold")
                if gold_sym:
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=gold_sym,
                        action="buy",
                        strength=round(strength, 2),
                        reason=(
                            f"Commodity super-cycle: all metals rising (7d avg "
                            f"+{avg_comm_7d:.1%}) — buy gold"
                        ),
                        data={
                            "signal_type": "commodity_super_cycle",
                            "avg_commodity_7d": round(avg_comm_7d, 4),
                        },
                    ))

        # ------------------------------------------------------------------
        # Deduplicate: if conflicting signals for the same symbol, keep the
        # strongest one.  (e.g. signal 1 sell + signal 2 buy on BTC)
        # ------------------------------------------------------------------
        signals = self._deduplicate(signals)

        if not signals:
            return [self._hold("No cross-asset divergence detected")]

        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, **self._last_context}

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _deduplicate(signals: list[Signal]) -> list[Signal]:
        """When multiple signals target the same symbol, keep the highest-strength one."""
        best: dict[str, Signal] = {}
        for sig in signals:
            key = sig.symbol
            if key not in best or sig.strength > best[key].strength:
                best[key] = sig
        return list(best.values())

    def _hold(self, reason: str) -> Signal:
        return Signal(
            strategy=self.name,
            symbol="BTC/USD",
            action="hold",
            strength=0.0,
            reason=reason,
        )
