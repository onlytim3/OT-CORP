"""Meme Momentum Strategy — rides momentum in meme coins using volume and
taker flow confirmation on AsterDex perpetual futures.

Signal logic:
  - Strong BUY:   momentum > 5% AND volume surge > 2x AND taker buy ratio > 0.55
  - Moderate BUY: momentum > 3% AND volume surge > 1.5x
  - SELL:         momentum < -5% AND volume surge > 2x (panic selling)
  - HOLD:         no conviction

Meme coins are high beta — require stronger confirmation signals than majors.
Uses 12h momentum for faster reaction alongside 24h lookback for volume baseline.
"""

import logging

import numpy as np

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
MOMENTUM_THRESHOLD = 0.03          # 3% for moderate signal
STRONG_MOMENTUM_THRESHOLD = 0.05   # 5% for strong signal
VOLUME_SURGE_THRESHOLD = 1.5       # 1.5x average volume for moderate
STRONG_VOLUME_SURGE = 2.0          # 2x average volume for strong
TAKER_CONFIRM = 0.55               # Taker buy ratio threshold for strong signal
LOOKBACK_HOURS = 24                # Hours of kline data for momentum calc
VOLUME_BASELINE_HOURS = 168        # 7 days of hourly candles for avg volume


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_coin_data(aster_symbol: str) -> dict | None:
    """Fetch hourly klines and compute momentum + volume surge.

    Returns dict with: price, momentum_24h, momentum_12h, volume_surge,
    current_volume, avg_volume — or None on failure.
    """
    try:
        from trading.data.aster import get_aster_ohlcv
    except ImportError:
        log.error("Cannot import get_aster_ohlcv — aster data layer unavailable")
        return None

    try:
        # Fetch 7 days of hourly candles for volume baseline
        df = get_aster_ohlcv(aster_symbol, interval="1h", limit=VOLUME_BASELINE_HOURS)
    except Exception as exc:
        log.warning("Failed to fetch OHLCV for %s: %s", aster_symbol, exc)
        return None

    if df is None or df.empty or len(df) < LOOKBACK_HOURS:
        log.debug("Insufficient data for %s: got %d candles, need %d",
                  aster_symbol, len(df) if df is not None else 0, LOOKBACK_HOURS)
        return None

    closes = df["close"].values
    volumes = df["volume"].values

    current_price = float(closes[-1])

    # 24h momentum (price change over last 24 candles)
    if len(closes) >= LOOKBACK_HOURS + 1:
        price_24h_ago = float(closes[-(LOOKBACK_HOURS + 1)])
        momentum_24h = (current_price - price_24h_ago) / price_24h_ago
    else:
        price_start = float(closes[0])
        momentum_24h = (current_price - price_start) / price_start if price_start > 0 else 0.0

    # 12h momentum (faster reaction for meme coins)
    lookback_12h = 12
    if len(closes) >= lookback_12h + 1:
        price_12h_ago = float(closes[-(lookback_12h + 1)])
        momentum_12h = (current_price - price_12h_ago) / price_12h_ago
    else:
        momentum_12h = momentum_24h  # fallback to 24h

    # Volume surge: current 24h volume vs 7d average 24h volume
    recent_vol = float(np.sum(volumes[-LOOKBACK_HOURS:]))

    # 7d average daily volume (total volume / 7)
    total_vol = float(np.sum(volumes))
    days_of_data = len(volumes) / 24.0
    avg_daily_vol = total_vol / days_of_data if days_of_data > 0 else 0.0

    volume_surge = recent_vol / avg_daily_vol if avg_daily_vol > 0 else 0.0

    return {
        "price": current_price,
        "momentum_24h": float(momentum_24h),
        "momentum_12h": float(momentum_12h),
        "volume_surge": float(volume_surge),
        "current_volume": recent_vol,
        "avg_volume": avg_daily_vol,
    }


def _fetch_taker_ratio(aster_symbol: str) -> float | None:
    """Fetch taker buy ratio for a symbol. Returns buy_ratio or None."""
    try:
        from trading.data.aster import get_taker_volume_ratio
    except ImportError:
        log.error("Cannot import get_taker_volume_ratio — aster data layer unavailable")
        return None

    try:
        result = get_taker_volume_ratio(aster_symbol, interval="1h", limit=LOOKBACK_HOURS)
        if result and "buy_ratio" in result:
            return float(result["buy_ratio"])
    except Exception as exc:
        log.warning("Failed to fetch taker ratio for %s: %s", aster_symbol, exc)

    return None


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

