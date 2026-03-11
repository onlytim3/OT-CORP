"""Commodity / ETF data from Alpaca (IEX feed) and FRED.

v3: ETF prices and history migrated from yfinance to Alpaca IEX feed.
    FRED data remains on FRED API (Alpaca doesn't provide economic data).
    yfinance kept only as fallback for DXY dollar index (not on Alpaca).
"""

import logging
import requests
import pandas as pd
import yfinance as yf

from trading.config import COMMODITY_ETFS, FRED_BASE, FRED_API_KEY
from trading.data.cache import cached

log = logging.getLogger(__name__)


def get_etf_prices() -> dict:
    """Get current prices for commodity ETFs via Alpaca snapshots.

    Returns dict of {name: {symbol, price, previous_close, change_pct}}
    Falls back to yfinance if Alpaca fails (e.g., outside market hours for IEX).
    """
    result = {}
    symbols = list(COMMODITY_ETFS.values())

    # Try Alpaca IEX first
    try:
        from trading.execution.alpaca_client import get_stock_snapshots
        snapshots = get_stock_snapshots(symbols)
        for name, symbol in COMMODITY_ETFS.items():
            if symbol in snapshots and snapshots[symbol].get("price"):
                snap = snapshots[symbol]
                result[name] = {
                    "symbol": symbol,
                    "price": snap["price"],
                    "previous_close": snap.get("prev_close"),
                    "change_pct": snap.get("change_pct", 0.0),
                }
            else:
                result[name] = {"symbol": symbol, "price": None, "previous_close": None, "change_pct": None}
        return result
    except Exception as e:
        log.warning("Alpaca ETF snapshots failed, falling back to yfinance: %s", e)

    # Fallback: yfinance
    try:
        tickers = yf.Tickers(" ".join(symbols))
    except Exception as e:
        log.error("yfinance tickers also failed: %s", e)
        return {name: {"symbol": sym, "price": None, "previous_close": None, "change_pct": None}
                for name, sym in COMMODITY_ETFS.items()}

    for name, symbol in COMMODITY_ETFS.items():
        try:
            ticker = tickers.tickers[symbol]
            info = ticker.fast_info
            price = info.last_price
            prev = info.previous_close

            if price is None or not isinstance(price, (int, float)) or price <= 0:
                log.warning("Invalid price for ETF %s (%s): %s", name, symbol, price)
                result[name] = {"symbol": symbol, "price": None, "previous_close": None, "change_pct": None}
                continue

            result[name] = {
                "symbol": symbol,
                "price": price,
                "previous_close": prev,
                "change_pct": ((price - prev) / prev * 100) if prev and prev > 0 else 0,
            }
        except Exception as e:
            log.warning("Failed to get ETF price for %s (%s): %s", name, symbol, e)
            result[name] = {"symbol": symbol, "price": None, "previous_close": None, "change_pct": None}
    return result


@cached(ttl=300)
def get_etf_history(symbol: str, period: str = "3mo") -> pd.DataFrame:
    """Get historical data for a commodity ETF from Alpaca (IEX feed).

    Args:
        symbol: ETF ticker (e.g., 'UGL', 'AGQ')
        period: '1mo', '3mo', '6mo', '1y' — converted to days for Alpaca

    Returns DataFrame with OHLCV data (columns: Open, High, Low, Close, Volume).
    Falls back to yfinance if Alpaca fails.
    """
    # Convert period string to days
    period_days = {
        "1mo": 30, "3mo": 90, "6mo": 180,
        "1y": 365, "2y": 730, "5y": 1825,
    }
    days = period_days.get(period, 90)

    # Skip Alpaca for non-standard symbols (e.g., DX-Y.NYB dollar index)
    # These are Yahoo Finance-only tickers that Alpaca doesn't carry
    _yfinance_only = {"DX-Y.NYB", "^VIX", "^GSPC", "^DJI", "^IXIC"}

    # Try Alpaca IEX first (if standard symbol)
    try:
        if symbol in _yfinance_only:
            raise ValueError(f"{symbol} is yfinance-only, skipping Alpaca")
        from trading.execution.alpaca_client import get_stock_daily_bars
        df = get_stock_daily_bars(symbol, days=days)
        if not df.empty:
            # Normalize column names to match yfinance format (strategies expect capitalized)
            rename_map = {}
            for col in df.columns:
                rename_map[col] = col.capitalize() if col in ("open", "high", "low", "close", "volume") else col
            df = df.rename(columns=rename_map)

            # Validate Close column
            if "Close" in df.columns:
                invalid = df["Close"].isna() | (df["Close"] <= 0)
                if invalid.any():
                    log.warning("Dropping %d invalid rows from %s Alpaca history", invalid.sum(), symbol)
                    df = df[~invalid]

            return df
    except Exception as e:
        log.warning("Alpaca ETF history failed for %s, falling back to yfinance: %s", symbol, e)

    # Fallback: yfinance
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period)
    except Exception as e:
        log.error("yfinance history also failed for %s: %s", symbol, e)
        return pd.DataFrame()

    if df.empty:
        log.warning("Empty history for ETF %s (period=%s)", symbol, period)
        return df

    if "Close" in df.columns:
        invalid = df["Close"].isna() | (df["Close"] <= 0)
        if invalid.any():
            log.warning("Dropping %d invalid rows from %s yfinance history", invalid.sum(), symbol)
            df = df[~invalid]

    return df


@cached(ttl=300)
def get_fred_series(series_id: str, limit: int = 90) -> pd.DataFrame:
    """Get economic data from FRED.

    Popular series:
      GOLDAMGBD228NLBM -- Gold price (London fix)
      DCOILWTICO       -- WTI crude oil
      DFII10           -- 10-Year TIPS real yield
      DGS10            -- 10-year Treasury yield
    """
    if not FRED_API_KEY:
        raise ValueError("FRED_API_KEY not set in .env")

    try:
        resp = requests.get(
            f"{FRED_BASE}/series/observations",
            params={
                "series_id": series_id,
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": limit,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.error("FRED API request failed for %s: %s", series_id, e)
        raise

    try:
        json_data = resp.json()
    except ValueError as e:
        log.error("FRED returned invalid JSON for %s: %s", series_id, e)
        raise

    if "observations" not in json_data:
        log.error("FRED response missing 'observations' key for %s", series_id)
        raise ValueError(f"FRED response missing 'observations' for {series_id}")

    data = json_data["observations"]
    if not data:
        log.warning("FRED returned empty data for %s", series_id)
        return pd.DataFrame(columns=["value"])

    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df[["date", "value"]].dropna()
    df.set_index("date", inplace=True)
    df.sort_index(inplace=True)
    return df


def get_commodity_prices_from_fred() -> dict:
    """Get latest commodity prices from FRED.

    Returns dict of {commodity: {value, date, series_id}}
    """
    series = {
        "gold": "GOLDAMGBD228NLBM",
        "oil_wti": "DCOILWTICO",
        "oil_brent": "DCOILBRENTEU",
        "natural_gas": "DHHNGSP",
    }
    result = {}
    for name, series_id in series.items():
        try:
            df = get_fred_series(series_id, limit=1)
            if not df.empty:
                result[name] = {
                    "value": df["value"].iloc[-1],
                    "date": str(df.index[-1].date()),
                    "series_id": series_id,
                }
            else:
                result[name] = {"value": None, "date": None, "series_id": series_id}
        except Exception as e:
            log.warning("Failed to get FRED data for %s: %s", name, e)
            result[name] = {"value": None, "date": None, "series_id": series_id}
    return result
