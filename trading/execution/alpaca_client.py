"""Alpaca API client for crypto and ETF execution."""

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.data.live import CryptoDataStream
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

from trading.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, TRADING_MODE


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
    """Get account info — cash, portfolio value, buying power."""
    client = get_client()
    account = client.get_account()
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
    positions = client.get_all_positions()
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
    """Submit a market order.

    Args:
        symbol: Trading symbol (e.g., 'BTC/USD' for crypto, 'GLD' for ETF)
        side: 'buy' or 'sell'
        notional: Dollar amount to trade (for fractional)
        qty: Number of shares/coins (alternative to notional)

    Returns dict with order details.
    """
    client = get_client()

    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    # Build order request
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
    order = client.submit_order(order_request)

    return {
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


def get_order_status(order_id: str) -> dict:
    """Check the status of an order."""
    client = get_client()
    order = client.get_order_by_id(order_id)
    return {
        "id": str(order.id),
        "symbol": order.symbol,
        "side": order.side.value,
        "status": order.status.value,
        "filled_avg_price": str(order.filled_avg_price) if order.filled_avg_price else None,
        "filled_qty": str(order.filled_qty) if order.filled_qty else None,
    }


def close_position(symbol: str) -> dict:
    """Close an entire position."""
    client = get_client()
    order = client.close_position(symbol)
    return {
        "id": str(order.id),
        "symbol": order.symbol,
        "side": order.side.value,
        "status": order.status.value,
    }


def get_crypto_quote(symbol: str) -> dict:
    """Get latest crypto quote."""
    client = CryptoHistoricalDataClient()
    from alpaca.data.requests import CryptoLatestQuoteRequest
    req = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
    quotes = client.get_crypto_latest_quote(req)
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
