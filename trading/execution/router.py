"""Execution router — unified interface for order routing.

Routes crypto orders to AsterDex perpetual futures.
Provides the same interface as alpaca_client so the rest of the system
(main.py, scheduler.py, risk manager) doesn't need to change.

Symbol translation: strategies emit Alpaca-style symbols (BTC/USD),
the router converts to AsterDex format (BTCUSDT) before execution.
"""

import logging
import uuid
from typing import Optional

from trading.config import ASTER_SYMBOLS, CRYPTO_SYMBOLS
from trading.data.aster import alpaca_to_aster, aster_to_alpaca

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------

# Alpaca symbol -> AsterDex symbol
_ALPACA_TO_ASTER = {v: ASTER_SYMBOLS[k] for k, v in CRYPTO_SYMBOLS.items()
                    if k in ASTER_SYMBOLS}

# AsterDex symbol -> Alpaca symbol (for position reporting)
_ASTER_TO_ALPACA = {v: k for k, v in _ALPACA_TO_ASTER.items()}

# ---------------------------------------------------------------------------
# AsterDex symbol validation (populated lazily on first order)
# ---------------------------------------------------------------------------

_VALID_ASTER_SYMBOLS: set[str] = set()


def validate_aster_symbols():
    """Fetch valid symbols from AsterDex and cache them."""
    global _VALID_ASTER_SYMBOLS
    try:
        from trading.execution.aster_client import get_aster_exchange_info
        info = get_aster_exchange_info()
        _VALID_ASTER_SYMBOLS = {s["symbol"] for s in info.get("symbols", [])}
        log.info("Validated %d AsterDex symbols", len(_VALID_ASTER_SYMBOLS))
    except Exception as e:
        log.warning("Could not validate AsterDex symbols: %s", e)


def _to_aster(symbol: str) -> str:
    """Convert any symbol format to AsterDex format."""
    # Already AsterDex format
    if symbol.endswith("USDT"):
        aster = symbol
    else:
        # Alpaca format (BTC/USD)
        aster = _ALPACA_TO_ASTER.get(symbol) or alpaca_to_aster(symbol)
        if not aster:
            # Try stripping / and appending USDT
            base = symbol.replace("/USD", "").replace("/", "")
            aster = f"{base}USDT"

    # Warn if converted symbol is not in the validated set
    if _VALID_ASTER_SYMBOLS and aster not in _VALID_ASTER_SYMBOLS:
        log.warning("Symbol %s (from %s) not found in AsterDex exchange info", aster, symbol)

    return aster


def _is_valid_symbol(aster_sym: str) -> bool:
    """Check if a symbol exists on AsterDex."""
    if not _VALID_ASTER_SYMBOLS:
        validate_aster_symbols()
    return not _VALID_ASTER_SYMBOLS or aster_sym in _VALID_ASTER_SYMBOLS


def _to_alpaca(symbol: str) -> str:
    """Convert AsterDex symbol back to Alpaca format for internal tracking."""
    if "/" in symbol:
        return symbol  # Already Alpaca format
    alpaca = _ASTER_TO_ALPACA.get(symbol) or aster_to_alpaca(symbol)
    if alpaca:
        return alpaca
    # Fallback: BTCUSDT -> BTC/USD
    base = symbol.replace("USDT", "")
    return f"{base}/USD"


# ---------------------------------------------------------------------------
# Account & positions (read from AsterDex)
# ---------------------------------------------------------------------------

