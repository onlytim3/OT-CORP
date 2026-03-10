"""Crypto market data from CoinGecko (free API).

v2: Adds response validation on all API calls.
"""

import logging
import time
import requests
import pandas as pd
from trading.config import COINGECKO_BASE, MOMENTUM
from trading.data.cache import cached

log = logging.getLogger(__name__)

_last_request = 0.0


class DataValidationError(Exception):
    """Raised when API response fails validation."""
    pass


def _validate_json(resp, expected_type=dict, label="API"):
    """Validate that a response is valid JSON of the expected type."""
    try:
        data = resp.json()
    except (ValueError, TypeError) as e:
        raise DataValidationError(f"{label}: Invalid JSON response — {e}")

    if isinstance(expected_type, type) and not isinstance(data, expected_type):
        raise DataValidationError(
            f"{label}: Expected {expected_type.__name__}, got {type(data).__name__}"
        )

    # Check for CoinGecko error responses
    if isinstance(data, dict) and "error" in data:
        raise DataValidationError(f"{label}: API error — {data['error']}")
    if isinstance(data, dict) and "status" in data and isinstance(data.get("status"), dict):
        status = data["status"]
        if status.get("error_code"):
            raise DataValidationError(f"{label}: API error {status.get('error_code')} — {status.get('error_message', 'unknown')}")

    return data


def _get(url, params=None):
    """Rate-limited GET with retry — respects CoinGecko free tier (5-15 req/min)."""
    global _last_request
    for attempt in range(4):
        wait = 6.0 - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        try:
            resp = requests.get(url, params=params, timeout=15)
        except requests.exceptions.RequestException as e:
            log.warning("CoinGecko request failed (attempt %d/4): %s", attempt + 1, e)
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
            continue

        if resp.status_code == 429:
            backoff = 15 * (attempt + 1)  # 15s, 30s, 45s, 60s
            log.warning("CoinGecko rate limit hit, backing off %ds", backoff)
            time.sleep(backoff)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()  # Raise on final failure
    return resp


def get_prices(coin_ids: list[str] | None = None) -> dict:
    """Get current prices for a list of coins.

    Returns dict of {coin_id: {usd: price, usd_24h_change: pct, usd_24h_vol: vol}}
    """
    if coin_ids is None:
        coin_ids = MOMENTUM["coins"]
    ids = ",".join(coin_ids)
    resp = _get(
        f"{COINGECKO_BASE}/simple/price",
        params={
            "ids": ids,
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_24hr_vol": "true",
            "include_market_cap": "true",
        },
    )
    data = _validate_json(resp, dict, "get_prices")

    # Validate individual coin data
    for coin_id in coin_ids:
        if coin_id in data:
            price = data[coin_id].get("usd")
            if price is not None and (not isinstance(price, (int, float)) or price <= 0):
                log.warning("Invalid price for %s: %s — removing", coin_id, price)
                del data[coin_id]

    return data


@cached(ttl=300)
def get_market_data(coin_ids: list[str] | None = None) -> pd.DataFrame:
    """Get detailed market data including 7d and 30d price changes.

    Returns DataFrame with columns: id, symbol, name, current_price,
    market_cap, total_volume, price_change_7d, price_change_30d
    """
    if coin_ids is None:
        coin_ids = MOMENTUM["coins"]
    ids = ",".join(coin_ids)
    resp = _get(
        f"{COINGECKO_BASE}/coins/markets",
        params={
            "vs_currency": "usd",
            "ids": ids,
            "order": "market_cap_desc",
            "per_page": len(coin_ids),
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "7d,30d",
        },
    )
    data = _validate_json(resp, list, "get_market_data")

    df = pd.DataFrame(data)
    if df.empty:
        return df

    # Validate price column
    if "current_price" in df.columns:
        invalid = df["current_price"].isna() | (df["current_price"] <= 0)
        if invalid.any():
            log.warning("Dropping %d coins with invalid prices", invalid.sum())
            df = df[~invalid]

    cols = {
        "id": "id",
        "symbol": "symbol",
        "name": "name",
        "current_price": "current_price",
        "market_cap": "market_cap",
        "total_volume": "total_volume",
        "price_change_percentage_7d_in_currency": "price_change_7d",
        "price_change_percentage_30d_in_currency": "price_change_30d",
        "price_change_percentage_24h": "price_change_24h",
    }
    available = {k: v for k, v in cols.items() if k in df.columns}
    return df.rename(columns=available)[list(available.values())]


@cached(ttl=300)
def get_ohlc(coin_id: str, days: int = 30) -> pd.DataFrame:
    """Get OHLC data for a coin. Days: 1, 7, 14, 30, 90, 180, 365, max."""
    resp = _get(
        f"{COINGECKO_BASE}/coins/{coin_id}/ohlc",
        params={"vs_currency": "usd", "days": days},
    )
    data = _validate_json(resp, list, f"get_ohlc({coin_id})")

    if not data:
        log.warning("Empty OHLC data for %s", coin_id)
        return pd.DataFrame(columns=["open", "high", "low", "close"])

    df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close"])

    # Validate OHLC values
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["close"], inplace=True)

    if df.empty:
        log.warning("All OHLC rows invalid for %s", coin_id)
        return df

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


@cached(ttl=300)
def get_historical_prices(coin_id: str, days: int = 90) -> pd.DataFrame:
    """Get historical daily prices for backtesting."""
    resp = _get(
        f"{COINGECKO_BASE}/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": days, "interval": "daily"},
    )
    data = _validate_json(resp, dict, f"get_historical({coin_id})")

    if "prices" not in data:
        raise DataValidationError(f"get_historical({coin_id}): Missing 'prices' key in response")

    prices = pd.DataFrame(data["prices"], columns=["timestamp", "price"])
    prices["price"] = pd.to_numeric(prices["price"], errors="coerce")
    prices.dropna(subset=["price"], inplace=True)
    prices["timestamp"] = pd.to_datetime(prices["timestamp"], unit="ms")
    prices.set_index("timestamp", inplace=True)

    volumes = pd.DataFrame(data.get("total_volumes", []), columns=["timestamp", "volume"])
    if not volumes.empty:
        volumes["volume"] = pd.to_numeric(volumes["volume"], errors="coerce")
        volumes["timestamp"] = pd.to_datetime(volumes["timestamp"], unit="ms")
        volumes.set_index("timestamp", inplace=True)
        return prices.join(volumes)
    return prices
