"""Bybit V5 perpetual futures client (linear USDT perps).

Wraps `pybit.unified_trading.HTTP` and flattens V5 responses to the
shape the rest of the system already consumes.
"""

import logging
import os
import threading
import time
from typing import Any, Optional

import pandas as pd

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
RETRY_BACKOFF_FACTOR = 2.0

_NON_RETRYABLE_KEYWORDS = frozenset({
    "insufficient",
    "invalid symbol",
    "invalid side",
    "invalid quantity",
    "invalid price",
    "order does not exist",
    "api key",
    "signature",
    "unauthorized",
    "param error",
    "leverage not modified",
    "position mode not modified",
    "10001",
})


def _bybit_env() -> dict:
    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": os.getenv("BYBIT_TESTNET", "true").lower() in ("1", "true", "yes"),
        "recv_window": int(os.getenv("BYBIT_RECV_WINDOW", "5000")),
    }


def is_bybit_configured() -> bool:
    cfg = _bybit_env()
    return bool(cfg["api_key"] and cfg["api_secret"])


class _RateLimiter:
    """Sliding-window rate limiter shared with prior client (test_upgrades imports)."""

    def __init__(self, max_requests: int = 600, window_seconds: float = 60.0):
        self._max = max_requests
        self._window = window_seconds
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now = time.time()
                cutoff = now - self._window
                self._timestamps = [t for t in self._timestamps if t > cutoff]
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return
                wait_time = self._timestamps[0] - cutoff + 0.05
            time.sleep(wait_time)

    @property
    def current_count(self) -> int:
        with self._lock:
            cutoff = time.time() - self._window
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            return len(self._timestamps)


_public_limiter = _RateLimiter(max_requests=600, window_seconds=60.0)
_private_limiter = _RateLimiter(max_requests=200, window_seconds=60.0)

_http_public = None
_http_auth = None
_http_lock = threading.Lock()


def _get_public_http():
    global _http_public
    if _http_public is None:
        from pybit.unified_trading import HTTP
        cfg = _bybit_env()
        with _http_lock:
            if _http_public is None:
                _http_public = HTTP(testnet=cfg["testnet"], recv_window=cfg["recv_window"])
                log.debug("Bybit public HTTP initialized (testnet=%s)", cfg["testnet"])
    return _http_public


def _get_auth_http():
    global _http_auth
    if _http_auth is None:
        from pybit.unified_trading import HTTP
        cfg = _bybit_env()
        if not (cfg["api_key"] and cfg["api_secret"]):
            raise ValueError(
                "Bybit auth requires BYBIT_API_KEY and BYBIT_API_SECRET env vars."
            )
        with _http_lock:
            if _http_auth is None:
                _http_auth = HTTP(
                    testnet=cfg["testnet"],
                    api_key=cfg["api_key"],
                    api_secret=cfg["api_secret"],
                    recv_window=cfg["recv_window"],
                )
                log.info(
                    "Bybit authenticated HTTP initialized (testnet=%s)",
                    cfg["testnet"],
                )
    return _http_auth


def _is_retryable(error: Exception) -> bool:
    err_str = str(error).lower()
    for kw in _NON_RETRYABLE_KEYWORDS:
        if kw in err_str:
            return False
    return True


def _retry(func, *args, **kwargs):
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            if not _is_retryable(e) or attempt == MAX_RETRIES - 1:
                raise
            delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
            log.warning(
                "Bybit API error (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, MAX_RETRIES, delay, e,
            )
            time.sleep(delay)
    raise last_err


def _unwrap(resp: Any) -> Any:
    """Extract `result` from Bybit V5 envelope, raising on retCode != 0."""
    if not isinstance(resp, dict):
        return resp
    code = resp.get("retCode")
    if code is not None and int(code) != 0:
        raise RuntimeError(f"Bybit API error {code}: {resp.get('retMsg', 'unknown')}")
    return resp.get("result", resp)


# Bybit V5 kline interval translation table
_INTERVAL_MAP = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "3d": "D", "1w": "W", "1M": "M",
}

# Bybit OI interval translation
_OI_INTERVAL_MAP = {
    "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "2h": "1h", "4h": "4h", "6h": "4h", "12h": "1d", "1d": "1d",
}

