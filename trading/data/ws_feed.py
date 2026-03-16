"""WebSocket real-time price feed for AsterDex.

Provides a background thread that maintains a WebSocket connection
for streaming mark prices. Falls back to REST when the websocket-client
library is not installed or the connection drops.
"""

import json
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_price_cache: dict[str, dict] = {}
_ws_thread: Optional[threading.Thread] = None
_ws_running = False


def get_realtime_price(symbol: str) -> Optional[float]:
    """Get cached real-time price, fallback to REST if stale or unavailable."""
    entry = _price_cache.get(symbol)
    if entry and time.time() - entry.get("updated", 0) < 30:
        return entry["price"]
    # Fallback to REST
    try:
        from trading.execution.aster_client import get_aster_mark_price

        price = get_aster_mark_price(symbol)
        if price:
            _price_cache[symbol] = {"price": float(price), "updated": time.time()}
            return float(price)
    except Exception as e:
        logger.debug("REST price fallback failed for %s: %s", symbol, e)
    return None


def _ws_listener(symbols: list[str]):
    """WebSocket listener thread -- reconnects automatically on failure."""
    global _ws_running
    try:
        import websocket  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("websocket-client not installed, using REST fallback only")
        _ws_running = False
        return

    streams = "/".join(f"{s.lower()}@markPrice" for s in symbols)
    url = f"wss://fstream.asterdex.com/ws/{streams}"

    def on_message(_ws, message):
        try:
            data = json.loads(message)
            sym = data.get("s", "")
            price = float(data.get("p", 0))
            if sym and price:
                _price_cache[sym] = {"price": price, "updated": time.time()}
        except Exception:
            pass

    def on_error(_ws, error):
        logger.warning("WebSocket error: %s", error)

    def on_close(_ws, close_status_code, close_msg):
        logger.info("WebSocket closed (code=%s)", close_status_code)

    while _ws_running:
        try:
            ws = websocket.WebSocketApp(
                url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            logger.warning("WebSocket reconnect after error: %s", e)
        if _ws_running:
            time.sleep(5)


def start_ws_feed(symbols: list[str]):
    """Start WebSocket feed in a background daemon thread."""
    global _ws_thread, _ws_running
    if _ws_running:
        return
    _ws_running = True
    _ws_thread = threading.Thread(
        target=_ws_listener, args=(symbols,), daemon=True
    )
    _ws_thread.start()
    logger.info("Started WebSocket price feed for %d symbols", len(symbols))


def stop_ws_feed():
    """Signal the WebSocket feed to stop."""
    global _ws_running
    _ws_running = False
