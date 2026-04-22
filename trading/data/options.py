"""Deribit public options flow — put/call ratio, DVOL, 25-delta skew.

All endpoints are public (no auth required). Data for BTC and ETH.
Provides directional market positioning signals from options market.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from trading.data.cache import cached

log = logging.getLogger(__name__)

_BASE = "https://www.deribit.com/api/v2/public"
_TIMEOUT = 10


def _get(endpoint: str, params: dict | None = None) -> dict | None:
    try:
        resp = requests.get(f"{_BASE}/{endpoint}", params=params or {}, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") is not None:
            return data["result"]
        return None
    except Exception as e:
        log.warning("Deribit API error (%s): %s", endpoint, e)
        return None


def _compute_put_call_ratio(currency: str) -> Optional[float]:
    """Compute put/call open interest ratio from book summary."""
    result = _get("get_book_summary_by_currency", {"currency": currency, "kind": "option"})
    if not result:
        return None
    put_oi = sum(r.get("open_interest", 0) for r in result if r.get("instrument_name", "").endswith("-P"))
    call_oi = sum(r.get("open_interest", 0) for r in result if r.get("instrument_name", "").endswith("-C"))
    if call_oi <= 0:
        return None
    return round(put_oi / call_oi, 4)


def _get_dvol(currency: str) -> Optional[float]:
    """Get Deribit Volatility Index (DVOL) via the time-series endpoint."""
    import time
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 3_600_000  # last 1 hour
    result = _get("get_volatility_index_data", {
        "currency": currency.upper(),
        "start_timestamp": start_ms,
        "end_timestamp": now_ms,
        "resolution": 3600,
    })
    if result and isinstance(result, dict):
        data = result.get("data", [])
        if data:
            return round(data[-1][4], 2)  # [timestamp, open, high, low, close]
    return None


def _get_25d_skew(currency: str) -> Optional[float]:
    """Approximate 25-delta skew from near-term option IVs.

    Positive skew = puts more expensive than calls = fear/downside hedging.
    Uses the nearest expiry with liquid options.
    """
    result = _get("get_book_summary_by_currency", {"currency": currency, "kind": "option"})
    if not result:
        return None

    # Find nearest expiry with both put and call volume
    from collections import defaultdict
    by_expiry: dict = defaultdict(lambda: {"puts": [], "calls": []})
    for r in result:
        name = r.get("instrument_name", "")
        parts = name.split("-")
        if len(parts) < 4:
            continue
        expiry = parts[1]
        iv = r.get("mark_iv")
        if iv is None or iv <= 0:
            continue
        if name.endswith("-P"):
            by_expiry[expiry]["puts"].append(iv)
        elif name.endswith("-C"):
            by_expiry[expiry]["calls"].append(iv)

    for expiry in sorted(by_expiry.keys()):
        p_ivs = by_expiry[expiry]["puts"]
        c_ivs = by_expiry[expiry]["calls"]
        if p_ivs and c_ivs:
            skew = round(sum(p_ivs) / len(p_ivs) - sum(c_ivs) / len(c_ivs), 2)
            return skew

    return None


def _confidence_weight(result: list | None) -> float:
    """Return a confidence multiplier in [0.0, 1.0] based on total options volume.

    Low volume → low confidence → shrink the signal toward zero so that thin
    markets don't produce noisy extreme readings.

    Thresholds (in contracts of open interest):
        >= 10 000  → full confidence  (1.0)
         >= 1 000  → high confidence  (0.75)
         >=   200  → medium           (0.5)
         <    200  → low              (0.25)
    """
    if not result:
        return 0.25
    total_oi = sum(r.get("open_interest", 0) for r in result)
    if total_oi >= 10_000:
        return 1.0
    elif total_oi >= 1_000:
        return 0.75
    elif total_oi >= 200:
        return 0.5
    return 0.25


def _interpret_signal(put_call_ratio: Optional[float], dvol: Optional[float],
                       skew: Optional[float]) -> tuple[str, float]:
    """Convert options metrics to a signal label and strength (-1 to +1)."""
    score = 0.0
    weight = 0.0

    if put_call_ratio is not None:
        if put_call_ratio > 1.5:
            score += -0.4
        elif put_call_ratio > 1.2:
            score += -0.2
        elif put_call_ratio < 0.6:
            score += 0.4
        elif put_call_ratio < 0.8:
            score += 0.2
        weight += 1.0

    if dvol is not None:
        if dvol > 80:
            score += -0.3
        elif dvol > 60:
            score += -0.15
        elif dvol < 30:
            score += 0.1
        weight += 0.5

    if skew is not None:
        if skew > 5:
            score += -0.2
        elif skew > 2:
            score += -0.1
        elif skew < -2:
            score += 0.1
        weight += 0.5

    if weight == 0:
        return "neutral", 0.0

    normalized = round(max(-1.0, min(1.0, score / max(weight, 1.0))), 4)
    if normalized >= 0.3:
        label = "bullish"
    elif normalized <= -0.3:
        label = "bearish"
    else:
        label = "neutral"
    return label, normalized


@cached(ttl=600)
def get_options_summary(currency: str = "BTC") -> dict:
    """Returns put_call_ratio, dvol, skew_25d, and directional signal for BTC or ETH.

    Signal strength is scaled by a volume-based confidence weight so that thin
    order-books produce attenuated signals rather than false extremes.
    """
    # Fetch the book once and reuse it for PCR, skew, and confidence weight.
    book_result = _get("get_book_summary_by_currency", {"currency": currency, "kind": "option"})
    confidence = _confidence_weight(book_result)

    pcr = None
    if book_result:
        put_oi = sum(r.get("open_interest", 0) for r in book_result if r.get("instrument_name", "").endswith("-P"))
        call_oi = sum(r.get("open_interest", 0) for r in book_result if r.get("instrument_name", "").endswith("-C"))
        if call_oi > 0:
            pcr = round(put_oi / call_oi, 4)

    dvol = _get_dvol(currency)

    skew = None
    if book_result:
        from collections import defaultdict
        by_expiry: dict = defaultdict(lambda: {"puts": [], "calls": []})
        for r in book_result:
            name = r.get("instrument_name", "")
            parts = name.split("-")
            if len(parts) < 4:
                continue
            expiry = parts[1]
            iv = r.get("mark_iv")
            if iv is None or iv <= 0:
                continue
            if name.endswith("-P"):
                by_expiry[expiry]["puts"].append(iv)
            elif name.endswith("-C"):
                by_expiry[expiry]["calls"].append(iv)
        for expiry in sorted(by_expiry.keys()):
            p_ivs = by_expiry[expiry]["puts"]
            c_ivs = by_expiry[expiry]["calls"]
            if p_ivs and c_ivs:
                skew = round(sum(p_ivs) / len(p_ivs) - sum(c_ivs) / len(c_ivs), 2)
                break

    signal_label, raw_strength = _interpret_signal(pcr, dvol, skew)
    signal_strength = round(raw_strength * confidence, 4)

    # Re-derive label from scaled strength to stay consistent.
    if signal_strength >= 0.3:
        signal_label = "bullish"
    elif signal_strength <= -0.3:
        signal_label = "bearish"
    else:
        signal_label = "neutral"

    return {
        "currency": currency,
        "put_call_ratio": pcr,
        "dvol": dvol,
        "skew_25d": skew,
        "signal": signal_label,
        "signal_strength": signal_strength,
        "volume_confidence": confidence,
    }


@cached(ttl=600)
def get_options_market_data() -> dict:
    """Aggregate options flow for BTC + ETH. Returns composite signal (-1 to +1)."""
    btc = get_options_summary("BTC")
    eth = get_options_summary("ETH")

    btc_s = btc["signal_strength"]
    eth_s = eth["signal_strength"]

    # BTC weighted 60%, ETH 40%
    composite = round(btc_s * 0.6 + eth_s * 0.4, 4)

    return {
        "btc": btc,
        "eth": eth,
        "composite_signal": composite,
    }