def get_account() -> dict:
    """Get account info from AsterDex, formatted like Alpaca's response."""
    import os
    from trading.config import TRADING_MODE
    from trading.db.store import get_setting
    from trading.execution.aster_client import aster_get_account, is_aster_configured

    if not is_aster_configured():
        log.warning("AsterDex not configured, returning empty account")
        return {
            "portfolio_value": 0.0,
            "cash": 0.0,
            "buying_power": 0.0,
            "equity": 0.0,
            "paper": True,
            "status": "INACTIVE",
            "trading_blocked": True,
        }

    try:
        acct = aster_get_account()
        total_balance = acct.get("totalWalletBalance", 0.0)
        available = acct.get("availableBalance", 0.0)
        unrealized_pnl = acct.get("totalUnrealizedProfit", 0.0)
        equity = total_balance + unrealized_pnl

        # Paper mode: use simulated balance (PAPER_BALANCE env or $1000 default)
        # so the system can generate realistic signals and position sizing
        is_paper = get_setting("trading_mode", TRADING_MODE) == "paper"
        if is_paper:
            paper_balance = float(os.getenv("PAPER_BALANCE", "1000"))
            equity = max(equity, paper_balance)
            available = max(available, paper_balance)
            total_balance = max(total_balance, paper_balance)

        return {
            "portfolio_value": equity,
            "cash": available,
            "buying_power": available,
            "equity": equity,
            "paper": is_paper,
            "status": "ACTIVE",
            "trading_blocked": False,
            "total_wallet_balance": total_balance,
            "unrealized_pnl": unrealized_pnl,
            "margin_balance": acct.get("totalMarginBalance", 0.0),
        }
    except Exception as e:
        log.error("Failed to get AsterDex account: %s", e)
        return {
            "portfolio_value": 0.0,
            "cash": 0.0,
            "buying_power": 0.0,
            "equity": 0.0,
            "paper": False,
            "status": "ERROR",
            "trading_blocked": True,
        }


def get_positions_from_aster() -> list[dict]:
    """Get positions from AsterDex, formatted like Alpaca's response."""
    from trading.execution.aster_client import aster_get_positions, is_aster_configured

    if not is_aster_configured():
        return []

    try:
        positions = aster_get_positions()
        result = []
        for pos in positions:
            qty = abs(pos.get("positionAmt", 0.0))
            if qty < 1e-10:
                continue  # Skip empty positions

            aster_sym = pos.get("symbol", "")
            alpaca_sym = _to_alpaca(aster_sym)
            entry_price = pos.get("entryPrice", 0.0)
            mark_price = pos.get("markPrice", 0.0)
            unrealized = pos.get("unRealizedProfit", 0.0)
            side = "long" if pos.get("positionAmt", 0) > 0 else "short"

            result.append({
                "symbol": alpaca_sym.replace("/", ""),  # Match Alpaca format: BTCUSD
                "qty": qty,
                "avg_cost": entry_price,
                "current_price": mark_price,
                "market_value": qty * mark_price,
                "unrealized_pl": unrealized,
                "unrealized_plpc": (mark_price - entry_price) / entry_price if entry_price > 0 else 0,
                "side": side,
                "strategy": "",  # AsterDex doesn't track this; DB has it
                "aster_symbol": aster_sym,
                "leverage": pos.get("leverage", 1),
                "liquidation_price": pos.get("liquidationPrice", 0),
            })
        return result
    except Exception as e:
        log.error("Failed to get AsterDex positions: %s", e)
        return []


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