# Bybit long/short ratio period translation (4d is the closest to 1d)
_LSR_PERIOD_MAP = {
    "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "2h": "1h", "4h": "4h", "6h": "4h", "12h": "4d", "1d": "4d",
}

# Bybit valid orderbook depths for linear
_VALID_OB_LIMITS = (1, 50, 200, 500)


# ---------------------------------------------------------------------------
# Public market data (no auth)
# ---------------------------------------------------------------------------

def get_bybit_exchange_info() -> dict:
    """Return {"symbols": [{symbol, status, filters: [...]}, ...]} flattened
    from Bybit V5 instruments-info to roughly the Bybit shape."""
    _public_limiter.acquire()
    h = _get_public_http()
    raw = _retry(h.get_instruments_info, category="linear")
    result = _unwrap(raw)
    symbols = []
    for s in result.get("list", []):
        lot = s.get("lotSizeFilter", {}) or {}
        price = s.get("priceFilter", {}) or {}
        symbols.append({
            "symbol": s.get("symbol", ""),
            "status": s.get("status", ""),
            "baseAsset": s.get("baseCoin", ""),
            "quoteAsset": s.get("quoteCoin", ""),
            "contractType": s.get("contractType", ""),
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": lot.get("qtyStep", "0"),
                    "minQty": lot.get("minOrderQty", "0"),
                    "maxQty": lot.get("maxOrderQty", "0"),
                },
                {
                    "filterType": "PRICE_FILTER",
                    "tickSize": price.get("tickSize", "0"),
                    "minPrice": price.get("minPrice", "0"),
                    "maxPrice": price.get("maxPrice", "0"),
                },
                {
                    "filterType": "MIN_NOTIONAL",
                    "notional": lot.get("minNotionalValue", "0"),
                },
            ],
        })
    return {"symbols": symbols}


def get_bybit_klines(
    symbol: str,
    interval: str = "1h",
    limit: int = 500,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
) -> pd.DataFrame:
    """OHLCV candles. Returns DataFrame with the same columns as the prior
    Bybit client; taker_buy_* columns are zero-filled (Bybit doesn't expose them)."""
    _public_limiter.acquire()
    h = _get_public_http()
    bybit_interval = _INTERVAL_MAP.get(interval, interval)
    kwargs = {
        "category": "linear",
        "symbol": symbol,
        "interval": bybit_interval,
        "limit": min(limit, 1000),
    }
    if start_time is not None:
        kwargs["start"] = int(start_time)
    if end_time is not None:
        kwargs["end"] = int(end_time)

    raw = _retry(h.get_kline, **kwargs)
    result = _unwrap(raw)
    rows = result.get("list", [])
    cols = ["open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base_vol", "taker_buy_quote_vol"]
    if not rows:
        return pd.DataFrame(columns=cols)

    # Bybit returns rows as [start, open, high, low, close, volume, turnover],
    # newest-first. Reverse for time-ascending.
    rows = list(reversed(rows))
    records = []
    for r in rows:
        start_ms = int(r[0])
        records.append({
            "open_time": start_ms,
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
            "close_time": start_ms,  # Bybit doesn't return close_time; same start
            "quote_volume": float(r[6]) if len(r) > 6 else 0.0,
            "trades": 0,
            "taker_buy_base_vol": 0.0,
            "taker_buy_quote_vol": 0.0,
        })
    df = pd.DataFrame(records)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df.set_index("open_time", inplace=True)
    return df


def get_bybit_orderbook(symbol: str, limit: int = 100) -> dict:
    _public_limiter.acquire()
    h = _get_public_http()
    # Round up to nearest valid Bybit depth
    valid = next((v for v in _VALID_OB_LIMITS if v >= limit), 500)
    raw = _retry(h.get_orderbook, category="linear", symbol=symbol, limit=valid)
    result = _unwrap(raw)
    bids = [[float(p), float(q)] for p, q in result.get("b", [])]
    asks = [[float(p), float(q)] for p, q in result.get("a", [])]
    return {
        "symbol": symbol,
        "bids": bids,
        "asks": asks,
        "lastUpdateId": result.get("u", 0),
    }


