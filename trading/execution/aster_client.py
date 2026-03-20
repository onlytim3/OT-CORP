"""AsterDex perpetual futures API client for trading execution + market data.

Implements EIP-712 signed authentication for trading endpoints.
Public market data endpoints (klines, orderbook, funding, tickers) require no auth.

Dependencies: eth-account, requests, pandas
"""

import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from typing import Any, Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------
ASTER_FUTURES_BASE = "https://fapi.asterdex.com"
ASTER_FUTURES_V3_BASE = "https://fapi3.asterdex.com"
ASTER_WS_BASE = "wss://fstream.asterdex.com"
ASTER_SPOT_BASE = "https://sapi.asterdex.com"

# ---------------------------------------------------------------------------
# EIP-712 domain (AsterDex signature scheme)
# ---------------------------------------------------------------------------
_EIP712_DOMAIN = {
    "name": "AsterSignTransaction",
    "version": "1",
    "chainId": 1666,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}

# ---------------------------------------------------------------------------
# Retry parameters (matches Alpaca client pattern)
# ---------------------------------------------------------------------------
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds
RETRY_BACKOFF_FACTOR = 2.0  # exponential: 2s, 4s, 8s

# HTTP status codes that should NOT be retried (client errors / business logic)
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404, 422})

# Error message keywords that indicate non-retryable conditions
_NON_RETRYABLE_KEYWORDS = frozenset({
    "insufficient",
    "invalid symbol",
    "invalid side",
    "invalid quantity",
    "invalid price",
    "order does not exist",
    "api-key",
    "signature",
    "unauthorized",
})


# ---------------------------------------------------------------------------
# Config loading (lazy, from env vars)
# ---------------------------------------------------------------------------

def _get_aster_config() -> dict:
    """Load AsterDex credentials from environment variables.

    Supports two auth modes:
      1. HMAC API key auth (preferred): ASTER_API_KEY + ASTER_API_SECRET
      2. EIP-712 Web3 auth: ASTER_USER_ADDRESS + ASTER_SIGNER_ADDRESS + ASTER_PRIVATE_KEY

    Public market data endpoints require no auth at all.
    """
    return {
        "api_key": os.getenv("ASTER_API_KEY", ""),
        "api_secret": os.getenv("ASTER_API_SECRET", ""),
        "user_address": os.getenv("ASTER_USER_ADDRESS", ""),
        "signer_address": os.getenv("ASTER_SIGNER_ADDRESS", ""),
        "private_key": os.getenv("ASTER_PRIVATE_KEY", ""),
    }


def _auth_mode() -> str:
    """Determine which auth mode to use.

    Returns 'hmac' if API key+secret are set, 'eip712' if wallet keys are set,
    or 'none' if nothing is configured.
    """
    cfg = _get_aster_config()
    if cfg["api_key"] and cfg["api_secret"]:
        return "hmac"
    if cfg["user_address"] and cfg["signer_address"] and cfg["private_key"]:
        return "eip712"
    return "none"


def is_aster_configured() -> bool:
    """Check if AsterDex trading credentials are configured.

    Returns True if either HMAC (API key+secret) or EIP-712 (wallet) auth
    is available. Public market data functions work without these.
    """
    return _auth_mode() != "none"


def _require_auth() -> dict:
    """Load and validate auth config, raising on missing credentials."""
    cfg = _get_aster_config()
    mode = _auth_mode()
    if mode == "none":
        raise ValueError(
            "AsterDex auth requires either ASTER_API_KEY + ASTER_API_SECRET "
            "(HMAC auth) or ASTER_USER_ADDRESS + ASTER_SIGNER_ADDRESS + "
            "ASTER_PRIVATE_KEY (EIP-712 auth). Set them in your .env file."
        )
    cfg["_mode"] = mode
    return cfg


# ---------------------------------------------------------------------------
# Signing — dispatches between HMAC (API key) and EIP-712 (wallet)
# ---------------------------------------------------------------------------

