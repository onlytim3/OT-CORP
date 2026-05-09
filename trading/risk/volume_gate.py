"""Volume gate — blocks entries on low-volume assets and exits dying positions.

Uses relative volume (current vs 7-day average) so thresholds work across
all assets regardless of absolute volume. Fails open on data errors.

Enhanced with:
  - Volume trend detection (is volume fading or building?)
  - Spread-aware liquidity check (wide spread = bad fills)
  - Market impact check (is our order too large for this market?)
  - Volume multiplier for position sizing (scale size with volume)
"""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class VolumeAnalysis:
    """Full volume analysis for a symbol."""
    ratio: float                # Current volume vs 7d average (1.0 = normal)
    trend: float                # Volume slope: positive = building, negative = fading
    spread_bps: float           # Current bid-ask spread in basis points
    recent_quote_volume: float  # Quote volume (USDT) in recent window
    sizing_multiplier: float    # Recommended position size multiplier (0.5–1.5)


def compute_volume_ratio(
    bybit_symbol: str,
    recent_hours: int = 4,
    baseline_hours: int = 168,
) -> Optional[float]:
    """Ratio of recent volume to the asset's 7-day average volume.

    Returns a float (e.g. 0.5 = 50% of normal volume), or None if data
    is unavailable. Callers should treat None as "pass" (fail-open).
    """
    try:
        from trading.data.bybit import get_bybit_ohlcv

        df = get_bybit_ohlcv(bybit_symbol, interval="1h", limit=baseline_hours)
        if df is None or df.empty or len(df) < recent_hours + 24:
            return None

        volumes = df["volume"].values
        recent_volume = float(volumes[-recent_hours:].sum())

        # Average volume in same-sized windows over the baseline period
        total_volume = float(volumes.sum())
        num_windows = len(volumes) / recent_hours
        avg_window_volume = total_volume / num_windows if num_windows > 0 else 0

        if avg_window_volume <= 0:
            return None

        return recent_volume / avg_window_volume
    except Exception:
        log.debug("Failed to compute volume ratio for %s", bybit_symbol, exc_info=True)
        return None


def compute_volume_trend(bybit_symbol: str, window: int = 6) -> Optional[float]:
    """Detect whether volume is building or fading.

    Compares the most recent `window` hours to the `window` hours before that.
    Returns a slope indicator:
      > 0  = volume is increasing (building momentum)
      < 0  = volume is decreasing (fading, move dying)
      ~0   = stable

    Normalized to -1..+1 range. None on failure.
    """
    try:
        from trading.data.bybit import get_bybit_ohlcv

        df = get_bybit_ohlcv(bybit_symbol, interval="1h", limit=window * 2 + 2)
        if df is None or df.empty or len(df) < window * 2:
            return None

        volumes = df["volume"].values
        recent = float(volumes[-window:].sum())
        previous = float(volumes[-window * 2:-window].sum())

        if previous <= 0:
            return None

        # Ratio of recent to previous, normalized to -1..+1
        change = (recent - previous) / previous
        # Clamp to [-1, 1]
        return max(-1.0, min(1.0, change))
    except Exception:
        log.debug("Failed to compute volume trend for %s", bybit_symbol, exc_info=True)
        return None


def check_spread(bybit_symbol: str) -> Optional[float]:
    """Get current bid-ask spread in basis points.

    Returns spread_bps or None on failure. Wide spread = illiquid market.
    """
    try:
        from trading.data.bybit import get_orderbook_imbalance

        book = get_orderbook_imbalance(bybit_symbol, depth=5)
        if book is None:
            return None
        return book.get("spread_bps", None)
    except Exception:
        log.debug("Failed to check spread for %s", bybit_symbol, exc_info=True)
        return None