def get_bybit_funding_rates(
    symbol: Optional[str] = None,
    limit: int = 100,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
) -> list[dict]:
    _public_limiter.acquire()
    h = _get_public_http()
    kwargs = {"category": "linear", "limit": min(limit, 200)}
    if symbol:
        kwargs["symbol"] = symbol
    if start_time is not None:
        kwargs["startTime"] = int(start_time)
    if end_time is not None:
        kwargs["endTime"] = int(end_time)
    raw = _retry(h.get_funding_rate_history, **kwargs)
    result = _unwrap(raw)
    out = []
    for e in result.get("list", []):
        out.append({
            "symbol": e.get("symbol", symbol or ""),
            "fundingRate": float(e.get("fundingRate", 0)),
            "fundingTime": int(e.get("fundingRateTimestamp", 0)),
        })
    return out


def _flatten_ticker(t: dict) -> dict:
    """Map a Bybit V5 linear ticker dict to Bybit flattened keys."""
    def f(key, default=0.0):
        try:
            return float(t.get(key, default))
        except (ValueError, TypeError):
            return default

    def i(key, default=0):
        try:
            return int(t.get(key, default))
        except (ValueError, TypeError):
            return default

    return {
        "symbol": t.get("symbol", ""),
        # Mark-price family (premiumIndex parity)
        "markPrice": f("markPrice"),
        "indexPrice": f("indexPrice"),
        "lastFundingRate": f("fundingRate"),
        "nextFundingTime": i("nextFundingTime"),
        "interestRate": 0.0,
        "time": i("nextFundingTime"),
        # 24hr ticker family
        "lastPrice": f("lastPrice"),
        "priceChange": (f("lastPrice") - f("prevPrice24h")),
        "priceChangePercent": f("price24hPcnt") * 100.0,
        "weightedAvgPrice": f("lastPrice"),
        "highPrice": f("highPrice24h"),
        "lowPrice": f("lowPrice24h"),
        "openPrice": f("prevPrice24h"),
        "volume": f("volume24h"),
        "quoteVolume": f("turnover24h"),
        "lastQty": 0.0,
        # Book ticker family
        "bidPrice": f("bid1Price"),
        "bidQty": f("bid1Size"),
        "askPrice": f("ask1Price"),
        "askQty": f("ask1Size"),
        # Open interest (as snapshot)
        "openInterest": f("openInterest"),
        "openInterestValue": f("openInterestValue"),
    }


def _get_tickers(symbol: Optional[str]) -> Any:
    _public_limiter.acquire()
    h = _get_public_http()
    kwargs = {"category": "linear"}
    if symbol:
        kwargs["symbol"] = symbol
    raw = _retry(h.get_tickers, **kwargs)
    result = _unwrap(raw)
    items = [_flatten_ticker(t) for t in result.get("list", [])]
    if symbol:
        return items[0] if items else {}
    return items


def get_bybit_mark_prices(symbol: Optional[str] = None) -> Any:
    return _get_tickers(symbol)


def get_bybit_ticker_24h(symbol: Optional[str] = None) -> Any:
    return _get_tickers(symbol)


def get_bybit_book_ticker(symbol: Optional[str] = None) -> Any:
    return _get_tickers(symbol)


def get_bybit_open_interest(symbol: str) -> dict:
    _public_limiter.acquire()
    h = _get_public_http()
    raw = _retry(
        h.get_open_interest,
        category="linear",
        symbol=symbol,
        intervalTime="5min",
        limit=1,
    )
    result = _unwrap(raw)
    rows = result.get("list", [])
    if not rows:
        return {"symbol": symbol, "openInterest": 0.0, "time": 0}
    latest = rows[0]
    return {
        "symbol": symbol,
        "openInterest": float(latest.get("openInterest", 0)),
        "time": int(latest.get("timestamp", 0)),
    }


