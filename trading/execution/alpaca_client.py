"""Alpaca API client for trading execution + market data.

v3: Adds comprehensive market data methods (crypto + stock/ETF) via Alpaca Data API.
     Crypto data is free (no auth needed). Stock/ETF data uses free IEX feed.
     Replaces CoinGecko entirely — no rate limiting, faster responses.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestQuoteRequest,
    CryptoSnapshotRequest,
    StockBarsRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame

from trading.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, TRADING_MODE

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry parameters
# ---------------------------------------------------------------------------
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds
RETRY_BACKOFF_FACTOR = 2.0  # exponential: 2s, 4s, 8s

# Errors that should NOT be retried (business logic errors)
_NON_RETRYABLE = frozenset({
    "insufficient",  # insufficient balance/buying power
    "account_blocked",
    "forbidden",
})


def _is_retryable(error: Exception) -> bool:
    """Check if an error is worth retrying (transient network/server issues)."""
    err_str = str(error).lower()
    for keyword in _NON_RETRYABLE:
        if keyword in err_str:
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
                "Alpaca API error (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, MAX_RETRIES, delay, e,
            )
            time.sleep(delay)
    raise last_err


# ---------------------------------------------------------------------------
# Client constructors
# ---------------------------------------------------------------------------

def _is_paper():
    return TRADING_MODE == "paper" or "paper" in ALPACA_BASE_URL


def get_client() -> TradingClient:
    """Get an Alpaca TradingClient."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise ValueError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env. "
            "Sign up at https://alpaca.markets and get your keys from the dashboard."
        )
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=_is_paper())


def _get_crypto_data_client() -> CryptoHistoricalDataClient:
    """Get Alpaca crypto data client (no auth needed — free tier)."""
    return CryptoHistoricalDataClient()


