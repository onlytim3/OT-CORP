"""Alpaca API client for crypto and ETF execution.

v2: Adds exponential-backoff retry on order submission and order status checks.
"""

import logging
import time

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest

from trading.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, TRADING_MODE

log = logging.getLogger(__name__)

# Retry parameters
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
    # Retry on 5xx, timeouts, connection errors
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
    raise last_err  # Should never reach here


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


def submit_order(symbol: str, side: str, notional: float = None, qty: float = None) -> dict:
    """Submit a market order with exponential-backoff retry.

    Args:
        symbol: Trading symbol (e.g., 'BTC/USD' for crypto, 'UGL' for ETF)
        side: 'buy' or 'sell'
        notional: Dollar amount to trade (for fractional)
        qty: Number of shares/coins (alternative to notional)

    Returns dict with order details.
    """
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


def get_crypto_quote(symbol: str) -> dict:
    """Get latest crypto quote."""
    client = CryptoHistoricalDataClient()
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
