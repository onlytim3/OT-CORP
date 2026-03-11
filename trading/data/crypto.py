"""Crypto market data from Alpaca Data API (free, no auth, no rate limits).

v3: Migrated from CoinGecko to Alpaca. Same function signatures so all 10
    strategies continue working without changes. Eliminates CoinGecko rate
    limiting entirely — all prices, OHLC, and historical data come from
    Alpaca's CryptoHistoricalDataClient in a single fast call.
"""

import logging

import pandas as pd

from trading.config import CRYPTO_SYMBOLS, MOMENTUM
from trading.data.cache import cached
from trading.execution.alpaca_client import (
    get_crypto_snapshots,
    get_crypto_bars,
    get_crypto_daily_bars,
)
from alpaca.data.timeframe import TimeFrame

log = logging.getLogger(__name__)


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
        coin_ids = MOMENTUM["coins"]
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
    symbols = _all_symbols(coin_ids)
    if not symbols:
        return {}

    try:
        snapshots = get_crypto_snapshots(symbols)
    except Exception as e:
        log.error("Alpaca crypto snapshot failed: %s", e)
        return {}

    result = {}
    for sym, snap in snapshots.items():
        coin_id = _symbol_to_coin(sym)
        if not coin_id:
            continue
        price = snap.get("price")
        if price is None or price <= 0:
            log.warning("Invalid price for %s (%s): %s — skipping", coin_id, sym, price)
            continue
        result[coin_id] = {
            "usd": price,
            "usd_24h_change": snap.get("change_pct", 0.0),
            "usd_24h_vol": snap.get("daily_volume", 0.0),
            "usd_market_cap": 0,  # Alpaca doesn't provide market cap
        }

    return result


@cached(ttl=300)
def get_market_data(coin_ids: list[str] | None = None) -> pd.DataFrame:
    """Get detailed market data including 7d and 30d price changes.

    Returns DataFrame with columns: id, symbol, name, current_price,
    market_cap, total_volume, price_change_7d, price_change_30d, price_change_24h

    7d and 30d changes are computed from Alpaca daily bars.
    """
    if coin_ids is None:
        coin_ids = MOMENTUM["coins"]

    symbols = _all_symbols(coin_ids)
    if not symbols:
        return pd.DataFrame()

    # Get current snapshots (prices + 24h change)
    try:
        snapshots = get_crypto_snapshots(symbols)
    except Exception as e:
        log.error("Alpaca snapshot failed for market data: %s", e)
        return pd.DataFrame()

    # Get 30d daily bars to compute 7d and 30d changes
    try:
        bars_df = get_crypto_bars(symbols, timeframe=TimeFrame.Day, days=31)
    except Exception as e:
        log.warning("Alpaca daily bars failed, using snapshot only: %s", e)
        bars_df = pd.DataFrame()

    rows = []
    for sym in symbols:
        coin_id = _symbol_to_coin(sym)
        if not coin_id or sym not in snapshots:
            continue

        snap = snapshots[sym]
        price = snap.get("price")
        if price is None or price <= 0:
            continue

        # Compute 7d and 30d changes from bars
        change_7d = 0.0
        change_30d = 0.0
        if not bars_df.empty:
            try:
                if isinstance(bars_df.index, pd.MultiIndex):
                    sym_bars = bars_df.xs(sym, level="symbol")
                else:
                    sym_bars = bars_df
                closes = sym_bars["close"].values
                if len(closes) >= 7:
                    change_7d = (price - closes[-7]) / closes[-7] * 100
                if len(closes) >= 30:
                    change_30d = (price - closes[-30]) / closes[-30] * 100
                elif len(closes) >= 2:
                    change_30d = (price - closes[0]) / closes[0] * 100
            except Exception as e:
                log.debug("Bar change calc failed for %s: %s", sym, e)

        # Extract short symbol (BTC, ETH, etc.)
        short_sym = sym.replace("/USD", "").lower()

        rows.append({
            "id": coin_id,
            "symbol": short_sym,
            "name": coin_id.replace("-", " ").title(),
            "current_price": price,
            "market_cap": 0,  # Alpaca doesn't provide market cap
            "total_volume": snap.get("daily_volume", 0.0),
            "price_change_7d": change_7d,
            "price_change_30d": change_30d,
            "price_change_24h": snap.get("change_pct", 0.0),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Validate price column
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
    indexed by timestamp. Uses hourly bars for <= 30 days, daily for longer.
    """
    symbol = _coin_to_symbol(coin_id)
    if not symbol:
        log.warning("Unknown coin ID for OHLC: %s", coin_id)
        return pd.DataFrame(columns=["open", "high", "low", "close"])

    # Use hourly bars for short periods (more data points), daily for longer
    timeframe = TimeFrame.Hour if days <= 30 else TimeFrame.Day

    try:
        df = get_crypto_bars(symbol, timeframe=timeframe, days=days)
    except Exception as e:
        log.error("Alpaca bars failed for %s: %s", coin_id, e)
        return pd.DataFrame(columns=["open", "high", "low", "close"])

    if df.empty:
        log.warning("Empty OHLC data for %s", coin_id)
        return pd.DataFrame(columns=["open", "high", "low", "close"])

    # Flatten multi-index if present
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")

    # Validate OHLC values
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["close"], inplace=True)

    if df.empty:
        log.warning("All OHLC rows invalid for %s", coin_id)
        return df

    # Return only OHLC columns (strategies expect this)
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
    symbol = _coin_to_symbol(coin_id)
    if not symbol:
        raise DataValidationError(f"Unknown coin ID: {coin_id}")

    try:
        df = get_crypto_daily_bars(symbol, days=days)
    except Exception as e:
        log.error("Alpaca daily bars failed for %s: %s", coin_id, e)
        raise DataValidationError(f"Failed to get historical data for {coin_id}: {e}")

    if df.empty:
        raise DataValidationError(f"Empty historical data for {coin_id}")

    # Convert to CoinGecko-compatible format: columns = price, volume
    result = pd.DataFrame({
        "price": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df["volume"], errors="coerce") if "volume" in df.columns else 0,
    }, index=df.index)

    result.dropna(subset=["price"], inplace=True)

    if result.empty:
        raise DataValidationError(f"All historical rows invalid for {coin_id}")

    return result
