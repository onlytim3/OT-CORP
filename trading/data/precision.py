"""Price-precision helpers.

Producers (data fetchers, strategies, risk manager) historically rounded raw
prices to 2 decimals before emitting them, which collapsed sub-dollar tokens
to ``0.00``. These helpers preserve real precision: prefer the exchange's
per-instrument ``tickSize`` when available, otherwise fall back to a
magnitude-based rule that mirrors ``dashboard/src/app/utils/format.ts``.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_TICK_DECIMALS_CACHE: dict[str, int] | None = None


def price_decimals(price: float) -> int:
    """Adaptive decimal count for a price magnitude."""
    abs_p = abs(price)
    if abs_p >= 100:
        return 2
    if abs_p >= 1:
        return 4
    if abs_p >= 0.01:
        return 6
    return 8


def _decimals_from_tick(tick_str: str) -> int | None:
    """Derive decimal places from a tick-size string (e.g. '0.0001' -> 4)."""
    if not tick_str:
        return None
    s = tick_str.strip()
    if "." not in s:
        return 0
    frac = s.split(".", 1)[1].rstrip("0")
    return len(frac)


def _load_tick_decimals() -> dict[str, int]:
    """Build {symbol: decimals} from Bybit instruments-info. Cached.

    Lazy-imports the bybit client to avoid circular imports with
    ``trading.data.bybit``.
    """
    global _TICK_DECIMALS_CACHE
    if _TICK_DECIMALS_CACHE is not None:
        return _TICK_DECIMALS_CACHE
    out: dict[str, int] = {}
    try:
        from trading.execution.bybit_client import get_bybit_exchange_info
        info = get_bybit_exchange_info()
        for sym in info.get("symbols", []):
            name = sym.get("symbol", "")
            if not name:
                continue
            for f in sym.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    d = _decimals_from_tick(str(f.get("tickSize", "")))
                    if d is not None:
                        out[name] = d
                    break
    except Exception as e:
        log.debug("tickSize lookup failed; falling back to adaptive precision: %s", e)
    _TICK_DECIMALS_CACHE = out
    return out


def refresh_tick_cache() -> None:
    """Force the next ``round_price`` call to re-read exchange info."""
    global _TICK_DECIMALS_CACHE
    _TICK_DECIMALS_CACHE = None


def round_price(price: float, symbol: str | None = None) -> float:
    """Round ``price`` to the correct precision for ``symbol``.

    Uses Bybit's ``tickSize`` when the symbol is known, otherwise falls back to
    ``price_decimals(price)``.
    """
    if price is None:
        return price  # type: ignore[return-value]
    decimals: int | None = None
    if symbol:
        decimals = _load_tick_decimals().get(symbol)
    if decimals is None:
        decimals = price_decimals(price)
    return round(float(price), decimals)