def get_bybit_open_interest_hist(
    symbol: str,
    period: str = "1h",
    limit: int = 30,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
) -> list[dict]:
    _public_limiter.acquire()
    h = _get_public_http()
    interval = _OI_INTERVAL_MAP.get(period, "1h")
    kwargs = {
        "category": "linear",
        "symbol": symbol,
        "intervalTime": interval,
        "limit": min(limit, 200),
    }
    if start_time is not None:
        kwargs["startTime"] = int(start_time)
    if end_time is not None:
        kwargs["endTime"] = int(end_time)
    raw = _retry(h.get_open_interest, **kwargs)
    result = _unwrap(raw)
    out = []
    for e in reversed(result.get("list", [])):
        oi = float(e.get("openInterest", 0))
        out.append({
            "symbol": symbol,
            "sumOpenInterest": oi,
            "sumOpenInterestValue": oi,
            "timestamp": int(e.get("timestamp", 0)),
        })
    return out


def get_bybit_long_short_ratio(
    symbol: str,
    period: str = "1h",
    limit: int = 30,
) -> list[dict]:
    _public_limiter.acquire()
    h = _get_public_http()
    bybit_period = _LSR_PERIOD_MAP.get(period, "1h")
    raw = _retry(
        h.get_long_short_ratio,
        category="linear",
        symbol=symbol,
        period=bybit_period,
        limit=min(limit, 500),
    )
    result = _unwrap(raw)
    out = []
    for e in reversed(result.get("list", [])):
        buy = float(e.get("buyRatio", 0))
        sell = float(e.get("sellRatio", 0))
        ratio = (buy / sell) if sell > 0 else 0.0
        out.append({
            "symbol": symbol,
            "longShortRatio": ratio,
            "longAccount": buy,
            "shortAccount": sell,
            "timestamp": int(e.get("timestamp", 0)),
        })
    return out


_taker_warning_emitted = False


def get_bybit_taker_buy_sell_volume(
    symbol: str,
    period: str = "1h",
    limit: int = 30,
) -> list[dict]:
    """No Bybit V5 equivalent. Returns []; warns once."""
    global _taker_warning_emitted
    if not _taker_warning_emitted:
        log.warning(
            "Bybit V5 has no taker buy/sell volume history endpoint; returning []."
        )
        _taker_warning_emitted = True
    return []


def bybit_ping() -> bool:
    try:
        _public_limiter.acquire()
        _retry(_get_public_http().get_server_time)
        return True
    except Exception as e:
        log.warning("Bybit ping failed: %s", e)
        return False


def bybit_server_time() -> int:
    _public_limiter.acquire()
    raw = _retry(_get_public_http().get_server_time)
    result = _unwrap(raw)
    return int(result.get("timeNano", 0)) // 1_000_000 or int(result.get("timeSecond", 0)) * 1000


# ---------------------------------------------------------------------------
# Trading (auth required)
# ---------------------------------------------------------------------------

_ORDER_TYPE_MAP = {
    "MARKET": ("Market", False),
    "LIMIT": ("Limit", False),
    "STOP": ("Limit", True),
    "STOP_MARKET": ("Market", True),
    "TAKE_PROFIT": ("Limit", True),
    "TAKE_PROFIT_MARKET": ("Market", True),
    "TRAILING_STOP_MARKET": ("Market", True),
}

_TIF_MAP = {
    "GTC": "GTC",
    "IOC": "IOC",
    "FOK": "FOK",
    "GTX": "PostOnly",
}

_POSITION_IDX = {"BOTH": 0, "LONG": 1, "SHORT": 2}


def _flatten_order(o: dict) -> dict:
    """Map a Bybit V5 order dict to Bybit-shaped order result."""
    def f(k, d=0.0):
        try:
            return float(o.get(k, d))
        except (ValueError, TypeError):
            return d

    side_raw = (o.get("side", "") or "").upper()
    return {
        "orderId": o.get("orderId", ""),
        "clientOrderId": o.get("orderLinkId", ""),
        "symbol": o.get("symbol", ""),
        "side": side_raw if side_raw in ("BUY", "SELL") else side_raw.upper(),
        "type": (o.get("orderType", "") or "").upper(),
        "status": o.get("orderStatus", ""),
        "price": f("price"),
        "avgPrice": f("avgPrice"),
        "origQty": f("qty"),
        "executedQty": f("cumExecQty"),
        "cumQuote": f("cumExecValue"),
        "timeInForce": o.get("timeInForce", ""),
        "reduceOnly": bool(o.get("reduceOnly", False)),
        "updateTime": int(o.get("updatedTime", 0) or 0),
    }


