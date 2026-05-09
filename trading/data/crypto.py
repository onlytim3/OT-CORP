"""Crypto market data — Bybit primary, Alpaca fallback.

v4: Migrated from Alpaca to Bybit klines for all crypto data.
    Same function signatures so all strategies continue working.
    Uses Bybit public endpoints (no auth needed for market data).
"""

import logging

import pandas as pd

from trading.config import CRYPTO_SYMBOLS, BYBIT_SYMBOLS, DEFAULT_COINS
from trading.data.cache import cached

log = logging.getLogger(__name__)


def _get_bybit_klines(symbol: str, interval: str = "1h", limit: int = 500) -> pd.DataFrame:
    """Fetch klines from Bybit and return as DataFrame.

    The bybit_client.get_bybit_klines already returns a properly-indexed DataFrame
    with open, high, low, close, volume columns.
    """
    from trading.execution.bybit_client import get_bybit_klines

    try:
        df = get_bybit_klines(symbol, interval=interval, limit=limit)
        return df
    except Exception as e:
        log.error("Bybit klines failed for %s: %s", symbol, e)
        return pd.DataFrame()


def _get_bybit_ticker(bybit_sym: str) -> dict:
    """Get 24h ticker from Bybit."""
    from trading.execution.bybit_client import get_bybit_ticker_24h
    try:
        return get_bybit_ticker_24h(bybit_sym)
    except Exception as e:
        log.error("Bybit ticker failed for %s: %s", bybit_sym, e)
        return {}


class DataValidationError(Exception):
    """Raised when API response fails validation."""
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coin_to_symbol(coin_id: str) -> str | None:
    """Map CoinGecko coin ID to Alpaca symbol (e.g., 'bitcoin' -> 'BTC/USD')."""
    return CRYPTO_SYMBOLS.get(coin_id)


def _symbol_to_coin(symbol: str) -> str | None:
    """Map Alpaca symbol to CoinGecko coin ID (e.g., 'BTC/USD' -> 'bitcoin')."""
    reverse = {v: k for k, v in CRYPTO_SYMBOLS.items()}
    return reverse.get(symbol)


def _all_symbols(coin_ids: list[str] | None = None) -> list[str]:
    """Convert coin IDs to Alpaca symbols, filtering out unknowns."""
    if coin_ids is None:
        coin_ids = DEFAULT_COINS
    symbols = []
    for cid in coin_ids:
        sym = _coin_to_symbol(cid)
        if sym:
            symbols.append(sym)
        else:
            log.warning("Unknown coin ID: %s — skipping", cid)
    return symbols


# ---------------------------------------------------------------------------
# Public API — same signatures as v2 (CoinGecko) so strategies don't change
# ---------------------------------------------------------------------------

def get_prices(coin_ids: list[str] | None = None) -> dict:
    """Get current prices for a list of coins.

    Returns dict of {coin_id: {usd: price, usd_24h_change: pct, usd_24h_vol: vol, usd_market_cap: cap}}

    Backward-compatible with CoinGecko format used by all strategies.
    """
    if coin_ids is None:
        coin_ids = DEFAULT_COINS

    result = {}
    for coin_id in coin_ids:
        bybit_sym = BYBIT_SYMBOLS.get(coin_id)
        if not bybit_sym:
            continue

        ticker = _get_bybit_ticker(bybit_sym)
        if not ticker:
            continue

        price = float(ticker.get("lastPrice", 0))
        if price <= 0:
            log.warning("Invalid price for %s: %s — skipping", coin_id, price)
            continue

        result[coin_id] = {
            "usd": price,
            "usd_24h_change": float(ticker.get("priceChangePercent", 0)),
            "usd_24h_vol": float(ticker.get("quoteVolume", 0)),
            "usd_market_cap": 0,
        }

    return result