def submit_order(
    symbol: str,
    side: str,
    notional: Optional[float] = None,
    qty: Optional[float] = None,
    order_type: str = "MARKET",
    leverage: int = 1,
    stop_loss_price: Optional[float] = None,
) -> dict:
    """Submit an order to AsterDex, returning Alpaca-compatible response.

    Args:
        symbol: Alpaca-style symbol (BTC/USD) or AsterDex (BTCUSDT).
        side: 'buy' or 'sell'.
        notional: Dollar amount to trade (converted to qty via mark price).
        qty: Exact quantity (overrides notional if both provided).
        order_type: MARKET or LIMIT (default MARKET).
        leverage: Leverage multiplier (default 1x).

    Returns dict matching Alpaca order response format:
        id, status, symbol, side, qty, filled_qty, filled_avg_price, etc.
    """
    from trading.execution.aster_client import (
        aster_submit_order,
        get_aster_mark_prices,
        get_aster_book_ticker,
        aster_set_leverage,
        is_aster_configured,
    )

    # Lazy symbol validation: fetch exchange info once on first order
    if not _VALID_ASTER_SYMBOLS:
        validate_aster_symbols()

    aster_sym = _to_aster(symbol)
    side_upper = side.upper()

    # Reject orders for symbols that don't exist on AsterDex
    if not _is_valid_symbol(aster_sym):
        log.warning("Rejecting order: %s (%s) not listed on AsterDex", aster_sym, symbol)
        from trading.db.store import log_action
        log_action("error", "symbol_not_listed", symbol=symbol,
                   details=f"{aster_sym} is not listed on AsterDex — order skipped")
        return {
            "id": str(uuid.uuid4()),
            "status": "rejected",
            "reason": f"{aster_sym} not listed on AsterDex",
            "symbol": symbol,
            "side": side,
            "qty": 0,
            "filled_qty": 0,
            "filled_avg_price": 0,
        }

    # Map buy/sell to AsterDex BUY/SELL
    if side_upper not in ("BUY", "SELL"):
        side_upper = "BUY" if side.lower() == "buy" else "SELL"

    # Get current price for qty calculation
    try:
        mark_data = get_aster_mark_prices(aster_sym)
        mark_price = mark_data.get("markPrice", 0)
    except Exception:
        mark_price = 0

    # Calculate quantity from notional if needed
    if qty is None and notional is not None and mark_price > 0:
        qty = notional / mark_price

    if qty is None or qty <= 0:
        log.warning("Cannot calculate order qty: notional=%s, price=%s", notional, mark_price)
        return {
            "id": str(uuid.uuid4()),
            "status": "rejected",
            "reason": "Could not calculate order quantity",
            "symbol": symbol,
            "side": side,
            "qty": 0,
            "filled_qty": 0,
            "filled_avg_price": 0,
        }

    # Set leverage if not default
    if leverage > 1:
        try:
            aster_set_leverage(aster_sym, leverage)
        except Exception as e:
            log.warning("Failed to set leverage %dx for %s: %s", leverage, aster_sym, e)

    # Round qty based on symbol (BTC needs more precision than AVAX)
    qty = _round_qty(aster_sym, qty)

    # -----------------------------------------------------------------------
    # Slippage estimation (advisory — does not block the order)
    # -----------------------------------------------------------------------
    try:
        book = get_aster_book_ticker(aster_sym)
        bid = float(book.get("bidPrice", 0))
        ask = float(book.get("askPrice", 0))
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid * 100.0
            best_qty = float(book.get("bidQty", 0)) if side_upper == "SELL" else float(book.get("askQty", 0))
            order_notional = (notional or 0) if notional else (qty * mid)

            if spread_pct > 0.5:
                log.warning(
                    "SLIPPAGE ADVISORY: %s spread=%.3f%% (bid=%.4f ask=%.4f) — wide spread",
                    aster_sym, spread_pct, bid, ask,
                )
            if best_qty > 0 and order_notional > mid * best_qty * 0.5:
                log.warning(
                    "SLIPPAGE ADVISORY: %s order notional $%.2f exceeds 50%% of best "
                    "book depth ($%.2f) — potential market impact",
                    aster_sym, order_notional, mid * best_qty,
                )
    except Exception as e:
        log.debug("Slippage check skipped for %s: %s", aster_sym, e)

    log.info("Routing to AsterDex: %s %s %.6f %s (notional=$%.2f)",
             side_upper, aster_sym, qty, order_type, notional or 0)

    try:
        result = aster_submit_order(
            symbol=aster_sym,
            side=side_upper,
            order_type=order_type,
            quantity=qty,
        )

        # Translate response to Alpaca-compatible format
        status = result.get("status", "UNKNOWN").lower()
        # Map AsterDex statuses to Alpaca equivalents
        status_map = {
            "new": "accepted",
            "partially_filled": "partially_filled",
            "filled": "filled",
            "canceled": "canceled",
            "rejected": "rejected",
            "expired": "expired",
        }
        alpaca_status = status_map.get(status, status)

        order_result = {
            "id": str(result.get("orderId", uuid.uuid4())),
            "status": alpaca_status,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "filled_qty": float(result.get("executedQty", 0)),
            "filled_avg_price": float(result.get("avgPrice", mark_price)),
            "order_type": order_type,
            "aster_order_id": result.get("orderId"),
            "aster_symbol": aster_sym,
        }

        # Submit server-side stop-loss if provided
        if stop_loss_price and stop_loss_price > 0:
            try:
                stop_side = "SELL" if side_upper == "BUY" else "BUY"
                from trading.execution.aster_client import aster_submit_order as _aster_order
                stop_order = _aster_order(
                    symbol=aster_sym,
                    side=stop_side,
                    order_type="STOP_MARKET",
                    quantity=qty,
                    stop_price=stop_loss_price,
                )
                log.info("Server-side stop-loss placed: %s %s @ $%.2f (order %s)",
                         stop_side, aster_sym, stop_loss_price,
                         stop_order.get("orderId", "unknown"))
                order_result["stop_order_id"] = stop_order.get("orderId")
            except Exception as e:
                log.warning("Failed to place server-side stop-loss for %s: %s", aster_sym, e)

        return order_result

    except Exception as e:
        log.error("AsterDex order failed: %s %s qty=%.6f: %s",
                  side_upper, aster_sym, qty, e)
        return {
            "id": str(uuid.uuid4()),
            "status": "rejected",
            "reason": str(e),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "filled_qty": 0,
            "filled_avg_price": 0,
        }