def _get_stock_data_client() -> StockHistoricalDataClient:
    """Get Alpaca stock data client (needs auth, uses free IEX feed)."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY required for stock data")
    return StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


# ---------------------------------------------------------------------------
# Account & Position queries
# ---------------------------------------------------------------------------

def get_account() -> dict:
    """Get account info — cash, portfolio value, buying power, status."""
    client = get_client()
    account = _retry(client.get_account)
    return {
        "cash": float(account.cash),
        "portfolio_value": float(account.portfolio_value),
        "buying_power": float(account.buying_power),
        "equity": float(account.equity),
        "status": account.status,
        "trading_blocked": account.trading_blocked,
        "paper": _is_paper(),
    }


def get_positions_from_alpaca() -> list[dict]:
    """Get all open positions from Alpaca."""
    client = get_client()
    positions = _retry(client.get_all_positions)
    return [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "avg_cost": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "market_value": float(p.market_value),
            "unrealized_pnl": float(p.unrealized_pl),
            "unrealized_pnl_pct": float(p.unrealized_plpc) * 100,
            "side": p.side,
        }
        for p in positions
    ]


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

def submit_order(symbol: str, side: str, notional: float = None, qty: float = None) -> dict:
    """Submit a market order with exponential-backoff retry."""
    client = get_client()
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    kwargs = {
        "symbol": symbol,
        "side": order_side,
        "time_in_force": TimeInForce.GTC,
    }
    if notional is not None:
        kwargs["notional"] = round(notional, 2)
    elif qty is not None:
        kwargs["qty"] = qty
    else:
        raise ValueError("Either notional or qty must be provided")

    order_request = MarketOrderRequest(**kwargs)

    log.info("Submitting %s order: %s %s (notional=%s, qty=%s)",
             side, symbol, order_side, notional, qty)

    order = _retry(client.submit_order, order_request)

    result = {
        "id": str(order.id),
        "symbol": order.symbol,
        "side": order.side.value,
        "qty": str(order.qty) if order.qty else None,
        "notional": str(order.notional) if order.notional else None,
        "status": order.status.value,
        "submitted_at": str(order.submitted_at),
        "filled_avg_price": str(order.filled_avg_price) if order.filled_avg_price else None,
        "filled_qty": str(order.filled_qty) if order.filled_qty else None,
    }
    log.info("Order result: %s %s -> %s", symbol, side, result["status"])
    return result


def get_order_status(order_id: str) -> dict:
    """Check the status of an order with retry."""
    client = get_client()
    order = _retry(client.get_order_by_id, order_id)
    return {
        "id": str(order.id),
        "symbol": order.symbol,
        "side": order.side.value,
        "status": order.status.value,
        "filled_avg_price": str(order.filled_avg_price) if order.filled_avg_price else None,
        "filled_qty": str(order.filled_qty) if order.filled_qty else None,
    }


def close_position(symbol: str) -> dict:
    """Close an entire position with retry."""
    client = get_client()
    log.info("Closing position: %s", symbol)
    order = _retry(client.close_position, symbol)
    return {
        "id": str(order.id),
        "symbol": order.symbol,
        "side": order.side.value,
        "status": order.status.value,
    }


# ---------------------------------------------------------------------------
# Crypto market data  (free — no auth required)
# ---------------------------------------------------------------------------

def get_crypto_quote(symbol: str) -> dict:
    """Get latest crypto quote (bid/ask/mid)."""
    client = _get_crypto_data_client()
    req = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
    quotes = _retry(client.get_crypto_latest_quote, req)
    quote = quotes.get(symbol)
    if quote:
        return {
            "symbol": symbol,
            "bid": float(quote.bid_price),
            "ask": float(quote.ask_price),
            "mid": (float(quote.bid_price) + float(quote.ask_price)) / 2,
            "timestamp": str(quote.timestamp),
        }
    return {"symbol": symbol, "bid": None, "ask": None, "mid": None}


def get_crypto_snapshots(symbols: list[str]) -> dict:
    """Get snapshots for multiple crypto symbols at once.

    Returns dict of {symbol: {price, prev_close, change_pct, daily_volume,
                              daily_open, daily_high, daily_low, daily_close}}.
    No rate limiting — single API call for all symbols.
    """
    client = _get_crypto_data_client()
    req = CryptoSnapshotRequest(symbol_or_symbols=symbols)
    snaps = _retry(client.get_crypto_snapshot, req)
    result = {}
    for sym, s in snaps.items():
        try:
            price = float(s.latest_trade.price)
            prev_close = float(s.previous_daily_bar.close) if s.previous_daily_bar else None
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
            result[sym] = {
                "price": price,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "daily_volume": float(s.daily_bar.volume) if s.daily_bar else 0.0,
                "daily_open": float(s.daily_bar.open) if s.daily_bar else None,
                "daily_high": float(s.daily_bar.high) if s.daily_bar else None,
                "daily_low": float(s.daily_bar.low) if s.daily_bar else None,
                "daily_close": float(s.daily_bar.close) if s.daily_bar else None,
                "timestamp": str(s.latest_trade.timestamp),
            }
        except Exception as e:
            log.warning("Failed to parse snapshot for %s: %s", sym, e)
            result[sym] = {"price": None, "prev_close": None, "change_pct": 0.0}
    return result


def get_crypto_bars(
    symbols: str | list[str],
    timeframe: TimeFrame = TimeFrame.Hour,
    days: int = 14,
) -> pd.DataFrame:
    """Get historical OHLCV bars for one or more crypto symbols.

    Args:
        symbols: Single symbol ('BTC/USD') or list of symbols.
        timeframe: TimeFrame.Minute, Hour, Day, Week, Month.
        days: How many days of history to fetch.

    Returns DataFrame with columns: open, high, low, close, volume, trade_count, vwap.
    Multi-level index: (symbol, timestamp) if multiple symbols.
    """
    client = _get_crypto_data_client()
    if isinstance(symbols, str):
        symbols = [symbols]

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    req = CryptoBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=timeframe,
        start=start,
        end=end,
    )
    bars = _retry(client.get_crypto_bars, req)
    df = bars.df

    if df.empty:
        log.warning("Empty bars for %s (tf=%s, days=%d)", symbols, timeframe, days)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    return df


def get_crypto_daily_bars(symbol: str, days: int = 90) -> pd.DataFrame:
    """Get daily OHLCV bars for a single crypto symbol.

    Convenience wrapper returning a flat DataFrame (no multi-index)
    with columns: open, high, low, close, volume, vwap.
    """
    df = get_crypto_bars(symbol, timeframe=TimeFrame.Day, days=days)
    if df.empty:
        return df

    # Flatten multi-index if present (symbol, timestamp) -> just timestamp
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")

    return df


# ---------------------------------------------------------------------------
# Stock / ETF market data  (auth required — uses free IEX feed)
# ---------------------------------------------------------------------------

def get_stock_snapshots(symbols: list[str]) -> dict:
    """Get current snapshots for stock/ETF symbols.

    Returns dict of {symbol: {price, prev_close, change_pct, daily_volume}}.
    Uses free IEX feed.
    """
    client = _get_stock_data_client()
    req = StockSnapshotRequest(symbol_or_symbols=symbols, feed="iex")
    snaps = _retry(client.get_stock_snapshot, req)
    result = {}
    for sym, s in snaps.items():
        try:
            price = float(s.latest_trade.price) if s.latest_trade else None
            prev_close = float(s.previous_daily_bar.close) if s.previous_daily_bar else None
            change_pct = ((price - prev_close) / prev_close * 100) if price and prev_close else 0.0
            result[sym] = {
                "price": price,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "daily_volume": float(s.daily_bar.volume) if s.daily_bar else 0.0,
            }
        except Exception as e:
            log.warning("Failed to parse stock snapshot for %s: %s", sym, e)
            result[sym] = {"price": None, "prev_close": None, "change_pct": 0.0}
    return result


def get_stock_bars(
    symbols: str | list[str],
    timeframe: TimeFrame = TimeFrame.Day,
    days: int = 30,
) -> pd.DataFrame:
    """Get historical OHLCV bars for stock/ETF symbols (IEX feed).

    Returns DataFrame with columns: open, high, low, close, volume, trade_count, vwap.
    """
    client = _get_stock_data_client()
    if isinstance(symbols, str):
        symbols = [symbols]

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=timeframe,
        start=start,
        end=end,
        feed="iex",
    )
    bars = _retry(client.get_stock_bars, req)
    df = bars.df

    if df.empty:
        log.warning("Empty stock bars for %s (tf=%s, days=%d)", symbols, timeframe, days)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    return df


def get_stock_daily_bars(symbol: str, days: int = 90) -> pd.DataFrame:
    """Get daily OHLCV bars for a single stock/ETF symbol (IEX feed).

    Convenience wrapper — flat DataFrame indexed by timestamp.
    """
    df = get_stock_bars(symbol, timeframe=TimeFrame.Day, days=days)
    if df.empty:
        return df

    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")

    return df
