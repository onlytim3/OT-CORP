"""Paper trading execution — simulated trades with no real money."""

import uuid
from datetime import datetime, timezone

from trading.data.crypto import get_prices
from trading.data.commodities import get_etf_prices
from trading.config import CRYPTO_SYMBOLS


# In-memory paper portfolio
_paper_portfolio = {
    "cash": 300.0,
    "positions": {},
    "orders": [],
}


def reset_paper(starting_cash: float = 300.0):
    """Reset paper trading portfolio."""
    _paper_portfolio["cash"] = starting_cash
    _paper_portfolio["positions"] = {}
    _paper_portfolio["orders"] = []


def get_paper_account() -> dict:
    positions_value = sum(
        p["qty"] * p["current_price"] for p in _paper_portfolio["positions"].values()
    )
    return {
        "cash": _paper_portfolio["cash"],
        "portfolio_value": _paper_portfolio["cash"] + positions_value,
        "buying_power": _paper_portfolio["cash"],
        "equity": _paper_portfolio["cash"] + positions_value,
        "status": "ACTIVE",
        "trading_blocked": False,
        "paper": True,
    }


def _get_current_price(symbol: str) -> float | None:
    """Get current price for a symbol."""
    # Crypto symbols like BTC/USD
    reverse_map = {v: k for k, v in CRYPTO_SYMBOLS.items()}
    if symbol in reverse_map:
        coin_id = reverse_map[symbol]
        prices = get_prices([coin_id])
        if coin_id in prices:
            return prices[coin_id]["usd"]
    # ETF symbols
    try:
        etf_prices = get_etf_prices()
        for name, data in etf_prices.items():
            if data["symbol"] == symbol:
                return data["price"]
    except Exception:
        pass
    return None


def submit_paper_order(symbol: str, side: str, notional: float = None, qty: float = None) -> dict:
    """Execute a simulated trade."""
    price = _get_current_price(symbol)
    if price is None:
        return {"id": None, "status": "rejected", "reason": f"Cannot get price for {symbol}"}

    order_id = str(uuid.uuid4())[:8]

    if side == "buy":
        if notional:
            buy_qty = notional / price
            cost = notional
        elif qty:
            buy_qty = qty
            cost = qty * price
        else:
            return {"id": None, "status": "rejected", "reason": "No notional or qty"}

        if cost > _paper_portfolio["cash"]:
            return {"id": order_id, "status": "rejected", "reason": f"Insufficient cash: ${_paper_portfolio['cash']:.2f} < ${cost:.2f}"}

        _paper_portfolio["cash"] -= cost
        if symbol in _paper_portfolio["positions"]:
            pos = _paper_portfolio["positions"][symbol]
            total_qty = pos["qty"] + buy_qty
            pos["avg_cost"] = (pos["avg_cost"] * pos["qty"] + price * buy_qty) / total_qty
            pos["qty"] = total_qty
            pos["current_price"] = price
        else:
            _paper_portfolio["positions"][symbol] = {
                "qty": buy_qty,
                "avg_cost": price,
                "current_price": price,
            }

    elif side == "sell":
        if symbol not in _paper_portfolio["positions"]:
            return {"id": order_id, "status": "rejected", "reason": f"No position in {symbol}"}

        pos = _paper_portfolio["positions"][symbol]
        sell_qty = qty if qty else pos["qty"]
        if sell_qty > pos["qty"]:
            sell_qty = pos["qty"]

        proceeds = sell_qty * price
        _paper_portfolio["cash"] += proceeds

        pos["qty"] -= sell_qty
        if pos["qty"] <= 0.0001:  # Floating point cleanup
            del _paper_portfolio["positions"][symbol]
        else:
            pos["current_price"] = price

    order = {
        "id": order_id,
        "symbol": symbol,
        "side": side,
        "qty": str(buy_qty if side == "buy" else sell_qty),
        "notional": str(notional) if notional else None,
        "status": "filled",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "filled_avg_price": str(price),
        "filled_qty": str(buy_qty if side == "buy" else sell_qty),
    }
    _paper_portfolio["orders"].append(order)
    return order


def get_paper_positions() -> list[dict]:
    result = []
    for symbol, pos in _paper_portfolio["positions"].items():
        price = _get_current_price(symbol) or pos["current_price"]
        pos["current_price"] = price
        unrealized = (price - pos["avg_cost"]) * pos["qty"]
        result.append({
            "symbol": symbol,
            "qty": pos["qty"],
            "avg_cost": pos["avg_cost"],
            "current_price": price,
            "market_value": pos["qty"] * price,
            "unrealized_pnl": unrealized,
            "unrealized_pnl_pct": (unrealized / (pos["avg_cost"] * pos["qty"])) * 100 if pos["avg_cost"] > 0 else 0,
            "side": "long",
        })
    return result