def bybit_submit_order(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float,
    price: Optional[float] = None,
    time_in_force: str = "GTC",
    position_side: str = "BOTH",
    reduce_only: Optional[bool] = None,
    stop_price: Optional[float] = None,
    client_order_id: Optional[str] = None,
) -> dict:
    side_u = side.upper()
    order_type_u = order_type.upper()
    if side_u not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got '{side}'")
    if order_type_u not in _ORDER_TYPE_MAP:
        raise ValueError(f"unsupported order_type '{order_type}'")
    if order_type_u == "LIMIT" and price is None:
        raise ValueError("price is required for LIMIT orders")

    bybit_type, is_trigger = _ORDER_TYPE_MAP[order_type_u]
    bybit_side = "Buy" if side_u == "BUY" else "Sell"

    params: dict = {
        "category": "linear",
        "symbol": symbol,
        "side": bybit_side,
        "orderType": bybit_type,
        "qty": str(quantity),
        "positionIdx": _POSITION_IDX.get(position_side.upper(), 0),
    }
    if bybit_type == "Limit":
        params["timeInForce"] = _TIF_MAP.get(time_in_force.upper(), "GTC")
    if price is not None:
        params["price"] = str(price)
    if reduce_only is True:
        params["reduceOnly"] = True
    if is_trigger and stop_price is not None:
        params["triggerPrice"] = str(stop_price)
        # triggerDirection: 1 = rises to trigger, 2 = falls
        # For a STOP_MARKET sell, price falls below trigger → 2
        # For a STOP_MARKET buy, price rises above trigger → 1
        params["triggerDirection"] = 1 if bybit_side == "Buy" else 2
    if client_order_id is not None:
        params["orderLinkId"] = client_order_id

    _private_limiter.acquire()
    h = _get_auth_http()
    log.info("Submitting Bybit order: %s %s %s qty=%s price=%s",
             bybit_side, symbol, order_type_u, quantity, price)
    raw = _retry(h.place_order, **params)
    result = _unwrap(raw)
    out = {
        "orderId": result.get("orderId", ""),
        "clientOrderId": result.get("orderLinkId", client_order_id or ""),
        "symbol": symbol,
        "side": side_u,
        "type": order_type_u,
        "status": "NEW",
        "price": float(price) if price is not None else 0.0,
        "origQty": float(quantity),
        "executedQty": 0.0,
        "avgPrice": 0.0,
    }
    log.info("Bybit order result: orderId=%s", out["orderId"])
    return out


def bybit_cancel_order(
    symbol: str,
    order_id: Optional[str] = None,
    client_order_id: Optional[str] = None,
) -> dict:
    if order_id is None and client_order_id is None:
        raise ValueError("Either order_id or client_order_id must be provided")
    params = {"category": "linear", "symbol": symbol}
    if order_id is not None:
        params["orderId"] = str(order_id)
    if client_order_id is not None:
        params["orderLinkId"] = client_order_id
    _private_limiter.acquire()
    raw = _retry(_get_auth_http().cancel_order, **params)
    result = _unwrap(raw)
    return {
        "orderId": result.get("orderId", str(order_id or "")),
        "clientOrderId": result.get("orderLinkId", client_order_id or ""),
        "symbol": symbol,
        "status": "CANCELED",
    }


def bybit_cancel_all_orders(symbol: str) -> dict:
    _private_limiter.acquire()
    raw = _retry(
        _get_auth_http().cancel_all_orders,
        category="linear",
        symbol=symbol,
    )
    return _unwrap(raw)


