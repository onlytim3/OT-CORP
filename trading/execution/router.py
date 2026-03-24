"""Execution router — unified interface for order routing.

Routes crypto orders to AsterDex perpetual futures.
Provides the same interface as alpaca_client so the rest of the system
(main.py, scheduler.py, risk manager) doesn't need to change.

Symbol translation: strategies emit Alpaca-style symbols (BTC/USD),
the router converts to AsterDex format (BTCUSDT) before execution.
"""

import logging
import os
import time as _time
import uuid
from datetime import datetime, timezone
from typing import Optional

from trading.config import ASTER_SYMBOLS, CRYPTO_SYMBOLS
from trading.data.aster import alpaca_to_aster, aster_to_alpaca

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Execution quality configuration
# ---------------------------------------------------------------------------
USE_LIMIT_ORDERS = os.getenv("USE_LIMIT_ORDERS", "true").lower() == "true"

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
        # Alpaca format (BTC/USD) or dashboard format (BTCUSD)
        aster = _ALPACA_TO_ASTER.get(symbol) or alpaca_to_aster(symbol)
        if not aster:
            # Strip USD suffix (handles both "BTC/USD" and "BTCUSD")
            base = symbol.replace("/USD", "").replace("/", "")
            if base.endswith("USD"):
                base = base[:-3]  # BTCUSD → BTC
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
    """Get account info — paper mode uses local simulation, live uses AsterDex."""
    import os
    from trading.config import TRADING_MODE
    from trading.db.store import get_setting
    from trading.execution.aster_client import aster_get_account, is_aster_configured

    # Paper mode: use fully simulated account
    mode = get_setting("trading_mode", TRADING_MODE)
    if mode == "paper":
        return _paper_get_account()

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

        return {
            "portfolio_value": equity,
            "cash": available,
            "buying_power": available,
            "equity": equity,
            "paper": False,
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
    """Get positions — paper mode reads from DB, live mode from AsterDex."""
    from trading.config import TRADING_MODE
    from trading.db.store import get_setting
    mode = get_setting("trading_mode", TRADING_MODE)
    if mode == "paper":
        return _paper_get_positions()

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

            # Direction-aware unrealized P&L %
            if entry_price > 0:
                raw_pct = (mark_price - entry_price) / entry_price * 100
                unrealized_pnl_pct = -raw_pct if side == "short" else raw_pct
            else:
                unrealized_pnl_pct = 0

            result.append({
                "symbol": alpaca_sym.replace("/", ""),  # Match Alpaca format: BTCUSD
                "qty": qty,
                "avg_cost": entry_price,
                "current_price": mark_price,
                "market_value": qty * mark_price,
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": unrealized_pnl_pct,
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
# Limit order support
# ---------------------------------------------------------------------------

def place_limit_order(
    symbol: str,
    side: str,
    qty: float,
    price: float,
    tif: str = "GTC",
    leverage: int = 1,
    stop_loss_price: Optional[float] = None,
) -> dict:
    """Place a limit order on AsterDex.

    Args:
        symbol: Alpaca-style or AsterDex symbol.
        side: 'buy' or 'sell'.
        qty: Order quantity.
        price: Limit price.
        tif: Time in force (GTC, IOC, FOK). Default GTC.
        leverage: Leverage multiplier.
        stop_loss_price: Optional stop-loss price.

    Returns:
        Alpaca-compatible order result dict.
    """
    return submit_order(
        symbol=symbol,
        side=side,
        qty=qty,
        order_type="LIMIT",
        limit_price=price,
        time_in_force=tif,
        leverage=leverage,
        stop_loss_price=stop_loss_price,
    )


def place_limit_with_fallback(
    symbol: str,
    side: str,
    qty: float,
    limit_price: Optional[float] = None,
    timeout: int = 120,
    leverage: int = 1,
    stop_loss_price: Optional[float] = None,
) -> dict:
    """Place a limit order, poll for fill, fall back to market if unfilled.

    If no limit_price is given, computes one from the current book:
      - Buys: mid + 1bps
      - Sells: mid - 1bps

    Args:
        symbol: Trading symbol.
        side: 'buy' or 'sell'.
        qty: Order quantity.
        limit_price: Explicit limit price (optional, auto-computed if None).
        timeout: Seconds to wait for fill before market fallback.
        leverage: Leverage multiplier.
        stop_loss_price: Optional stop-loss price.

    Returns:
        Alpaca-compatible order result dict (from limit or market fallback).
    """
    from trading.execution.aster_client import (
        get_aster_book_ticker,
        aster_cancel_order,
    )

    aster_sym = _to_aster(symbol)

    # Auto-compute limit price from book if not provided
    if limit_price is None:
        try:
            book = get_aster_book_ticker(aster_sym)
            bid = float(book.get("bidPrice", 0))
            ask = float(book.get("askPrice", 0))
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                # 1 basis point offset for aggressive fill
                if side.lower() == "buy":
                    limit_price = mid * 1.0001  # mid + 1bps
                else:
                    limit_price = mid * 0.9999  # mid - 1bps
            else:
                log.warning("Cannot compute limit price for %s, using market order", symbol)
                return submit_order(
                    symbol=symbol, side=side, qty=qty,
                    leverage=leverage, stop_loss_price=stop_loss_price,
                )
        except Exception as e:
            log.warning("Book ticker failed for %s, using market order: %s", symbol, e)
            return submit_order(
                symbol=symbol, side=side, qty=qty,
                leverage=leverage, stop_loss_price=stop_loss_price,
            )

    # Place the limit order (without SL -- SL goes on fallback/final fill)
    log.info("Limit order: %s %s %.6f @ $%.4f (timeout=%ds)",
             side.upper(), symbol, qty, limit_price, timeout)
    order = submit_order(
        symbol=symbol,
        side=side,
        qty=qty,
        order_type="LIMIT",
        limit_price=limit_price,
        time_in_force="GTC",
        leverage=leverage,
    )

    if order.get("status") in ("rejected", "error"):
        return order

    # Poll for fill
    order_id = order.get("aster_order_id") or order.get("id")
    poll_interval = 5
    elapsed = 0
    while elapsed < timeout:
        _time.sleep(poll_interval)
        elapsed += poll_interval
        try:
            status = get_order_status(str(order_id), symbol=symbol)
            if status.get("status") == "filled":
                log.info("Limit order filled: %s %s @ $%.4f",
                         side.upper(), symbol,
                         status.get("filled_avg_price", 0))
                # Place SL now that we're filled
                if stop_loss_price and stop_loss_price > 0:
                    _place_stop_loss(aster_sym, side, qty, stop_loss_price)
                return status
            if status.get("status") in ("canceled", "rejected", "expired"):
                log.warning("Limit order %s for %s -- falling back to market",
                            status["status"], symbol)
                break
        except Exception as e:
            log.debug("Poll error for order %s: %s", order_id, e)

    # Timeout or cancelled -- cancel remaining and fall back to market
    try:
        aster_cancel_order(aster_sym, order_id=int(order_id))
        log.info("Cancelled unfilled limit order %s for %s", order_id, symbol)
    except Exception as e:
        log.debug("Cancel attempt for %s: %s", order_id, e)

    # Check how much was partially filled
    try:
        final_status = get_order_status(str(order_id), symbol=symbol)
        filled_so_far = float(final_status.get("filled_qty", 0))
    except Exception:
        filled_so_far = 0

    remaining_qty = qty - filled_so_far
    if remaining_qty > 0:
        log.info("Market fallback for %s: %.6f remaining of %.6f",
                 symbol, remaining_qty, qty)
        fallback = submit_order(
            symbol=symbol, side=side, qty=remaining_qty,
            leverage=leverage, stop_loss_price=stop_loss_price,
        )
        # Merge results
        total_filled = filled_so_far + float(fallback.get("filled_qty", 0))
        if total_filled > 0:
            avg_price = (
                (filled_so_far * float(final_status.get("filled_avg_price", 0)))
                + (float(fallback.get("filled_qty", 0)) * float(fallback.get("filled_avg_price", 0)))
            ) / total_filled
        else:
            avg_price = float(fallback.get("filled_avg_price", 0))
        fallback["filled_qty"] = total_filled
        fallback["filled_avg_price"] = avg_price
        fallback["qty"] = qty
        return fallback

    # Fully filled during cancel/poll window
    if stop_loss_price and stop_loss_price > 0:
        _place_stop_loss(aster_sym, side, qty, stop_loss_price)
    return final_status


def _place_stop_loss(aster_sym: str, side: str, qty: float, stop_loss_price: float):
    """Place a server-side stop-loss order."""
    try:
        from trading.execution.aster_client import aster_submit_order as _aster_order
        stop_side = "SELL" if side.lower() == "buy" else "BUY"
        _aster_order(
            symbol=aster_sym,
            side=stop_side,
            order_type="STOP_MARKET",
            quantity=qty,
            stop_price=stop_loss_price,
        )
        log.info("Stop-loss placed: %s %s @ $%.2f", stop_side, aster_sym, stop_loss_price)
    except Exception as e:
        log.warning("Failed to place stop-loss for %s: %s", aster_sym, e)


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
    limit_price: Optional[float] = None,
    time_in_force: str = "GTC",
) -> dict:
    """Submit an order -- paper mode simulates locally, live mode hits AsterDex.

    When USE_LIMIT_ORDERS is enabled and order_type is MARKET, the order
    is automatically upgraded to a limit-with-fallback. Large orders
    (>5% of 1h volume) are routed through TWAP.

    Args:
        symbol: Alpaca-style symbol (BTC/USD) or AsterDex (BTCUSDT).
        side: 'buy' or 'sell'.
        notional: Dollar amount to trade (converted to qty via mark price).
        qty: Exact quantity (overrides notional if both provided).
        order_type: MARKET or LIMIT (default MARKET).
        leverage: Leverage multiplier (default 1x).
        stop_loss_price: Optional stop-loss trigger price.
        limit_price: Limit price (required when order_type is LIMIT).
        time_in_force: GTC, IOC, FOK (default GTC, used for LIMIT orders).

    Returns dict matching Alpaca order response format:
        id, status, symbol, side, qty, filled_qty, filled_avg_price, etc.
    """
    # Check if paper mode -- simulate locally
    from trading.config import TRADING_MODE
    from trading.db.store import get_setting
    mode = get_setting("trading_mode", TRADING_MODE)
    if mode != "live":
        log.debug("Paper mode order: %s %s notional=%s leverage=%dx", side, symbol, notional, leverage)
        return _paper_submit_order(symbol, side, notional=notional, qty=qty,
                                   stop_loss_price=stop_loss_price, leverage=leverage)

    # -------------------------------------------------------------------
    # Smart execution: TWAP for large orders, limit orders when enabled
    # Only applies to MARKET orders (LIMIT orders pass through directly)
    # -------------------------------------------------------------------
    if order_type == "MARKET" and USE_LIMIT_ORDERS:
        # Check if order is large enough for TWAP
        effective_notional = notional or 0
        if effective_notional > 0:
            try:
                from trading.execution.twap import should_use_twap, execute_twap
                if should_use_twap(effective_notional, symbol):
                    # Need qty for TWAP -- compute from notional
                    from trading.execution.aster_client import get_aster_mark_prices as _gmp
                    _aster = _to_aster(symbol)
                    _md = _gmp(_aster)
                    _mp = _md.get("markPrice", 0)
                    if _mp > 0:
                        twap_qty = qty if qty else (effective_notional / _mp)
                        twap_qty = _round_qty(_aster, twap_qty)
                        if twap_qty > 0:
                            log.info("Routing %s %s to TWAP (notional=$%.2f)",
                                     side, symbol, effective_notional)
                            return execute_twap(
                                symbol=symbol,
                                side=side,
                                total_qty=twap_qty,
                                stop_loss_price=stop_loss_price,
                                leverage=leverage,
                            )
            except Exception as e:
                log.debug("TWAP check/execution failed, continuing normally: %s", e)

        # Not large enough for TWAP -- use limit-with-fallback
        # Need to resolve qty from notional if not provided
        resolved_qty = qty
        if resolved_qty is None and notional and notional > 0:
            try:
                from trading.execution.aster_client import get_aster_mark_prices as _gmp2
                _aster2 = _to_aster(symbol)
                _md2 = _gmp2(_aster2)
                _mp2 = _md2.get("markPrice", 0)
                if _mp2 > 0:
                    resolved_qty = _round_qty(_aster2, notional / _mp2)
            except Exception:
                pass

        if resolved_qty and resolved_qty > 0:
            log.debug("Upgrading MARKET to limit-with-fallback for %s", symbol)
            return place_limit_with_fallback(
                symbol=symbol,
                side=side,
                qty=resolved_qty,
                leverage=leverage,
                stop_loss_price=stop_loss_price,
            )
        # Fall through to standard MARKET if qty resolution failed

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

    # Round qty to exchange stepSize and enforce minQty
    qty = _round_qty(aster_sym, qty)

    if qty <= 0:
        log.warning("Qty rounded to 0 for %s (notional=$%.2f, price=%.4f)", aster_sym, notional or 0, mark_price)
        return {
            "id": str(uuid.uuid4()),
            "status": "rejected",
            "reason": f"Order too small for {aster_sym} after rounding to exchange step size",
            "symbol": symbol,
            "side": side,
            "qty": 0,
            "filled_qty": 0,
            "filled_avg_price": 0,
        }

    # Check min notional ($5 on most AsterDex pairs)
    _load_symbol_filters()
    sym_filters = _SYMBOL_FILTERS.get(aster_sym, {})
    min_notional = sym_filters.get("minNotional", 5)
    order_notional_est = qty * mark_price if mark_price > 0 else (notional or 0)
    if order_notional_est < min_notional:
        log.warning("Order below min notional: %s $%.2f < $%.2f", aster_sym, order_notional_est, min_notional)
        return {
            "id": str(uuid.uuid4()),
            "status": "rejected",
            "reason": f"Order value ${order_notional_est:.2f} below minimum ${min_notional:.0f} for {aster_sym}",
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "filled_qty": 0,
            "filled_avg_price": 0,
        }

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
        submit_kwargs = {
            "symbol": aster_sym,
            "side": side_upper,
            "order_type": order_type,
            "quantity": qty,
        }
        if order_type == "LIMIT" and limit_price is not None:
            submit_kwargs["price"] = limit_price
            submit_kwargs["time_in_force"] = time_in_force
        result = aster_submit_order(**submit_kwargs)

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
                log.error("CRITICAL: Failed to place server-side stop-loss for %s @ $%.2f: %s",
                          aster_sym, stop_loss_price, e)
                order_result["stop_loss_failed"] = True
                order_result["stop_loss_error"] = str(e)

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
    """Close a position — paper mode uses DB, live mode uses AsterDex."""
    from trading.config import TRADING_MODE
    from trading.db.store import get_setting
    mode = get_setting("trading_mode", TRADING_MODE)
    if mode == "paper":
        return _paper_close_position(symbol)

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

_SYMBOL_FILTERS: dict[str, dict] = {}


def _load_symbol_filters():
    """Load exchange info filters for all symbols (cached)."""
    global _SYMBOL_FILTERS
    if _SYMBOL_FILTERS:
        return
    try:
        from trading.execution.aster_client import get_aster_exchange_info
        info = get_aster_exchange_info()
        for s in info.get("symbols", []):
            filters = {}
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    filters["stepSize"] = float(f["stepSize"])
                    filters["minQty"] = float(f["minQty"])
                    filters["maxQty"] = float(f["maxQty"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    filters["minNotional"] = float(f["notional"])
            _SYMBOL_FILTERS[s["symbol"]] = filters
        log.info("Loaded filters for %d symbols", len(_SYMBOL_FILTERS))
    except Exception as e:
        log.warning("Could not load symbol filters: %s", e)


def _round_qty(aster_symbol: str, qty: float) -> float:
    """Round quantity to the exchange's stepSize and enforce minQty.

    Uses live exchange info to determine precision, ensuring orders
    comply with AsterDex's LOT_SIZE filter.
    """
    _load_symbol_filters()
    filters = _SYMBOL_FILTERS.get(aster_symbol)

    if filters and "stepSize" in filters:
        step = filters["stepSize"]
        min_qty = filters.get("minQty", 0)

        # Round down to nearest stepSize
        if step > 0:
            import math
            qty = math.floor(qty / step) * step
            # Determine decimal places from stepSize
            if step >= 1:
                qty = float(int(qty))
            else:
                decimals = max(0, -int(math.log10(step)))
                qty = round(qty, decimals)

        # Enforce minimum
        if qty < min_qty:
            qty = min_qty

        return qty

    # Fallback: round to 2 decimals
    return round(qty, 2)


# ---------------------------------------------------------------------------
# Paper Trading Simulation
# ---------------------------------------------------------------------------
# In paper mode, orders are simulated locally using real market prices.
# Positions are tracked in the `paper_positions` table in SQLite.
# No orders are sent to AsterDex. When switching to live mode, paper
# positions are ignored and real exchange positions take over.
# ---------------------------------------------------------------------------

def _init_paper_tables():
    """Create paper trading tables if they don't exist."""
    from trading.db.store import get_db
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_positions (
                symbol TEXT PRIMARY KEY,
                side TEXT NOT NULL DEFAULT 'long',
                qty REAL NOT NULL DEFAULT 0,
                avg_cost REAL NOT NULL DEFAULT 0,
                strategy TEXT,
                leverage INTEGER NOT NULL DEFAULT 1,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_balance (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        # Migration: add leverage column to existing tables
        try:
            conn.execute("ALTER TABLE paper_positions ADD COLUMN leverage INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass  # Column already exists


def _get_paper_cash() -> float:
    """Get current paper cash balance."""
    from trading.db.store import get_db
    import os
    _init_paper_tables()
    with get_db() as conn:
        row = conn.execute("SELECT cash FROM paper_balance WHERE id = 1").fetchone()
        if row:
            return row["cash"]
        # Initialize with PAPER_BALANCE
        initial = float(os.getenv("PAPER_BALANCE", "1000"))
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO paper_balance (id, cash, updated_at) VALUES (1, ?, ?)",
            (initial, now),
        )
        return initial


def _set_paper_cash(cash: float):
    """Update paper cash balance."""
    from trading.db.store import get_db
    _init_paper_tables()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO paper_balance (id, cash, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET cash = excluded.cash, updated_at = excluded.updated_at",
            (cash, now),
        )


def _paper_submit_order(
    symbol: str,
    side: str,
    notional: Optional[float] = None,
    qty: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    leverage: int = 1,
) -> dict:
    """Simulate an order using real market prices, no exchange interaction.

    Fills instantly at the current mark price. Updates paper_positions and
    paper_balance tables in the DB.

    With leverage, the cash deducted = notional / leverage (margin requirement).
    The position tracks the full notional exposure.
    """
    from trading.execution.aster_client import get_aster_mark_prices
    from trading.db.store import get_db, log_action

    _init_paper_tables()
    aster_sym = _to_aster(symbol)
    side_lower = side.lower()

    # Get real market price
    try:
        mark_data = get_aster_mark_prices(aster_sym)
        fill_price = mark_data.get("markPrice", 0)
    except Exception as e:
        log.warning("Paper: could not get mark price for %s: %s", aster_sym, e)
        return {
            "id": str(uuid.uuid4()),
            "status": "rejected",
            "reason": f"Could not get market price for {symbol}",
            "symbol": symbol, "side": side,
            "qty": 0, "filled_qty": 0, "filled_avg_price": 0,
        }

    if fill_price <= 0:
        return {
            "id": str(uuid.uuid4()),
            "status": "rejected",
            "reason": f"Invalid mark price for {symbol}",
            "symbol": symbol, "side": side,
            "qty": 0, "filled_qty": 0, "filled_avg_price": 0,
        }

    # Calculate qty from notional
    if qty is None and notional is not None and fill_price > 0:
        qty = notional / fill_price

    if qty is None or qty <= 0:
        return {
            "id": str(uuid.uuid4()),
            "status": "rejected",
            "reason": "Could not calculate order quantity",
            "symbol": symbol, "side": side,
            "qty": 0, "filled_qty": 0, "filled_avg_price": 0,
        }

    # Round to exchange step size (even paper should be realistic)
    qty = _round_qty(aster_sym, qty)
    if qty <= 0:
        return {
            "id": str(uuid.uuid4()),
            "status": "rejected",
            "reason": f"Order too small for {aster_sym}",
            "symbol": symbol, "side": side,
            "qty": 0, "filled_qty": 0, "filled_avg_price": 0,
        }

    order_value = qty * fill_price
    margin_required = order_value / max(leverage, 1)  # With leverage, only margin is deducted
    cash = _get_paper_cash()
    order_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    sym_key = symbol.replace("/", "")  # Normalize: BTC/USD → BTCUSD

    with get_db() as conn:
        # Get existing position
        pos_row = conn.execute(
            "SELECT * FROM paper_positions WHERE symbol = ?", (sym_key,)
        ).fetchone()
        existing_qty = pos_row["qty"] if pos_row else 0
        existing_cost = pos_row["avg_cost"] if pos_row else 0
        existing_side = pos_row["side"] if pos_row else "long"

        if side_lower == "buy":
            # Check cash (only margin required, not full notional)
            if margin_required > cash:
                return {
                    "id": order_id, "status": "rejected",
                    "reason": f"Insufficient paper margin: ${cash:.2f} < ${margin_required:.2f} ({leverage}x leverage)",
                    "symbol": symbol, "side": side,
                    "qty": qty, "filled_qty": 0, "filled_avg_price": 0,
                }

            # Deduct margin from cash
            _set_paper_cash(cash - margin_required)

            if pos_row and existing_side == "long" and existing_qty > 0:
                # Add to existing long — weighted avg cost
                new_qty = existing_qty + qty
                new_avg = ((existing_qty * existing_cost) + (qty * fill_price)) / new_qty
                conn.execute(
                    "UPDATE paper_positions SET qty = ?, avg_cost = ?, updated_at = ? WHERE symbol = ?",
                    (new_qty, new_avg, now, sym_key),
                )
            elif pos_row and existing_side == "short" and existing_qty > 0:
                # Buying against a short — reduce short position
                if qty >= existing_qty:
                    # Close short entirely (and maybe go long)
                    pnl = existing_qty * (existing_cost - fill_price)
                    _set_paper_cash(_get_paper_cash() + pnl + existing_qty * fill_price)
                    remaining = qty - existing_qty
                    if remaining > 0:
                        conn.execute(
                            "UPDATE paper_positions SET side = 'long', qty = ?, avg_cost = ?, updated_at = ? WHERE symbol = ?",
                            (remaining, fill_price, now, sym_key),
                        )
                    else:
                        conn.execute("DELETE FROM paper_positions WHERE symbol = ?", (sym_key,))
                else:
                    conn.execute(
                        "UPDATE paper_positions SET qty = ?, updated_at = ? WHERE symbol = ?",
                        (existing_qty - qty, now, sym_key),
                    )
                    pnl = qty * (existing_cost - fill_price)
                    _set_paper_cash(_get_paper_cash() + pnl + qty * fill_price)
            else:
                # New long position
                conn.execute(
                    "INSERT INTO paper_positions (symbol, side, qty, avg_cost, strategy, leverage, opened_at, updated_at) "
                    "VALUES (?, 'long', ?, ?, ?, ?, ?, ?)",
                    (sym_key, qty, fill_price, "", leverage, now, now),
                )

        elif side_lower == "sell":
            if pos_row and existing_side == "long" and existing_qty > 0:
                # Selling a long position — realize P&L
                sell_qty = min(qty, existing_qty)
                pnl = sell_qty * (fill_price - existing_cost)
                _set_paper_cash(_get_paper_cash() + sell_qty * fill_price)

                remaining = existing_qty - sell_qty
                if remaining > 1e-10:
                    conn.execute(
                        "UPDATE paper_positions SET qty = ?, updated_at = ? WHERE symbol = ?",
                        (remaining, now, sym_key),
                    )
                else:
                    conn.execute("DELETE FROM paper_positions WHERE symbol = ?", (sym_key,))
            else:
                # Opening a short or adding to short
                margin_needed = order_value  # 1x margin for shorts
                if margin_needed > cash:
                    return {
                        "id": order_id, "status": "rejected",
                        "reason": f"Insufficient paper cash for short: ${cash:.2f} < ${margin_needed:.2f}",
                        "symbol": symbol, "side": side,
                        "qty": qty, "filled_qty": 0, "filled_avg_price": 0,
                    }
                _set_paper_cash(cash - margin_needed)

                if pos_row and existing_side == "short":
                    new_qty = existing_qty + qty
                    new_avg = ((existing_qty * existing_cost) + (qty * fill_price)) / new_qty
                    conn.execute(
                        "UPDATE paper_positions SET qty = ?, avg_cost = ?, updated_at = ? WHERE symbol = ?",
                        (new_qty, new_avg, now, sym_key),
                    )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO paper_positions (symbol, side, qty, avg_cost, strategy, leverage, opened_at, updated_at) "
                        "VALUES (?, 'short', ?, ?, ?, ?, ?, ?)",
                        (sym_key, qty, fill_price, "", leverage, now, now),
                    )

    log.info("PAPER FILL: %s %s %.6f %s @ $%.4f ($%.2f)",
             side_lower.upper(), symbol, qty, aster_sym, fill_price, order_value)

    return {
        "id": order_id,
        "status": "filled",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "filled_qty": qty,
        "filled_avg_price": fill_price,
        "order_type": "MARKET",
        "paper": True,
    }


def _paper_get_positions() -> list[dict]:
    """Get paper positions from DB, with live mark prices for P&L."""
    from trading.db.store import get_db
    from trading.execution.aster_client import get_aster_mark_prices

    _init_paper_tables()

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM paper_positions WHERE qty > 0").fetchall()

    if not rows:
        return []

    # Batch-fetch ALL mark prices in one call to avoid per-symbol failures
    mark_prices: dict[str, float] = {}
    try:
        all_marks = get_aster_mark_prices()  # No symbol = fetch all
        if isinstance(all_marks, list):
            for entry in all_marks:
                sym = entry.get("symbol", "")
                mp = entry.get("markPrice")
                if sym and mp:
                    mark_prices[sym] = float(mp) if not isinstance(mp, float) else mp
            log.info("Fetched %d mark prices from AsterDex", len(mark_prices))
        else:
            log.warning("Unexpected mark prices response type: %s", type(all_marks))
    except Exception as e:
        log.error("Failed to fetch mark prices from AsterDex: %s", e)

    result = []
    for row in rows:
        row = dict(row)
        sym_key = row["symbol"]
        qty = row["qty"]
        avg_cost = row["avg_cost"]
        side = row.get("side", "long")

        # Get current mark price from batch data
        aster_sym = _to_aster(sym_key)
        mark_price = mark_prices.get(aster_sym)
        if mark_price is None or mark_price <= 0:
            log.warning("No mark price for %s (aster: %s), falling back to avg_cost %.4f",
                        sym_key, aster_sym, avg_cost)
            mark_price = avg_cost

        if side == "long":
            unrealized = qty * (mark_price - avg_cost)
        else:
            unrealized = qty * (avg_cost - mark_price)

        unrealized_pct = (mark_price - avg_cost) / avg_cost if avg_cost > 0 else 0
        if side == "short":
            unrealized_pct = -unrealized_pct

        result.append({
            "symbol": sym_key,
            "qty": qty,
            "avg_cost": avg_cost,
            "current_price": mark_price,
            "market_value": qty * mark_price,
            "unrealized_pnl": unrealized,
            "unrealized_pnl_pct": unrealized_pct * 100,
            "side": side,
            "strategy": row.get("strategy", ""),
            "aster_symbol": aster_sym,
            "leverage": row.get("leverage", 1) or 1,
            "liquidation_price": 0,
            "paper": True,
        })

    return result


def _paper_close_position(symbol: str) -> dict:
    """Close a paper position by simulating a market order."""
    from trading.db.store import get_db

    _init_paper_tables()
    sym_key = symbol.replace("/", "")

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM paper_positions WHERE symbol = ?", (sym_key,)
        ).fetchone()

    if not row or row["qty"] <= 0:
        return {"status": "no_position", "symbol": symbol}

    close_side = "sell" if row["side"] == "long" else "buy"
    return _paper_submit_order(symbol, close_side, qty=row["qty"])


def _paper_get_account() -> dict:
    """Get paper account summary with real-time position values."""
    import os
    _init_paper_tables()

    cash = _get_paper_cash()
    positions = _paper_get_positions()

    positions_value = sum(p["market_value"] for p in positions)
    unrealized_pnl = sum(p["unrealized_pnl"] for p in positions)
    equity = cash + positions_value

    return {
        "portfolio_value": equity,
        "cash": cash,
        "buying_power": cash,
        "equity": equity,
        "paper": True,
        "status": "ACTIVE",
        "trading_blocked": False,
        "total_wallet_balance": cash,
        "unrealized_pnl": unrealized_pnl,
        "margin_balance": equity,
    }
