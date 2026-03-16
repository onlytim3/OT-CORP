"""News sentiment from CryptoPanic free API."""
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY", "")
_sentiment_cache: dict = {}
_CACHE_TTL = 600


def get_news_sentiment(coin: str = "BTC", hours: int = 24) -> Optional[float]:
    """Get news sentiment score from -1 to +1."""
    import urllib.request
    import json
    cache_key = f"{coin}_{hours}"
    now = time.time()
    if cache_key in _sentiment_cache and now - _sentiment_cache[cache_key]["updated"] < _CACHE_TTL:
        return _sentiment_cache[cache_key]["score"]
    if not CRYPTOPANIC_API_KEY:
        return None
    try:
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API_KEY}&currencies={coin}&kind=news"
        req = urllib.request.Request(url, headers={"User-Agent": "TradingBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])
        if not results:
            return None
        positive = sum(1 for r in results if r.get("votes", {}).get("positive", 0) > r.get("votes", {}).get("negative", 0))
        negative = sum(1 for r in results if r.get("votes", {}).get("negative", 0) > r.get("votes", {}).get("positive", 0))
        total = len(results)
        if total == 0:
            return None
        score = (positive - negative) / total
        _sentiment_cache[cache_key] = {"score": score, "updated": now}
        return round(score, 3)
    except Exception as e:
        logger.warning(f"Failed to get news sentiment for {coin}: {e}")
        return None
