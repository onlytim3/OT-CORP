"""Bybit V5 WebSocket real-time price feed (linear perps).

Uses pybit.unified_trading.WebSocket which manages reconnection internally.
REST fallback via bybit_client.get_bybit_mark_prices when stale or unavailable.
"""

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_price_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
_ws_client = None
_ws_running = False


def _testnet_flag() -> bool:
    return os.getenv("BYBIT_TESTNET", "true").lower() in ("1", "true", "yes")


def get_realtime_price(symbol: str) -> Optional[float]:
    """Cached real-time mark price; REST fallback if stale (>30s) or missing."""
    entry = _price_cache.get(symbol)
    if entry and time.time() - entry.get("updated", 0) < 30:
        return entry["price"]
    try:
        from trading.execution.bybit_client import get_bybit_mark_prices

        data = get_bybit_mark_prices(symbol)
        price = data.get("markPrice") if isinstance(data, dict) else None
        if price:
            with _cache_lock:
                _price_cache[symbol] = {"price": float(price), "updated": time.time()}
            return float(price)
    except Exception as e:
        logger.debug("REST price fallback failed for %s: %s", symbol, e)
    return None


def _on_ticker(message):
    """pybit invokes this for each ticker update.

    Bybit V5 ticker stream sends partial updates; markPrice may be absent on
    a particular delta — we only update the cache when it's present.
    """
    try:
        data = message.get("data", {}) if isinstance(message, dict) else {}
        sym = data.get("symbol", "")
        mp = data.get("markPrice")
        if sym and mp:
            with _cache_lock:
                _price_cache[sym] = {"price": float(mp), "updated": time.time()}
    except Exception as e:
        logger.debug("ws message parse error: %s", e)


def start_ws_feed(symbols: list[str]):
    """Start the Bybit WebSocket feed in a background thread.

    pybit's WebSocket spawns its own daemon thread per channel and handles
    reconnection automatically, so no manual reconnect loop is needed.
    """
    global _ws_client, _ws_running
    if _ws_running:
        return
    try:
        from pybit.unified_trading import WebSocket
    except ImportError:
        logger.warning("pybit not installed, using REST fallback only")
        return

    testnet = _testnet_flag()
    try:
        _ws_client = WebSocket(testnet=testnet, channel_type="linear")
    except Exception as e:
        logger.warning("Bybit WebSocket init failed: %s", e)
        return

    _ws_running = True
    subscribed = 0
    for s in symbols:
        try:
            _ws_client.ticker_stream(symbol=s, callback=_on_ticker)
            subscribed += 1
        except Exception as e:
            logger.warning("WS subscribe failed for %s: %s", s, e)
    logger.info(
        "Started Bybit WebSocket feed for %d/%d symbols (testnet=%s)",
        subscribed, len(symbols), testnet,
    )


def stop_ws_feed():
    """Tear down the WebSocket client."""
    global _ws_running, _ws_client
    _ws_running = False
    if _ws_client is not None:
        try:
            _ws_client.exit()
        except Exception:
            pass
        _ws_client = None
