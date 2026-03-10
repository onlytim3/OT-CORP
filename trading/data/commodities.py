"""Commodity data from yfinance and FRED (both free)."""

import requests
import pandas as pd
import yfinance as yf
from trading.config import COMMODITY_ETFS, FRED_BASE, FRED_API_KEY
from trading.data.cache import cached


def get_etf_prices() -> dict:
    """Get current prices for commodity ETFs via yfinance.

    Returns dict of {name: {symbol, price, change_pct, volume}}
    """
    result = {}
    symbols = list(COMMODITY_ETFS.values())
    tickers = yf.Tickers(" ".join(symbols))
    for name, symbol in COMMODITY_ETFS.items():
        try:
            ticker = tickers.tickers[symbol]
            info = ticker.fast_info
            result[name] = {
                "symbol": symbol,
                "price": info.last_price,
                "previous_close": info.previous_close,
                "change_pct": ((info.last_price - info.previous_close) / info.previous_close * 100)
                if info.previous_close
                else 0,
            }
        except Exception:
            result[name] = {"symbol": symbol, "price": None, "previous_close": None, "change_pct": None}
    return result


@cached(ttl=300)
def get_etf_history(symbol: str, period: str = "3mo") -> pd.DataFrame:
    """Get historical data for a commodity ETF.

    Args:
        symbol: ETF ticker (e.g., 'GLD', 'USO')
        period: '1mo', '3mo', '6mo', '1y', '2y', '5y', 'max'

    Returns DataFrame with OHLCV data.
    """
    ticker = yf.Ticker(symbol)
    return ticker.history(period=period)


@cached(ttl=300)
def get_fred_series(series_id: str, limit: int = 90) -> pd.DataFrame:
    """Get economic data from FRED.

    Popular series:
      GOLDAMGBD228NLBM — Gold price (London fix)
      DCOILWTICO — WTI crude oil
      DCOILBRENTEU — Brent crude oil
      DHHNGSP — Henry Hub natural gas
      DEXUSEU — EUR/USD exchange rate
      CPIAUCSL — CPI (inflation)
      DGS10 — 10-year Treasury yield
    """
    if not FRED_API_KEY:
        raise ValueError("FRED_API_KEY not set in .env")
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
    data = resp.json()["observations"]
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df[["date", "value"]].dropna()
    df.set_index("date", inplace=True)
    df.sort_index(inplace=True)
    return df


def get_commodity_prices_from_fred() -> dict:
    """Get latest commodity prices from FRED (free, no yfinance dependency).

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
        except Exception:
            result[name] = {"value": None, "date": None, "series_id": series_id}
    return result