def bybit_get_order(
    symbol: str,
    order_id: Optional[str] = None,
    client_order_id: Optional[str] = None,
) -> dict:
    if order_id is None and client_order_id is None:
        raise ValueError("Either order_id or client_order_id must be provided")
    params = {"category": "linear", "symbol": symbol}
    if order_id is not None:
        params["orderId"] = str(order_id)
    if client_order_id is not None:
        params["orderLinkId"] = client_order_id

    _private_limiter.acquire()
    h = _get_auth_http()
    raw = _retry(h.get_open_orders, **params)
    result = _unwrap(raw)
    rows = result.get("list", [])
    if not rows:
        # Fall back to closed-order history
        raw = _retry(h.get_order_history, **params)
        result = _unwrap(raw)
        rows = result.get("list", [])
    if not rows:
        raise RuntimeError(f"Order {order_id or client_order_id} not found on {symbol}")
    return _flatten_order(rows[0])


def bybit_get_open_orders(symbol: Optional[str] = None) -> list[dict]:
    _private_limiter.acquire()
    params = {"category": "linear"}
    if symbol:
        params["symbol"] = symbol
    else:
        # Bybit requires either symbol or settleCoin
        params["settleCoin"] = "USDT"
    raw = _retry(_get_auth_http().get_open_orders, **params)
    result = _unwrap(raw)
    return [_flatten_order(o) for o in result.get("list", [])]


def bybit_get_balance() -> list[dict]:
    _private_limiter.acquire()
    raw = _retry(_get_auth_http().get_wallet_balance, accountType="UNIFIED")
    result = _unwrap(raw)
    out = []
    for acct in result.get("list", []):
        for c in acct.get("coin", []):
            def f(k, default=0.0):
                v = c.get(k, "")
                try:
                    return float(v) if v not in ("", None) else default
                except (ValueError, TypeError):
                    return default
            out.append({
                "asset": c.get("coin", ""),
                "balance": f("walletBalance"),
                "crossWalletBalance": f("walletBalance"),
                "availableBalance": f("availableToWithdraw") or f("walletBalance"),
                "crossUnPnl": f("unrealisedPnl"),
                "maxWithdrawAmount": f("availableToWithdraw"),
            })
    return out


def bybit_get_account() -> dict:
    _private_limiter.acquire()
    raw = _retry(_get_auth_http().get_wallet_balance, accountType="UNIFIED")
    result = _unwrap(raw)
    accts = result.get("list", [])
    if not accts:
        return {
            "totalWalletBalance": 0.0,
            "totalUnrealizedProfit": 0.0,
            "totalMarginBalance": 0.0,
            "availableBalance": 0.0,
            "totalCrossWalletBalance": 0.0,
            "totalCrossUnPnl": 0.0,
            "totalInitialMargin": 0.0,
            "totalMaintMargin": 0.0,
        }
    a = accts[0]

    def f(k, default=0.0):
        v = a.get(k, "")
        try:
            return float(v) if v not in ("", None) else default
        except (ValueError, TypeError):
            return default

    wallet = f("totalWalletBalance") or f("totalEquity")
    return {
        "totalWalletBalance": wallet,
        "totalUnrealizedProfit": f("totalPerpUPL"),
        "totalMarginBalance": f("totalMarginBalance") or wallet,
        "availableBalance": f("totalAvailableBalance"),
        "totalCrossWalletBalance": wallet,
        "totalCrossUnPnl": f("totalPerpUPL"),
        "totalInitialMargin": f("totalInitialMargin"),
        "totalMaintMargin": f("totalMaintenanceMargin"),
    }


def bybit_get_positions() -> list[dict]:
    _private_limiter.acquire()
    raw = _retry(
        _get_auth_http().get_positions,
        category="linear",
        settleCoin="USDT",
    )
    result = _unwrap(raw)
    out = []
    for p in result.get("list", []):
        def f(k, default=0.0):
            v = p.get(k, "")
            try:
                return float(v) if v not in ("", None) else default
            except (ValueError, TypeError):
                return default

        size = f("size")
        side = (p.get("side", "") or "").lower()
        # Bybit returns unsigned size + side; Bybit shape uses signed positionAmt.
        signed = size if side == "buy" else (-size if side == "sell" else 0.0)
        out.append({
            "symbol": p.get("symbol", ""),
            "positionSide": p.get("positionIdx", 0) and "LONG" or "BOTH",
            "positionAmt": signed,
            "entryPrice": f("avgPrice"),
            "markPrice": f("markPrice"),
            "unRealizedProfit": f("unrealisedPnl"),
            "liquidationPrice": f("liqPrice"),
            "leverage": f("leverage"),
            "marginType": "isolated" if p.get("tradeMode", 0) == 1 else "cross",
            "isolatedMargin": f("positionIM"),
            "notional": f("positionValue"),
        })
    return out