def get_order_status(order_id: str, symbol: str = None) -> dict:
    """Check order status on AsterDex.

    Args:
        order_id: AsterDex orderId (numeric, stored as string).
        symbol: AsterDex or Alpaca symbol. If not provided, attempts
                to look it up from the local DB trades table.

    Returns:
        Alpaca-compatible order status dict with id, status,
        filled_qty, filled_avg_price, etc.
    """
    from trading.execution.aster_client import aster_get_order, is_aster_configured

    if not is_aster_configured():
        return {"id": order_id, "status": "unknown", "reason": "AsterDex not configured"}

    # Resolve symbol if not provided — look up from DB trades table
    if not symbol:
        try:
            from trading.db.store import get_db
            with get_db() as conn:
                row = conn.execute(
                    "SELECT symbol FROM trades WHERE alpaca_order_id = ? LIMIT 1",
                    (str(order_id),),
                ).fetchone()
                if row:
                    symbol = row["symbol"]
        except Exception as e:
            log.warning("Could not look up symbol for order %s from DB: %s", order_id, e)

    if not symbol:
        log.warning("get_order_status: no symbol for order %s, cannot query AsterDex", order_id)
        return {"id": order_id, "status": "unknown", "reason": "symbol not available"}

    aster_sym = _to_aster(symbol)

    try:
        result = aster_get_order(aster_sym, order_id=int(order_id))

        # Translate AsterDex status to Alpaca-compatible format
        raw_status = result.get("status", "UNKNOWN").lower()
        status_map = {
            "new": "accepted",
            "partially_filled": "partially_filled",
            "filled": "filled",
            "canceled": "canceled",
            "rejected": "rejected",
            "expired": "expired",
        }
        alpaca_status = status_map.get(raw_status, raw_status)

        return {
            "id": str(order_id),
            "status": alpaca_status,
            "symbol": symbol,
            "filled_qty": float(result.get("executedQty", 0)),
            "filled_avg_price": float(result.get("avgPrice", 0)),
            "qty": float(result.get("origQty", 0)),
            "side": result.get("side", "").lower(),
            "order_type": result.get("type", ""),
            "aster_order_id": result.get("orderId"),
        }

    except Exception as e:
        log.error("Failed to get order status for %s (symbol=%s): %s", order_id, aster_sym, e)
        return {"id": order_id, "status": "unknown", "reason": str(e)}


def close_position(symbol: str) -> dict:
    """Close a position by submitting an opposite market order."""
    from trading.execution.aster_client import aster_get_positions

    aster_sym = _to_aster(symbol)

    try:
        positions = aster_get_positions()
        for pos in positions:
            if pos.get("symbol") == aster_sym:
                amt = pos.get("positionAmt", 0)
                if abs(amt) < 1e-10:
                    return {"status": "no_position", "symbol": symbol}

                # Close: sell if long, buy if short
                close_side = "SELL" if amt > 0 else "BUY"
                return submit_order(
                    symbol=symbol,
                    side=close_side,
                    qty=abs(amt),
                )

        return {"status": "no_position", "symbol": symbol}

    except Exception as e:
        log.error("Failed to close position %s: %s", symbol, e)
        return {"status": "error", "reason": str(e), "symbol": symbol}