def check_market_impact(
    bybit_symbol: str,
    order_value_usd: float,
    max_impact_pct: float = 0.01,
) -> Optional[bool]:
    """Check if our order is too large relative to recent market volume.

    Returns True if order is safe (< max_impact_pct of recent quote volume),
    False if order would be a significant portion of the market,
    None on data failure (fail-open).
    """
    try:
        from trading.data.bybit import get_bybit_ohlcv

        df = get_bybit_ohlcv(bybit_symbol, interval="1h", limit=4)
        if df is None or df.empty:
            return None

        recent_quote_vol = float(df["quote_volume"].sum())
        if recent_quote_vol <= 0:
            return None

        impact = order_value_usd / recent_quote_vol
        return impact <= max_impact_pct
    except Exception:
        log.debug("Failed to check market impact for %s", bybit_symbol, exc_info=True)
        return None


def compute_volume_sizing_multiplier(bybit_symbol: str) -> float:
    """Compute a position sizing multiplier based on current volume conditions.

    High volume → bigger positions (up to 1.5x)
    Normal volume → 1.0x
    Low volume → smaller positions (down to 0.5x)
    Data failure → 1.0x (neutral)
    """
    ratio = compute_volume_ratio(bybit_symbol)
    if ratio is None:
        return 1.0

    trend = compute_volume_trend(bybit_symbol) or 0.0

    # Base multiplier from volume ratio
    # ratio=2.0 → 1.4x, ratio=1.0 → 1.0x, ratio=0.5 → 0.7x, ratio=0.3 → 0.5x
    base = max(0.5, min(1.5, 0.4 + 0.6 * ratio))

    # Trend adjustment: fading volume penalizes, building volume rewards
    # trend=+1 → +0.1, trend=-1 → -0.15 (asymmetric — fading is more dangerous)
    if trend > 0:
        trend_adj = trend * 0.10
    else:
        trend_adj = trend * 0.15

    multiplier = max(0.5, min(1.5, base + trend_adj))

    log.debug(
        "Volume sizing %s: ratio=%.2f trend=%.2f → multiplier=%.2f",
        bybit_symbol, ratio, trend, multiplier,
    )
    return round(multiplier, 3)


def full_volume_analysis(bybit_symbol: str) -> Optional[VolumeAnalysis]:
    """Run complete volume analysis for a symbol. Returns None on failure."""
    ratio = compute_volume_ratio(bybit_symbol)
    if ratio is None:
        return None

    trend = compute_volume_trend(bybit_symbol) or 0.0
    spread = check_spread(bybit_symbol) or 0.0

    # Get recent quote volume
    try:
        from trading.data.bybit import get_bybit_ohlcv
        df = get_bybit_ohlcv(bybit_symbol, interval="1h", limit=4)
        quote_vol = float(df["quote_volume"].sum()) if df is not None and not df.empty else 0
    except Exception:
        quote_vol = 0

    sizing = compute_volume_sizing_multiplier(bybit_symbol)

    return VolumeAnalysis(
        ratio=round(ratio, 3),
        trend=round(trend, 3),
        spread_bps=round(spread, 2),
        recent_quote_volume=round(quote_vol, 2),
        sizing_multiplier=sizing,
    )


def record_volume_snapshot(bybit_symbol: str) -> None:
    """Record current hourly volume snapshot for learning.

    Stores hour-of-day and day-of-week so the system can learn
    when volume is typically high or low for each asset.
    """
    try:
        from datetime import datetime, timezone
        from trading.data.bybit import get_bybit_ohlcv
        from trading.db.store import insert_volume_profile

        ratio = compute_volume_ratio(bybit_symbol, recent_hours=1, baseline_hours=168)
        if ratio is None:
            return

        # Get absolute quote volume for the last hour
        df = get_bybit_ohlcv(bybit_symbol, interval="1h", limit=2)
        quote_vol = float(df["quote_volume"].iloc[-1]) if df is not None and not df.empty else 0
        trade_count = int(df["trades"].iloc[-1]) if df is not None and not df.empty and "trades" in df.columns else 0

        now = datetime.now(timezone.utc)
        insert_volume_profile(
            symbol=bybit_symbol,
            hour_of_day=now.hour,
            day_of_week=now.weekday(),
            volume_ratio=ratio,
            quote_volume=quote_vol,
            trade_count=trade_count,
        )
    except Exception:
        log.debug("Failed to record volume snapshot for %s", bybit_symbol, exc_info=True)