def bybit_set_leverage(symbol: str, leverage: int) -> dict:
    if not 1 <= leverage <= 125:
        raise ValueError(f"Leverage must be 1-125, got {leverage}")
    _private_limiter.acquire()
    try:
        raw = _retry(
            _get_auth_http().set_leverage,
            category="linear",
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        _unwrap(raw)
    except Exception as e:
        # 110043 = leverage not modified (already set); treat as success.
        if "110043" in str(e) or "leverage not modified" in str(e).lower():
            log.debug("Bybit leverage already at %dx for %s", leverage, symbol)
        else:
            raise
    return {"symbol": symbol, "leverage": leverage}


def bybit_set_margin_type(symbol: str, margin_type: str) -> dict:
    margin_type_u = margin_type.upper()
    if margin_type_u not in ("ISOLATED", "CROSSED"):
        raise ValueError(f"margin_type must be 'ISOLATED' or 'CROSSED', got '{margin_type}'")
    trade_mode = 1 if margin_type_u == "ISOLATED" else 0
    _private_limiter.acquire()
    h = _get_auth_http()
    # Need current leverage; fetch from position info
    try:
        pos_raw = _retry(h.get_positions, category="linear", symbol=symbol)
        plist = _unwrap(pos_raw).get("list", [])
        lev = str(int(float(plist[0].get("leverage", "1")))) if plist else "1"
    except Exception:
        lev = "1"
    raw = _retry(
        h.switch_margin_mode,
        category="linear",
        symbol=symbol,
        tradeMode=trade_mode,
        buyLeverage=lev,
        sellLeverage=lev,
    )
    return _unwrap(raw)


def bybit_get_trades(symbol: str, limit: int = 500) -> list[dict]:
    _private_limiter.acquire()
    raw = _retry(
        _get_auth_http().get_executions,
        category="linear",
        symbol=symbol,
        limit=min(limit, 100),
    )
    result = _unwrap(raw)
    out = []
    for e in result.get("list", []):
        def f(k, default=0.0):
            v = e.get(k, "")
            try:
                return float(v) if v not in ("", None) else default
            except (ValueError, TypeError):
                return default
        out.append({
            "symbol": e.get("symbol", symbol),
            "id": e.get("execId", ""),
            "orderId": e.get("orderId", ""),
            "side": (e.get("side", "") or "").upper(),
            "price": f("execPrice"),
            "qty": f("execQty"),
            "commission": f("execFee"),
            "commissionAsset": e.get("feeCurrency", "USDT"),
            "time": int(e.get("execTime", 0) or 0),
        })
    return out


def bybit_get_income(
    symbol: Optional[str] = None,
    income_type: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Merged income history over Bybit closed-PnL + transaction-log endpoints.

    Bybit /fapi/v3/income returned a single stream; Bybit splits realized P&L
    from funding fees and commissions. We fan out and prefix tranId by source
    to keep INSERT OR IGNORE collisions out of income_history.
    """
    _private_limiter.acquire()
    h = _get_auth_http()
    out: list[dict] = []

    types = [income_type] if income_type else ["REALIZED_PNL", "FUNDING_FEE", "COMMISSION"]

    if "REALIZED_PNL" in types:
        params = {"category": "linear", "limit": min(limit, 100)}
        if symbol:
            params["symbol"] = symbol
        try:
            raw = _retry(h.get_closed_pnl, **params)
            for e in _unwrap(raw).get("list", []):
                tran = e.get("orderId", "") or e.get("execTime", "")
                pnl = float(e.get("closedPnl", 0) or 0)
                out.append({
                    "symbol": e.get("symbol", symbol or ""),
                    "incomeType": "REALIZED_PNL",
                    "income": pnl,
                    "asset": "USDT",
                    "time": int(e.get("updatedTime", 0) or e.get("createdTime", 0) or 0),
                    "tranId": f"pnl_{tran}",
                    "tradeId": e.get("orderId", ""),
                })
        except Exception as e:
            log.warning("Bybit get_closed_pnl failed: %s", e)

    if "FUNDING_FEE" in types or "COMMISSION" in types:
        params = {"accountType": "UNIFIED", "category": "linear", "limit": min(limit, 50)}
        if symbol:
            params["symbol"] = symbol
        try:
            raw = _retry(h.get_transaction_log, **params)
            for e in _unwrap(raw).get("list", []):
                tx_type = (e.get("type", "") or "").upper()
                amt = float(e.get("change", 0) or 0)
                if tx_type == "SETTLEMENT" and "FUNDING_FEE" in types:
                    out.append({
                        "symbol": e.get("symbol", symbol or ""),
                        "incomeType": "FUNDING_FEE",
                        "income": amt,
                        "asset": e.get("currency", "USDT"),
                        "time": int(e.get("transactionTime", 0) or 0),
                        "tranId": f"fund_{e.get('id', '')}",
                        "tradeId": e.get("tradeId", ""),
                    })
                elif tx_type in ("TRADE", "TRADE_FEE", "FEE") and "COMMISSION" in types:
                    fee = float(e.get("fee", 0) or 0)
                    if fee:
                        out.append({
                            "symbol": e.get("symbol", symbol or ""),
                            "incomeType": "COMMISSION",
                            "income": -abs(fee),
                            "asset": e.get("currency", "USDT"),
                            "time": int(e.get("transactionTime", 0) or 0),
                            "tranId": f"comm_{e.get('id', '')}",
                            "tradeId": e.get("tradeId", ""),
                        })
        except Exception as e:
            log.warning("Bybit get_transaction_log failed: %s", e)

    out.sort(key=lambda x: x.get("time", 0), reverse=True)
    return out[:limit]


def bybit_transfer(asset: str, amount: float, transfer_type: int) -> dict:
    """Internal transfer between sub-accounts. With UNIFIED accounts the
    spot↔futures transfer is usually unnecessary; kept for parity."""
    if transfer_type not in (1, 2):
        raise ValueError("transfer_type must be 1 (spot->futures) or 2 (futures->spot)")
    log.info("Bybit transfer is a no-op for UNIFIED accounts (asset=%s, amt=%s)",
             asset, amount)
    return {"tranId": f"noop_{int(time.time())}", "status": "SUCCESS"}


def bybit_batch_orders(orders: list[dict]) -> list[dict]:
    if len(orders) > 10:
        raise ValueError(f"Bybit batch supports max 10 orders, got {len(orders)}")
    request = []
    for o in orders:
        side_u = (o.get("side", "") or "").upper()
        order_type_u = (o.get("type") or o.get("orderType", "") or "").upper()
        bybit_type, is_trigger = _ORDER_TYPE_MAP.get(order_type_u, ("Limit", False))
        entry = {
            "symbol": o["symbol"],
            "side": "Buy" if side_u == "BUY" else "Sell",
            "orderType": bybit_type,
            "qty": str(o.get("quantity", o.get("qty", 0))),
            "positionIdx": _POSITION_IDX.get((o.get("positionSide", "BOTH") or "BOTH").upper(), 0),
        }
        if bybit_type == "Limit":
            entry["timeInForce"] = _TIF_MAP.get((o.get("timeInForce", "GTC") or "GTC").upper(), "GTC")
            if "price" in o:
                entry["price"] = str(o["price"])
        if o.get("reduceOnly"):
            entry["reduceOnly"] = True
        if is_trigger and o.get("stopPrice") is not None:
            entry["triggerPrice"] = str(o["stopPrice"])
            entry["triggerDirection"] = 1 if entry["side"] == "Buy" else 2
        if o.get("clientOrderId"):
            entry["orderLinkId"] = o["clientOrderId"]
        request.append(entry)

    _private_limiter.acquire()
    raw = _retry(_get_auth_http().place_batch_order, category="linear", request=request)
    result = _unwrap(raw)
    return [
        {
            "orderId": r.get("orderId", ""),
            "clientOrderId": r.get("orderLinkId", ""),
            "symbol": r.get("symbol", ""),
        }
        for r in result.get("list", [])
    ]