def _sign_params(params: dict, cfg: dict) -> dict:
    """Sign request parameters using the configured auth mode.

    HMAC mode: appends timestamp + HMAC-SHA256 signature as query params.
    EIP-712 mode: appends user/signer/nonce + EIP-712 signature.
    """
    mode = cfg.get("_mode", _auth_mode())
    if mode == "hmac":
        return _sign_params_hmac(params, cfg)
    return _sign_params_eip712(params, cfg)


def _sign_params_hmac(params: dict, cfg: dict) -> dict:
    """Sign params with HMAC-SHA256 (Binance-compatible API key auth).

    Adds timestamp and signature to params. The API key is sent as a header.
    """
    params["timestamp"] = int(time.time() * 1000)  # milliseconds
    query_string = urllib.parse.urlencode(params, doseq=True)
    signature = hmac.new(
        cfg["api_secret"].encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    params["signature"] = signature
    return params


def _sign_params_eip712(params: dict, cfg: dict) -> dict:
    """Sign params with EIP-712 typed data (Web3 wallet auth)."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    nonce = str(int(time.time() * 1_000_000))
    params["user"] = cfg["user_address"]
    params["signer"] = cfg["signer_address"]
    params["nonce"] = nonce

    message_str = urllib.parse.urlencode(params, doseq=True)

    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Message": [
                {"name": "content", "type": "string"},
            ],
        },
        "primaryType": "Message",
        "domain": _EIP712_DOMAIN,
        "message": {"content": message_str},
    }

    signable = encode_typed_data(full_message=typed_data)
    signed = Account.sign_message(signable, private_key=cfg["private_key"])
    params["signature"] = signed.signature.hex()
    if not params["signature"].startswith("0x"):
        params["signature"] = "0x" + params["signature"]

    return params


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

def _is_retryable(error: Exception) -> bool:
    """Check if an error is worth retrying (transient network/server issues)."""
    err_str = str(error).lower()
    for keyword in _NON_RETRYABLE_KEYWORDS:
        if keyword in err_str:
            return False

    # Check HTTP status code if available
    if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
        if error.response.status_code in _NON_RETRYABLE_STATUSES:
            return False

    return True


def _retry(func, *args, **kwargs):
    """Execute func with exponential backoff retry."""
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
                "AsterDex API error (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, MAX_RETRIES, delay, e,
            )
            time.sleep(delay)
    raise last_err


# ---------------------------------------------------------------------------
# Rate limiter — prevents hitting AsterDex API rate limits
# ---------------------------------------------------------------------------

import threading

class _RateLimiter:
    """Thread-safe sliding-window rate limiter.

    AsterDex allows ~1200 requests/minute for public endpoints and
    ~300/minute for private. We enforce conservative limits to stay safe
    when 22 strategies fire in a single cycle.
    """

    def __init__(self, max_requests: int = 600, window_seconds: float = 60.0):
        self._max = max_requests
        self._window = window_seconds
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def acquire(self):
        """Block until a request slot is available."""
        while True:
            with self._lock:
                now = time.time()
                cutoff = now - self._window
                self._timestamps = [t for t in self._timestamps if t > cutoff]
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return
                # Wait until the oldest request expires
                wait_time = self._timestamps[0] - cutoff + 0.05
            time.sleep(wait_time)

    @property
    def current_count(self) -> int:
        with self._lock:
            cutoff = time.time() - self._window
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            return len(self._timestamps)


# Shared rate limiters for public and private endpoints
_public_limiter = _RateLimiter(max_requests=600, window_seconds=60.0)
_private_limiter = _RateLimiter(max_requests=200, window_seconds=60.0)


# ---------------------------------------------------------------------------
# HTTP session singletons (lazy)
# ---------------------------------------------------------------------------

_public_session: Optional[requests.Session] = None
_auth_session: Optional[requests.Session] = None


def _get_public_session() -> requests.Session:
    """Return the singleton requests.Session for unauthenticated endpoints."""
    global _public_session
    if _public_session is None:
        _public_session = requests.Session()
        _public_session.headers.update({
            "Accept": "application/json",
            "User-Agent": "AsterDex-Python-Client/1.0",
        })
        log.debug("Created AsterDex public HTTP session")
    return _public_session


def _get_auth_session() -> requests.Session:
    """Return the singleton requests.Session for authenticated endpoints.

    For HMAC auth, sets the X-MBX-APIKEY header (Binance-compatible).
    """
    global _auth_session
    if _auth_session is None:
        cfg = _require_auth()
        _auth_session = requests.Session()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "AsterDex-Python-Client/1.0",
        }
        # HMAC mode: API key goes in header
        if cfg.get("_mode") == "hmac":
            headers["X-MBX-APIKEY"] = cfg["api_key"]
        log.debug("Created AsterDex authenticated HTTP session (mode=%s)",
                  cfg.get("_mode", "unknown"))
        _auth_session.headers.update(headers)
    return _auth_session


# ---------------------------------------------------------------------------
# Low-level request helpers
# ---------------------------------------------------------------------------

def _public_get(path: str, params: Optional[dict] = None) -> Any:
    """Make an unauthenticated GET request to the AsterDex futures API."""
    _public_limiter.acquire()
    session = _get_public_session()
    url = f"{ASTER_FUTURES_BASE}{path}"

    # Strip None values from params
    if params:
        params = {k: v for k, v in params.items() if v is not None}

    def _do_request():
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    return _retry(_do_request)


def _auth_request(method: str, path: str, params: Optional[dict] = None) -> Any:
    """Make an authenticated request to the AsterDex futures API.

    Signs the params, then sends as form-encoded body (POST/DELETE)
    or query params (GET).

    Note: AsterDex auth endpoints use /fapi/v1/, public use /fapi/v3/.
    """
    _private_limiter.acquire()
    cfg = _require_auth()
    session = _get_auth_session()
    # Auth endpoints must use v1 (v3 returns signature errors)
    auth_path = path.replace("/fapi/v3/", "/fapi/v1/")
    url = f"{ASTER_FUTURES_BASE}{auth_path}"

    if params is None:
        params = {}
    # Strip None values
    params = {k: v for k, v in params.items() if v is not None}

    # Sign the parameters
    signed_params = _sign_params(params, cfg)

    def _do_request():
        if method.upper() == "GET":
            resp = session.get(url, params=signed_params, timeout=30)
        elif method.upper() == "POST":
            resp = session.post(url, data=signed_params, timeout=30)
        elif method.upper() == "DELETE":
            resp = session.delete(url, data=signed_params, timeout=30)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        if resp.status_code >= 400:
            # Parse and include the actual error body in the exception
            err_msg = ""
            try:
                err_body = resp.json()
                err_msg = err_body.get("msg", str(err_body))
                log.error("AsterDex %d error on %s: %s", resp.status_code, auth_path, err_body)
            except Exception:
                err_msg = resp.text[:500]
                log.error("AsterDex %d error on %s: %s", resp.status_code, auth_path, err_msg)
            raise requests.HTTPError(
                f"AsterDex {resp.status_code} on {auth_path}: {err_msg}",
                response=resp,
            )
        data = resp.json()

        # AsterDex returns {"code": -xxxx, "msg": "..."} on logical errors
        if isinstance(data, dict) and data.get("code") and int(data["code"]) < 0:
            raise RuntimeError(
                f"AsterDex API error {data['code']}: {data.get('msg', 'unknown')}"
            )

        return data

    return _retry(_do_request)


# ---------------------------------------------------------------------------
# Market data functions (NO AUTH -- public endpoints)
# ---------------------------------------------------------------------------

def get_aster_exchange_info() -> dict:
    """Get exchange info including all symbols, filters, and trading rules.

    Returns the full exchangeInfo response as a dict with 'symbols' list
    containing contract specifications, price/quantity filters, etc.
    """
    log.debug("Fetching AsterDex exchange info")
    return _public_get("/fapi/v3/exchangeInfo")


def get_aster_klines(
    symbol: str,
    interval: str = "1h",
    limit: int = 500,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
) -> pd.DataFrame:
    """Get OHLCV candlestick data for a symbol.

    Args:
        symbol: Trading pair, e.g. 'BTCUSDT'.
        interval: Candle interval (1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,3d,1w,1M).
        limit: Number of candles (max 1500).
        start_time: Start timestamp in milliseconds.
        end_time: End timestamp in milliseconds.

    Returns:
        DataFrame with columns: open, high, low, close, volume,
        close_time, quote_volume, trades, taker_buy_base_vol, taker_buy_quote_vol.
        Indexed by open_time as datetime.
    """
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": min(limit, 1500),
        "startTime": start_time,
        "endTime": end_time,
    }

    log.debug("Fetching %s klines: interval=%s limit=%d", symbol, interval, limit)
    data = _public_get("/fapi/v3/klines", params)

    if not data:
        log.warning("Empty klines response for %s", symbol)
        return pd.DataFrame(columns=[
            "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base_vol", "taker_buy_quote_vol",
        ])

    columns = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base_vol", "taker_buy_quote_vol", "_ignore",
    ]
    df = pd.DataFrame(data, columns=columns)
    df.drop(columns=["_ignore"], inplace=True, errors="ignore")

    # Convert numeric columns
    numeric_cols = ["open", "high", "low", "close", "volume",
                    "quote_volume", "taker_buy_base_vol", "taker_buy_quote_vol"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trades"] = pd.to_numeric(df["trades"], errors="coerce", downcast="integer")

    # Convert timestamps to datetime and set index
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df.set_index("open_time", inplace=True)

    return df


def get_aster_orderbook(symbol: str, limit: int = 100) -> dict:
    """Get the order book (depth) for a symbol.

    Args:
        symbol: Trading pair, e.g. 'BTCUSDT'.
        limit: Depth limit (5, 10, 20, 50, 100, 500, 1000).

    Returns:
        Dict with 'bids' and 'asks' as lists of [price, quantity] pairs,
        plus 'lastUpdateId' and 'symbol'.
    """
    valid_limits = {5, 10, 20, 50, 100, 500, 1000}
    if limit not in valid_limits:
        # Round up to nearest valid limit
        limit = min(v for v in valid_limits if v >= limit) if limit < 1000 else 1000

    log.debug("Fetching %s orderbook depth=%d", symbol, limit)
    data = _public_get("/fapi/v3/depth", {"symbol": symbol, "limit": limit})

    # Convert string prices/quantities to floats for easier consumption
    if "bids" in data:
        data["bids"] = [[float(p), float(q)] for p, q in data["bids"]]
    if "asks" in data:
        data["asks"] = [[float(p), float(q)] for p, q in data["asks"]]
    data["symbol"] = symbol

    return data


def get_aster_funding_rates(
    symbol: Optional[str] = None,
    limit: int = 100,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
) -> list[dict]:
    """Get funding rate history.

    Args:
        symbol: Trading pair (optional, returns all if None).
        limit: Number of records (default 100).
        start_time: Start timestamp in milliseconds.
        end_time: End timestamp in milliseconds.

    Returns list of dicts with symbol, fundingRate, fundingTime, etc.
    """
    params = {
        "symbol": symbol,
        "limit": limit,
        "startTime": start_time,
        "endTime": end_time,
    }
    log.debug("Fetching funding rates: symbol=%s limit=%d", symbol, limit)
    data = _public_get("/fapi/v3/fundingRate", params)

    # Convert rate strings to floats
    for entry in data:
        if "fundingRate" in entry:
            entry["fundingRate"] = float(entry["fundingRate"])
        if "fundingTime" in entry:
            entry["fundingTime"] = int(entry["fundingTime"])

    return data


def get_aster_mark_prices(symbol: Optional[str] = None) -> Any:
    """Get mark price and funding rate info.

    Args:
        symbol: Trading pair (optional). If None, returns all symbols.

    Returns:
        Single dict if symbol specified, list of dicts if None.
        Each contains: symbol, markPrice, indexPrice, lastFundingRate,
        nextFundingTime, interestRate.
    """
    params = {"symbol": symbol} if symbol else {}
    log.debug("Fetching mark prices: symbol=%s", symbol or "ALL")
    data = _public_get("/fapi/v3/premiumIndex", params)

    def _parse_entry(entry: dict) -> dict:
        for key in ("markPrice", "indexPrice", "lastFundingRate", "interestRate"):
            if key in entry:
                entry[key] = float(entry[key])
        for key in ("time", "nextFundingTime"):
            if key in entry:
                entry[key] = int(entry[key])
        return entry

    if isinstance(data, list):
        return [_parse_entry(e) for e in data]
    return _parse_entry(data)


def get_aster_ticker_24h(symbol: Optional[str] = None) -> Any:
    """Get 24-hour price change statistics.

    Args:
        symbol: Trading pair (optional). If None, returns all symbols.

    Returns:
        Single dict if symbol specified, list of dicts if None.
        Contains: priceChange, priceChangePercent, weightedAvgPrice,
        lastPrice, volume, quoteVolume, highPrice, lowPrice, openPrice, etc.
    """
    params = {"symbol": symbol} if symbol else {}
    log.debug("Fetching 24h ticker: symbol=%s", symbol or "ALL")
    data = _public_get("/fapi/v3/ticker/24hr", params)

    def _parse_ticker(t: dict) -> dict:
        float_keys = [
            "priceChange", "priceChangePercent", "weightedAvgPrice",
            "lastPrice", "volume", "quoteVolume", "highPrice", "lowPrice",
            "openPrice", "lastQty",
        ]
        for key in float_keys:
            if key in t:
                try:
                    t[key] = float(t[key])
                except (ValueError, TypeError):
                    pass
        return t

    if isinstance(data, list):
        return [_parse_ticker(t) for t in data]
    return _parse_ticker(data)


def get_aster_book_ticker(symbol: Optional[str] = None) -> Any:
    """Get best bid and ask prices.

    Args:
        symbol: Trading pair (optional). If None, returns all symbols.

    Returns:
        Single dict if symbol specified, list of dicts if None.
        Contains: symbol, bidPrice, bidQty, askPrice, askQty, time.
    """
    params = {"symbol": symbol} if symbol else {}
    log.debug("Fetching book ticker: symbol=%s", symbol or "ALL")
    data = _public_get("/fapi/v3/ticker/bookTicker", params)

    def _parse_book(b: dict) -> dict:
        for key in ("bidPrice", "bidQty", "askPrice", "askQty"):
            if key in b:
                b[key] = float(b[key])
        return b

    if isinstance(data, list):
        return [_parse_book(b) for b in data]
    return _parse_book(data)


# ---------------------------------------------------------------------------
# Trading functions (AUTH REQUIRED)
# ---------------------------------------------------------------------------

def aster_submit_order(
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
    """Place an order on AsterDex perpetual futures.

    Args:
        symbol: Trading pair, e.g. 'BTCUSDT'.
        side: 'BUY' or 'SELL'.
        order_type: LIMIT, MARKET, STOP, STOP_MARKET, TAKE_PROFIT,
                     TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET.
        quantity: Order quantity.
        price: Limit price (required for LIMIT orders).
        time_in_force: GTC, IOC, FOK, GTX (default GTC).
        position_side: BOTH, LONG, SHORT (default BOTH for one-way mode).
        reduce_only: If True, only reduce existing position.
        stop_price: Trigger price for stop/take-profit orders.
        client_order_id: Custom order ID for idempotency.

    Returns:
        Dict with order details (orderId, status, symbol, side, price, etc.).
    """
    side = side.upper()
    order_type = order_type.upper()

    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got '{side}'")

    valid_types = {
        "LIMIT", "MARKET", "STOP", "STOP_MARKET",
        "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET",
    }
    if order_type not in valid_types:
        raise ValueError(f"order_type must be one of {valid_types}, got '{order_type}'")

    if order_type == "LIMIT" and price is None:
        raise ValueError("price is required for LIMIT orders")

    params = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": str(quantity),
        "positionSide": position_side,
    }
    # timeInForce only applies to LIMIT-type orders; MARKET/STOP_MARKET reject it
    if order_type in ("LIMIT", "STOP", "TAKE_PROFIT"):
        params["timeInForce"] = time_in_force
    if price is not None:
        params["price"] = str(price)
    if reduce_only is not None:
        params["reduceOnly"] = str(reduce_only).lower()
    if stop_price is not None:
        params["stopPrice"] = str(stop_price)
    if client_order_id is not None:
        params["newClientOrderId"] = client_order_id

    log.info(
        "Submitting AsterDex order: %s %s %s qty=%s price=%s type=%s",
        side, symbol, order_type, quantity, price, order_type,
    )

    result = _auth_request("POST", "/fapi/v3/order", params)
    log.info(
        "AsterDex order result: %s %s -> orderId=%s status=%s",
        symbol, side, result.get("orderId"), result.get("status"),
    )
    return result


def aster_cancel_order(symbol: str, order_id: Optional[int] = None,
                       client_order_id: Optional[str] = None) -> dict:
    """Cancel an open order.

    Args:
        symbol: Trading pair.
        order_id: AsterDex order ID (numeric).
        client_order_id: Original client order ID (alternative to order_id).

    Returns dict with cancellation details.
    """
    if order_id is None and client_order_id is None:
        raise ValueError("Either order_id or client_order_id must be provided")

    params = {"symbol": symbol}
    if order_id is not None:
        params["orderId"] = order_id
    if client_order_id is not None:
        params["origClientOrderId"] = client_order_id

    log.info("Cancelling AsterDex order: symbol=%s orderId=%s", symbol, order_id or client_order_id)
    result = _auth_request("DELETE", "/fapi/v3/order", params)
    log.info("AsterDex cancel result: orderId=%s status=%s",
             result.get("orderId"), result.get("status"))
    return result


def aster_cancel_all_orders(symbol: str) -> dict:
    """Cancel all open orders for a symbol.

    Args:
        symbol: Trading pair.

    Returns dict confirming cancellation.
    """
    log.info("Cancelling all AsterDex orders for %s", symbol)
    result = _auth_request("DELETE", "/fapi/v3/allOpenOrders", {"symbol": symbol})
    log.info("AsterDex cancel-all result for %s: %s", symbol, result)
    return result


def aster_get_order(symbol: str, order_id: Optional[int] = None,
                    client_order_id: Optional[str] = None) -> dict:
    """Query an order by ID.

    Args:
        symbol: Trading pair.
        order_id: AsterDex order ID.
        client_order_id: Original client order ID (alternative).

    Returns dict with full order details.
    """
    if order_id is None and client_order_id is None:
        raise ValueError("Either order_id or client_order_id must be provided")

    params = {"symbol": symbol}
    if order_id is not None:
        params["orderId"] = order_id
    if client_order_id is not None:
        params["origClientOrderId"] = client_order_id

    return _auth_request("GET", "/fapi/v3/order", params)


def aster_get_open_orders(symbol: Optional[str] = None) -> list:
    """Get all open orders, optionally filtered by symbol.

    Returns list of order dicts.
    """
    params = {}
    if symbol is not None:
        params["symbol"] = symbol
    return _auth_request("GET", "/fapi/v3/openOrders", params)


def aster_get_balance() -> list[dict]:
    """Get futures account balance for all assets.

    Returns list of dicts with: asset, balance, crossWalletBalance,
    availableBalance, crossUnPnl, maxWithdrawAmount.
    """
    log.debug("Fetching AsterDex account balance")
    data = _auth_request("GET", "/fapi/v3/balance", {})
    # Convert numeric strings to floats
    for entry in data:
        for key in ("balance", "crossWalletBalance", "availableBalance",
                     "crossUnPnl", "maxWithdrawAmount"):
            if key in entry:
                try:
                    entry[key] = float(entry[key])
                except (ValueError, TypeError):
                    pass
    return data


def aster_get_account() -> dict:
    """Get full account information.

    Returns dict with: totalWalletBalance, totalUnrealizedProfit,
    totalMarginBalance, availableBalance, positions, assets, etc.
    """
    log.debug("Fetching AsterDex account info")
    data = _auth_request("GET", "/fapi/v3/account", {})

    # Parse top-level numeric fields
    float_keys = [
        "totalWalletBalance", "totalUnrealizedProfit", "totalMarginBalance",
        "availableBalance", "totalCrossWalletBalance", "totalCrossUnPnl",
        "totalInitialMargin", "totalMaintMargin",
    ]
    for key in float_keys:
        if key in data:
            try:
                data[key] = float(data[key])
            except (ValueError, TypeError):
                pass

    return data


def aster_get_positions() -> list[dict]:
    """Get all position information.

    Returns list of dicts with: symbol, positionSide, positionAmt,
    entryPrice, markPrice, unRealizedProfit, liquidationPrice, leverage,
    marginType, isolatedMargin, etc.
    """
    log.debug("Fetching AsterDex positions")
    data = _auth_request("GET", "/fapi/v3/positionRisk", {})

    for pos in data:
        float_keys = [
            "positionAmt", "entryPrice", "markPrice", "unRealizedProfit",
            "liquidationPrice", "leverage", "isolatedMargin", "notional",
        ]
        for key in float_keys:
            if key in pos:
                try:
                    pos[key] = float(pos[key])
                except (ValueError, TypeError):
                    pass

    return data


def aster_set_leverage(symbol: str, leverage: int) -> dict:
    """Set leverage for a symbol.

    Args:
        symbol: Trading pair.
        leverage: Leverage value (1-125).

    Returns dict with symbol, leverage, maxNotionalValue.
    """
    if not 1 <= leverage <= 125:
        raise ValueError(f"Leverage must be 1-125, got {leverage}")

    log.info("Setting AsterDex leverage: %s -> %dx", symbol, leverage)
    result = _auth_request("POST", "/fapi/v3/leverage", {
        "symbol": symbol,
        "leverage": leverage,
    })
    log.info("AsterDex leverage set: %s = %dx", symbol, result.get("leverage"))
    return result


def aster_set_margin_type(symbol: str, margin_type: str) -> dict:
    """Set margin type for a symbol.

    Args:
        symbol: Trading pair.
        margin_type: 'ISOLATED' or 'CROSSED'.

    Returns dict confirming the change.
    """
    margin_type = margin_type.upper()
    if margin_type not in ("ISOLATED", "CROSSED"):
        raise ValueError(f"margin_type must be 'ISOLATED' or 'CROSSED', got '{margin_type}'")

    log.info("Setting AsterDex margin type: %s -> %s", symbol, margin_type)
    return _auth_request("POST", "/fapi/v3/marginType", {
        "symbol": symbol,
        "marginType": margin_type,
    })


def aster_get_trades(symbol: str, limit: int = 500) -> list:
    """Get account trade history for a symbol.

    Args:
        symbol: Trading pair.
        limit: Number of trades (default 500).

    Returns list of trade dicts.
    """
    log.debug("Fetching AsterDex trades: symbol=%s limit=%d", symbol, limit)
    return _auth_request("GET", "/fapi/v3/userTrades", {
        "symbol": symbol,
        "limit": limit,
    })


def aster_get_income(
    symbol: Optional[str] = None,
    income_type: Optional[str] = None,
    limit: int = 100,
) -> list:
    """Get income/PnL history.

    Args:
        symbol: Trading pair (optional).
        income_type: Filter by type (e.g. REALIZED_PNL, FUNDING_FEE, COMMISSION).
        limit: Number of records.

    Returns list of income entries.
    """
    params = {"limit": limit}
    if symbol:
        params["symbol"] = symbol
    if income_type:
        params["incomeType"] = income_type
    return _auth_request("GET", "/fapi/v3/income", params)


def aster_transfer(
    asset: str,
    amount: float,
    transfer_type: int,
) -> dict:
    """Transfer assets between spot and futures wallets.

    Args:
        asset: Asset name, e.g. 'USDT'.
        amount: Amount to transfer.
        transfer_type: 1 = spot to futures, 2 = futures to spot.

    Returns dict with tranId.
    """
    if transfer_type not in (1, 2):
        raise ValueError("transfer_type must be 1 (spot->futures) or 2 (futures->spot)")

    direction = "spot->futures" if transfer_type == 1 else "futures->spot"
    log.info("AsterDex transfer: %s %s %s", amount, asset, direction)
    return _auth_request("POST", "/fapi/v3/asset/wallet/transfer", {
        "asset": asset,
        "amount": str(amount),
        "type": transfer_type,
    })


# ---------------------------------------------------------------------------
# Open Interest & Position Data (NO AUTH — public endpoints)
# ---------------------------------------------------------------------------

def get_aster_open_interest(symbol: str) -> dict:
    """Get current open interest for a symbol.

    Args:
        symbol: Trading pair, e.g. 'BTCUSDT'.

    Returns dict with openInterest, symbol, time.
    """
    log.debug("Fetching open interest for %s", symbol)
    data = _public_get("/fapi/v1/openInterest", {"symbol": symbol})
    if "openInterest" in data:
        data["openInterest"] = float(data["openInterest"])
    return data


def get_aster_open_interest_hist(
    symbol: str,
    period: str = "1h",
    limit: int = 30,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
) -> list[dict]:
    """Get historical open interest data.

    Args:
        symbol: Trading pair.
        period: Data period (5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d).
        limit: Number of records (max 500).

    Returns list of dicts with symbol, sumOpenInterest, sumOpenInterestValue, timestamp.
    """
    params = {
        "symbol": symbol,
        "period": period,
        "limit": min(limit, 500),
        "startTime": start_time,
        "endTime": end_time,
    }
    log.debug("Fetching OI history for %s period=%s limit=%d", symbol, period, limit)
    data = _public_get("/futures/data/openInterestHist", params)
    for entry in data:
        for key in ("sumOpenInterest", "sumOpenInterestValue"):
            if key in entry:
                entry[key] = float(entry[key])
    return data


def get_aster_long_short_ratio(
    symbol: str,
    period: str = "1h",
    limit: int = 30,
) -> list[dict]:
    """Get top trader long/short account ratio.

    Args:
        symbol: Trading pair.
        period: Data period (5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d).
        limit: Number of records.

    Returns list of dicts with symbol, longShortRatio, longAccount, shortAccount, timestamp.
    """
    params = {"symbol": symbol, "period": period, "limit": min(limit, 500)}
    log.debug("Fetching long/short ratio for %s", symbol)
    data = _public_get("/futures/data/topLongShortAccountRatio", params)
    for entry in data:
        for key in ("longShortRatio", "longAccount", "shortAccount"):
            if key in entry:
                entry[key] = float(entry[key])
    return data


def get_aster_taker_buy_sell_volume(
    symbol: str,
    period: str = "1h",
    limit: int = 30,
) -> list[dict]:
    """Get taker buy/sell volume ratio.

    Args:
        symbol: Trading pair.
        period: Data period (5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d).
        limit: Number of records.

    Returns list of dicts with buySellRatio, buyVol, sellVol, timestamp.
    """
    params = {"symbol": symbol, "period": period, "limit": min(limit, 500)}
    log.debug("Fetching taker volume for %s", symbol)
    data = _public_get("/futures/data/takerlongshortRatio", params)
    for entry in data:
        for key in ("buySellRatio", "buyVol", "sellVol"):
            if key in entry:
                entry[key] = float(entry[key])
    return data


# ---------------------------------------------------------------------------
# Batch orders
# ---------------------------------------------------------------------------

def aster_batch_orders(orders: list[dict]) -> list[dict]:
    """Submit up to 5 orders in a single batch request.

    Args:
        orders: List of order param dicts (same fields as aster_submit_order).
                Maximum 5 orders per batch.

    Returns list of order result dicts.
    """
    if len(orders) > 5:
        raise ValueError(f"Batch supports max 5 orders, got {len(orders)}")

    import json
    params = {
        "batchOrders": json.dumps(orders),
    }
    log.info("Submitting AsterDex batch of %d orders", len(orders))
    return _auth_request("POST", "/fapi/v3/batchOrders", params)


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------

def aster_ping() -> bool:
    """Check AsterDex API connectivity. Returns True if reachable."""
    try:
        _public_get("/fapi/v3/ping")
        return True
    except Exception as e:
        log.warning("AsterDex ping failed: %s", e)
        return False


def aster_server_time() -> int:
    """Get AsterDex server time in milliseconds."""
    data = _public_get("/fapi/v3/time")
    return data.get("serverTime", 0)