@cached(ttl=300)
def get_market_data(coin_ids: list[str] | None = None) -> pd.DataFrame:
    """Get detailed market data including 7d and 30d price changes.

    Returns DataFrame with columns: id, symbol, name, current_price,
    market_cap, total_volume, price_change_7d, price_change_30d, price_change_24h

    Uses Bybit 24h tickers + daily klines for 7d/30d changes.
    """
    if coin_ids is None:
        coin_ids = DEFAULT_COINS

    rows = []
    for coin_id in coin_ids:
        bybit_sym = BYBIT_SYMBOLS.get(coin_id)
        if not bybit_sym:
            continue

        ticker = _get_bybit_ticker(bybit_sym)
        if not ticker:
            continue

        price = float(ticker.get("lastPrice", 0))
        if price <= 0:
            continue

        # Get 30d daily klines for 7d/30d changes
        change_7d = 0.0
        change_30d = 0.0
        try:
            daily_df = _get_bybit_klines(bybit_sym, interval="1d", limit=31)
            if not daily_df.empty:
                closes = daily_df["close"].values
                if len(closes) >= 7:
                    change_7d = (price - closes[-7]) / closes[-7] * 100
                if len(closes) >= 30:
                    change_30d = (price - closes[-30]) / closes[-30] * 100
                elif len(closes) >= 2:
                    change_30d = (price - closes[0]) / closes[0] * 100
        except Exception as e:
            log.debug("Daily kline change calc failed for %s: %s", coin_id, e)

        short_sym = bybit_sym.replace("USDT", "").lower()

        rows.append({
            "id": coin_id,
            "symbol": short_sym,
            "name": coin_id.replace("-", " ").title(),
            "current_price": price,
            "market_cap": 0,
            "total_volume": float(ticker.get("quoteVolume", 0)),
            "price_change_7d": change_7d,
            "price_change_30d": change_30d,
            "price_change_24h": float(ticker.get("priceChangePercent", 0)),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    invalid = df["current_price"].isna() | (df["current_price"] <= 0)
    if invalid.any():
        log.warning("Dropping %d coins with invalid prices", invalid.sum())
        df = df[~invalid]

    return df


@cached(ttl=300)
def get_ohlc(coin_id: str, days: int = 30) -> pd.DataFrame:
    """Get OHLC data for a coin.

    Args:
        coin_id: CoinGecko-style coin ID (e.g., 'bitcoin', 'ethereum')
        days: Number of days of history

    Returns DataFrame with columns: open, high, low, close
    indexed by timestamp. Uses hourly klines for <= 30 days, daily for longer.
    """
    bybit_sym = BYBIT_SYMBOLS.get(coin_id)
    if not bybit_sym:
        log.warning("Unknown coin ID for OHLC: %s", coin_id)
        return pd.DataFrame(columns=["open", "high", "low", "close"])

    # Use hourly klines for short periods, daily for longer
    if days <= 30:
        interval = "4h"
        limit = min(days * 6, 500)  # 6 candles per day at 4h
    else:
        interval = "1d"
        limit = min(days, 500)

    df = _get_bybit_klines(bybit_sym, interval=interval, limit=limit)

    if df.empty:
        log.warning("Empty OHLC data for %s", coin_id)
        return pd.DataFrame(columns=["open", "high", "low", "close"])

    df.dropna(subset=["close"], inplace=True)

    if df.empty:
        log.warning("All OHLC rows invalid for %s", coin_id)
        return df

    return df[["open", "high", "low", "close"]]


@cached(ttl=300)
def get_historical_prices(coin_id: str, days: int = 90) -> pd.DataFrame:
    """Get historical daily prices for backtesting.

    Args:
        coin_id: CoinGecko-style coin ID (e.g., 'bitcoin')
        days: Number of days of history

    Returns DataFrame with columns: price, volume
    indexed by timestamp (daily).
    """
    bybit_sym = BYBIT_SYMBOLS.get(coin_id)
    if not bybit_sym:
        raise DataValidationError(f"Unknown coin ID: {coin_id}")

    df = _get_bybit_klines(bybit_sym, interval="1d", limit=min(days, 500))

    if df.empty:
        raise DataValidationError(f"Empty historical data for {coin_id}")

    result = pd.DataFrame({
        "price": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df["volume"], errors="coerce") if "volume" in df.columns else 0,
    }, index=df.index)

    result.dropna(subset=["price"], inplace=True)

    if result.empty:
        raise DataValidationError(f"All historical rows invalid for {coin_id}")

    return result
