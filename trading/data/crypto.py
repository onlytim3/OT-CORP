"""Crypto market data from CoinGecko (free API)."""

import time
import requests
import pandas as pd
from trading.config import COINGECKO_BASE, MOMENTUM
from trading.data.cache import cached

_last_request = 0.0


def _get(url, params=None):
    """Rate-limited GET with retry — respects CoinGecko free tier (5-15 req/min)."""
    global _last_request
    for attempt in range(4):
        wait = 6.0 - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            backoff = 15 * (attempt + 1)  # 15s, 30s, 45s, 60s
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
    return resp.json()


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
    data = resp.json()
    df = pd.DataFrame(data)
    if df.empty:
        return df
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
    data = resp.json()
    df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close"])
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
    data = resp.json()
    prices = pd.DataFrame(data["prices"], columns=["timestamp", "price"])
    prices["timestamp"] = pd.to_datetime(prices["timestamp"], unit="ms")
    prices.set_index("timestamp", inplace=True)
    volumes = pd.DataFrame(data["total_volumes"], columns=["timestamp", "volume"])
    volumes["timestamp"] = pd.to_datetime(volumes["timestamp"], unit="ms")
    volumes.set_index("timestamp", inplace=True)
    return prices.join(volumes)
