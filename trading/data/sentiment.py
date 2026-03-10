"""Sentiment data from free APIs — Fear & Greed Index + social signals."""

import requests
import pandas as pd
from trading.config import FEAR_GREED_URL
from trading.data.cache import cached


@cached(ttl=300)
def get_fear_greed(limit: int = 30) -> dict:
    """Get Crypto Fear & Greed Index from alternative.me.

    Returns dict with 'current' (latest value) and 'history' (DataFrame).
    Values: 0-24 Extreme Fear, 25-49 Fear, 50-74 Greed, 75-100 Extreme Greed.
    """
    resp = requests.get(
        FEAR_GREED_URL,
        params={"limit": limit, "format": "json"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()["data"]

    current = {
        "value": int(data[0]["value"]),
        "classification": data[0]["value_classification"],
        "timestamp": data[0]["timestamp"],
    }

    history = pd.DataFrame(data)
    history["value"] = history["value"].astype(int)
    history["timestamp"] = pd.to_datetime(history["timestamp"].astype(int), unit="s")
    history.set_index("timestamp", inplace=True)
    history.sort_index(inplace=True)

    return {"current": current, "history": history[["value", "value_classification"]]}


def classify_fear_greed(value: int) -> str:
    """Classify a Fear & Greed value into a category."""
    if value <= 24:
        return "Extreme Fear"
    elif value <= 49:
        return "Fear"
    elif value <= 74:
        return "Greed"
    else:
        return "Extreme Greed"


def get_market_sentiment_summary() -> dict:
    """Get a combined sentiment summary for trading decisions.

    Returns dict with fear_greed data plus derived trading signals.
    """
    fg = get_fear_greed(limit=30)
    current = fg["current"]
    history = fg["history"]

    # Calculate trend (is fear/greed increasing or decreasing?)
    if len(history) >= 7:
        recent_avg = history["value"].tail(7).mean()
        older_avg = history["value"].head(7).mean()
        trend = "increasing" if recent_avg > older_avg else "decreasing"
    else:
        recent_avg = current["value"]
        older_avg = current["value"]
        trend = "flat"

    # Generate signal
    value = current["value"]
    if value <= 25:
        signal = "buy"
        strength = (25 - value) / 25  # Stronger signal at lower values
    elif value >= 75:
        signal = "sell"
        strength = (value - 75) / 25
    else:
        signal = "hold"
        strength = 0.0

    return {
        "fear_greed_value": value,
        "classification": current["classification"],
        "trend": trend,
        "7d_avg": round(recent_avg, 1),
        "signal": signal,
        "signal_strength": round(strength, 2),
        "history": history,
    }