@register
class MemeMomentumStrategy(Strategy):
    """Ride momentum in meme coins with volume and taker flow confirmation."""

    name = "meme_momentum"

    def __init__(self):
        self._last_context: dict = {}

    def generate_signals(self) -> list[Signal]:
        try:
            from trading.config import CRYPTO_MEME, ASTER_SYMBOLS, CRYPTO_SYMBOLS
        except ImportError:
            log.error("Cannot import config — symbol mappings unavailable")
            return [self._hold("Config import failed")]

        signals: list[Signal] = []
        coin_data: dict[str, dict] = {}

        for coin_id in CRYPTO_MEME:
            aster_sym = ASTER_SYMBOLS.get(coin_id)
            if not aster_sym:
                log.debug("No AsterDex symbol for %s — skipping", coin_id)
                continue

            data = _fetch_coin_data(aster_sym)
            if data is None:
                continue

            # Fetch taker buy ratio for confirmation
            taker_ratio = _fetch_taker_ratio(aster_sym)
            data["taker_buy_ratio"] = taker_ratio

            coin_data[coin_id] = data

            signal_symbol = CRYPTO_SYMBOLS.get(coin_id)
            if not signal_symbol:
                continue

            # Use 12h momentum for faster meme reaction
            momentum = data["momentum_12h"]
            vol_surge = data["volume_surge"]

            # ----- Strong BUY: momentum > 5%, volume > 2x, taker > 0.55 -----
            if (momentum > STRONG_MOMENTUM_THRESHOLD
                    and vol_surge > STRONG_VOLUME_SURGE
                    and taker_ratio is not None
                    and taker_ratio > TAKER_CONFIRM):
                strength = min(
                    0.5 + (momentum - STRONG_MOMENTUM_THRESHOLD) * 5
                    + (vol_surge - STRONG_VOLUME_SURGE) * 0.1
                    + (taker_ratio - TAKER_CONFIRM) * 2,
                    0.90,
                )
                signals.append(Signal(
                    strategy=self.name,
                    symbol=signal_symbol,
                    action="buy",
                    strength=round(strength, 2),
                    reason=(
                        f"Strong meme momentum: {coin_id} +{momentum:.1%} (12h), "
                        f"vol surge {vol_surge:.1f}x, taker buy {taker_ratio:.2f}"
                    ),
                    data={
                        "signal_type": "strong_meme_buy",
                        "coin_id": coin_id,
                        "momentum_12h": round(momentum, 4),
                        "momentum_24h": round(data["momentum_24h"], 4),
                        "volume_surge": round(vol_surge, 2),
                        "taker_buy_ratio": round(taker_ratio, 4),
                    },
                ))

            # ----- Moderate BUY: momentum > 3%, volume > 1.5x -----
            elif (momentum > MOMENTUM_THRESHOLD
                    and vol_surge > VOLUME_SURGE_THRESHOLD):
                strength = min(
                    0.3 + (momentum - MOMENTUM_THRESHOLD) * 4
                    + (vol_surge - VOLUME_SURGE_THRESHOLD) * 0.1,
                    0.65,
                )
                taker_note = ""
                if taker_ratio is not None:
                    taker_note = f", taker buy {taker_ratio:.2f}"
                signals.append(Signal(
                    strategy=self.name,
                    symbol=signal_symbol,
                    action="buy",
                    strength=round(strength, 2),
                    reason=(
                        f"Meme momentum: {coin_id} +{momentum:.1%} (12h), "
                        f"vol surge {vol_surge:.1f}x{taker_note}"
                    ),
                    data={
                        "signal_type": "moderate_meme_buy",
                        "coin_id": coin_id,
                        "momentum_12h": round(momentum, 4),
                        "momentum_24h": round(data["momentum_24h"], 4),
                        "volume_surge": round(vol_surge, 2),
                        "taker_buy_ratio": round(taker_ratio, 4) if taker_ratio else None,
                    },
                ))

            # ----- SELL: momentum < -5%, volume > 2x (panic selling) -----
            elif (momentum < -STRONG_MOMENTUM_THRESHOLD
                    and vol_surge > STRONG_VOLUME_SURGE):
                strength = min(
                    0.5 + (abs(momentum) - STRONG_MOMENTUM_THRESHOLD) * 5
                    + (vol_surge - STRONG_VOLUME_SURGE) * 0.1,
                    0.85,
                )
                signals.append(Signal(
                    strategy=self.name,
                    symbol=signal_symbol,
                    action="sell",
                    strength=round(strength, 2),
                    reason=(
                        f"Meme panic sell: {coin_id} {momentum:.1%} (12h), "
                        f"vol surge {vol_surge:.1f}x — high-volume dump"
                    ),
                    data={
                        "signal_type": "meme_panic_sell",
                        "coin_id": coin_id,
                        "momentum_12h": round(momentum, 4),
                        "momentum_24h": round(data["momentum_24h"], 4),
                        "volume_surge": round(vol_surge, 2),
                        "taker_buy_ratio": round(taker_ratio, 4) if taker_ratio else None,
                    },
                ))

        # Store context
        self._last_context = {
            "coins_analyzed": len(coin_data),
            "coins_with_signals": len(signals),
            "coin_metrics": {
                cid: {
                    "momentum_12h": round(d["momentum_12h"], 4),
                    "momentum_24h": round(d["momentum_24h"], 4),
                    "volume_surge": round(d["volume_surge"], 2),
                    "taker_buy_ratio": round(d["taker_buy_ratio"], 4) if d["taker_buy_ratio"] else None,
                }
                for cid, d in coin_data.items()
            },
        }

        if not signals:
            return [self._hold("No meme momentum conviction")]

        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, **self._last_context}

    # ---- internals --------------------------------------------------------

    def _hold(self, reason: str) -> Signal:
        return Signal(
            strategy=self.name,
            symbol="DOGE/USD",
            action="hold",
            strength=0.0,
            reason=reason,
        )