def get_crypto_quote(symbol: str) -> dict:
    """Get current bid/ask/mid from AsterDex."""
    from trading.execution.aster_client import get_aster_book_ticker

    aster_sym = _to_aster(symbol)
    try:
        data = get_aster_book_ticker(aster_sym)
        bid = data.get("bidPrice", 0)
        ask = data.get("askPrice", 0)
        return {
            "bid": bid,
            "ask": ask,
            "mid": (bid + ask) / 2 if bid and ask else 0,
            "symbol": symbol,
        }
    except Exception as e:
        log.warning("Failed to get quote for %s: %s", symbol, e)
        return {"bid": 0, "ask": 0, "mid": 0, "symbol": symbol}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_qty(aster_symbol: str, qty: float) -> float:
    """Round quantity to appropriate precision for the symbol.

    AsterDex has different min quantity / step size per symbol.
    Uses exchange info lookup with hardcoded fallbacks for known symbols.
    """
    # High-value assets need more decimal precision (smaller qty per dollar)
    precision_map = {
        # Crypto — high price
        "BTCUSDT": 3, "ETHUSDT": 3, "BCHUSDT": 3,
        "PAXGUSDT": 3,
        # Crypto — medium price
        "AAVEUSDT": 2, "LTCUSDT": 2, "BNBUSDT": 2, "ETCUSDT": 2,
        "XMRUSDT": 3, "ZECUSDT": 3, "INJUSDT": 2, "TAOUSDT": 3,
        "MKRUSDT": 3, "RENDERUSDT": 1,
        # Crypto — lower price (larger qty per dollar)
        "SOLUSDT": 1, "AVAXUSDT": 1, "DOTUSDT": 1, "LINKUSDT": 1,
        "UNIUSDT": 1, "ADAUSDT": 0, "XRPUSDT": 0, "TRXUSDT": 0,
        "DOGEUSDT": 0, "SUIUSDT": 0, "APTUSDT": 1, "ATOMUSDT": 1,
        "NEARUSDT": 1, "TONUSDT": 1, "SEIUSDT": 0, "ARBUSDT": 0,
        "OPUSDT": 1, "CRVUSDT": 0, "JUPUSDT": 0, "FETUSDT": 0,
        "FILUSDT": 1, "ARUSDT": 1, "HBARUSDT": 0, "KASUSDT": 0,
        "STXUSDT": 1, "DYDXUSDT": 1, "STRKUSDT": 0, "ICPUSDT": 1,
        "MOVUSDT": 0, "FLOWUSDT": 0, "AXSUSDT": 1, "GALAUSDT": 0,
        "APEUSDT": 0, "WLDUSDT": 1, "HYPEUSDT": 1, "PYTHUSDT": 0,
        "JASMYUSDT": 0, "PENDLEUSDT": 1, "ONDOUSDT": 1,
        "SNXUSDT": 1, "COWUSDT": 0, "EIGENUSDT": 1, "LDOUSDT": 1,
        "CAKEUSDT": 1, "ETHFIUSDT": 1,
        # Meme — very low price, trade in thousands
        "1000SHIBUSDT": 0, "1000PEPEUSDT": 0, "1000BONKUSDT": 0,
        "1000FLOKIUSDT": 0, "PNUTUSDT": 0, "TRUMPUSDT": 1,
        "FARTCOINUSDT": 0, "MELANIAUSDT": 0, "BOMEUSDT": 0,
        "MOODENGUSDT": 0, "TURBOUSDT": 0,
        # AI tokens
        "AIOUSDT": 0, "VIRTUALUSDT": 1, "GRASSUSDT": 0,
        # Stocks — priced in USD, typically 0.01 share precision
        "AAPLUSDT": 2, "AMZNUSDT": 2, "MSFTUSDT": 2, "NVDAUSDT": 2,
        "TSLAUSDT": 2, "GOOGUSDT": 2, "METAUSDT": 2, "INTCUSDT": 1,
        "HOODUSDT": 1,
        # Commodities
        "XAUUSDT": 2, "XAGUSDT": 1, "XCUUSDT": 1, "XPTUSDT": 2,
        "XPDUSDT": 2, "NATGASUSDT": 1,
        # Indices
        "SPXUSDT": 2, "QQQUSDT": 2,
    }
    decimals = precision_map.get(aster_symbol, 2)
    return round(qty, decimals)
