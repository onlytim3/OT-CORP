"""On-chain data: DeFi Llama TVL and exchange netflow."""
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_tvl_cache: dict = {"data": None, "updated": 0}
_CACHE_TTL = 300


def get_tvl_change(hours: int = 24) -> Optional[float]:
    """Get total DeFi TVL change over the last N hours from DeFi Llama."""
    import urllib.request
    import json
    now = time.time()
    if _tvl_cache["data"] is not None and now - _tvl_cache["updated"] < _CACHE_TTL:
        return _tvl_cache["data"]
    try:
        url = "https://api.llama.fi/v2/historicalChainTvl"
        req = urllib.request.Request(url, headers={"User-Agent": "TradingBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if len(data) < 2:
            return None
        current_tvl = data[-1].get("tvl", 0)
        target_ts = now - hours * 3600
        past_tvl = current_tvl
        for entry in reversed(data):
            if entry.get("date", 0) <= target_ts:
                past_tvl = entry.get("tvl", current_tvl)
                break
        if past_tvl == 0:
            return None
        change = (current_tvl - past_tvl) / past_tvl
        _tvl_cache["data"] = round(change, 4)
        _tvl_cache["updated"] = now
        return _tvl_cache["data"]
    except Exception as e:
        logger.warning(f"Failed to get TVL data: {e}")
        return None


def get_exchange_netflow(coin: str = "BTC", hours: int = 24) -> Optional[float]:
    """Get exchange netflow. Positive=inflows (bearish), Negative=outflows (bullish)."""
    logger.debug(f"Exchange netflow not available for {coin} (no API key)")
    return None
