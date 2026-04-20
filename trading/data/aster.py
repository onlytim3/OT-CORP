"""AsterDex derivatives market data — funding rates, order flow, basis spreads.

Wraps the low-level ``trading.execution.aster_client`` public endpoints into
higher-level functions that strategies and the intelligence engine consume.
All public endpoints require NO authentication or API keys.

Every function uses ``@cached(ttl=...)`` from the shared cache layer and
returns neutral/empty data on failure so callers never crash.
"""

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

from trading.config import ASTER_SYMBOLS, DEFAULT_COINS
from trading.data.cache import cached

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mapping helpers
# ---------------------------------------------------------------------------

# Reverse map: BTCUSDT -> bitcoin
_ASTER_TO_COIN = {v: k for k, v in ASTER_SYMBOLS.items()}

# Build Alpaca <-> AsterDex maps dynamically from config
from trading.config import CRYPTO_SYMBOLS
_ALPACA_TO_ASTER = {}
_ASTER_TO_ALPACA = {}
for coin_id, aster_sym in ASTER_SYMBOLS.items():
    alpaca_sym = CRYPTO_SYMBOLS.get(coin_id)
    if alpaca_sym:
        _ALPACA_TO_ASTER[alpaca_sym] = aster_sym
        _ASTER_TO_ALPACA[aster_sym] = alpaca_sym


def alpaca_to_aster(symbol: str) -> str | None:
    """Convert Alpaca symbol (BTC/USD) to AsterDex symbol (BTCUSDT)."""
    return _ALPACA_TO_ASTER.get(symbol)


def aster_to_alpaca(symbol: str) -> str | None:
    """Convert AsterDex symbol (BTCUSDT) to Alpaca symbol (BTC/USD)."""
    return _ASTER_TO_ALPACA.get(symbol)


def coin_to_aster(coin_id: str) -> str | None:
    """Convert CoinGecko coin ID to AsterDex symbol."""
    return ASTER_SYMBOLS.get(coin_id)


def _all_aster_symbols() -> list[str]:
    """Return all tracked AsterDex symbols."""
    return list(ASTER_SYMBOLS.values())


# ---------------------------------------------------------------------------
# Public data functions
# ---------------------------------------------------------------------------

@cached(ttl=300)
def get_funding_rates(symbols: list[str] | None = None) -> dict[str, float]:
    """Get current funding rates for tracked symbols.

    Returns mapping of coin_id -> funding rate (e.g. 0.0003 = 0.03%).
    Returns empty dict on failure.
    """
    try:
        from trading.execution.aster_client import get_aster_mark_prices

        data = get_aster_mark_prices()
        if not isinstance(data, list):
            data = [data]

        result = {}
        target_symbols = set(symbols) if symbols else set(_all_aster_symbols())

        for entry in data:
            sym = entry.get("symbol", "")
            if sym in target_symbols or not symbols:
                coin_id = _ASTER_TO_COIN.get(sym)
                if coin_id:
                    rate = entry.get("lastFundingRate", 0.0)
                    result[coin_id] = float(rate)

        return result

    except Exception as e:
        log.warning("Failed to fetch AsterDex funding rates: %s", e)
        return {}


@cached(ttl=600)
def get_funding_rate_history(symbol: str, limit: int = 100) -> list[float]:
    """Get historical funding rate series for z-score calculation.

    Args:
        symbol: AsterDex symbol (e.g. 'BTCUSDT').
        limit: Number of historical entries.

    Returns list of funding rates (oldest first). Empty list on failure.
    """
    try:
        from trading.execution.aster_client import get_aster_funding_rates

        data = get_aster_funding_rates(symbol=symbol, limit=limit)
        if not data:
            return []

        # Sort by fundingTime ascending (oldest first)
        sorted_data = sorted(data, key=lambda x: x.get("fundingTime", 0))
        return [float(entry.get("fundingRate", 0.0)) for entry in sorted_data]

    except Exception as e:
        log.warning("Failed to fetch funding history for %s: %s", symbol, e)
        return []


@cached(ttl=60)
def get_orderbook_imbalance(symbol: str, depth: int = 20) -> dict | None:
    """Get bid/ask volume imbalance from order book.

    Args:
        symbol: AsterDex symbol (e.g. 'BTCUSDT').
        depth: Order book depth (default 20 levels).

    Returns dict with bid_volume, ask_volume, imbalance (-1 to +1),
    spread_bps, mid_price. None on failure.
    """
    try:
        from trading.execution.aster_client import get_aster_orderbook

        book = get_aster_orderbook(symbol, limit=depth)
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            return None

        bid_vol = sum(qty for _, qty in bids)
        ask_vol = sum(qty for _, qty in asks)
        total = bid_vol + ask_vol

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid_price = (best_bid + best_ask) / 2.0
        spread_bps = (best_ask - best_bid) / mid_price * 10_000 if mid_price > 0 else 0

        return {
            "bid_volume": round(bid_vol, 4),
            "ask_volume": round(ask_vol, 4),
            "imbalance": round((bid_vol - ask_vol) / total, 4) if total > 0 else 0.0,
            "spread_bps": round(spread_bps, 2),
            "mid_price": round(mid_price, 2),
        }

    except Exception as e:
        log.warning("Failed to fetch orderbook for %s: %s", symbol, e)
        return None


@cached(ttl=60)
def get_basis_spread(symbol: str | None = None) -> dict | list[dict]:
    """Get mark vs index price basis spread.

    Args:
        symbol: AsterDex symbol. If None, returns all tracked symbols.

    Returns:
        Single dict if symbol provided, list of dicts otherwise.
        Each contains: symbol, markPrice, indexPrice, basis_pct, fundingRate.
    """
    try:
        from trading.execution.aster_client import get_aster_mark_prices

        if symbol:
            data = get_aster_mark_prices(symbol=symbol)
            if not isinstance(data, dict):
                return {"symbol": symbol, "markPrice": 0, "indexPrice": 0,
                        "basis_pct": 0, "fundingRate": 0}
            mark = data.get("markPrice", 0)
            index = data.get("indexPrice", 0)
            basis = ((mark - index) / index * 100) if index > 0 else 0
            return {
                "symbol": data.get("symbol", symbol),
                "markPrice": mark,
                "indexPrice": index,
                "basis_pct": round(basis, 4),
                "fundingRate": data.get("lastFundingRate", 0),
            }

        # All symbols
        data = get_aster_mark_prices()
        if not isinstance(data, list):
            return []

        tracked = set(_all_aster_symbols())
        results = []
        for entry in data:
            sym = entry.get("symbol", "")
            if sym not in tracked:
                continue
            mark = entry.get("markPrice", 0)
            index = entry.get("indexPrice", 0)
            basis = ((mark - index) / index * 100) if index > 0 else 0
            results.append({
                "symbol": sym,
                "markPrice": mark,
                "indexPrice": index,
                "basis_pct": round(basis, 4),
                "fundingRate": entry.get("lastFundingRate", 0),
            })
        return results

    except Exception as e:
        log.warning("Failed to fetch basis spread: %s", e)
        if symbol:
            return {"symbol": symbol, "markPrice": 0, "indexPrice": 0,
                    "basis_pct": 0, "fundingRate": 0}
        return []


@cached(ttl=300)
def get_taker_volume_ratio(symbol: str, interval: str = "1h",
                           limit: int = 24) -> dict | None:
    """Get taker buy/sell volume ratio from kline data.

    Computes the ratio of taker buy volume to total volume over recent candles.

    Args:
        symbol: AsterDex symbol (e.g. 'BTCUSDT').
        interval: Kline interval (default '1h').
        limit: Number of candles (default 24 = last 24 hours for 1h).

    Returns dict with buy_ratio, sell_ratio, net_ratio (-1 to +1), periods.
    None on failure.
    """
    try:
        from trading.execution.aster_client import get_aster_klines

        df = get_aster_klines(symbol, interval=interval, limit=limit)
        if df.empty:
            return None

        total_vol = df["volume"].sum()
        taker_buy_vol = df["taker_buy_base_vol"].sum()

        if total_vol <= 0:
            return None

        buy_ratio = taker_buy_vol / total_vol
        sell_ratio = 1.0 - buy_ratio

        return {
            "symbol": symbol,
            "buy_ratio": round(float(buy_ratio), 4),
            "sell_ratio": round(float(sell_ratio), 4),
            "net_ratio": round(float(buy_ratio * 2 - 1), 4),  # -1 to +1
            "periods": limit,
        }

    except Exception as e:
        log.warning("Failed to fetch taker volume for %s: %s", symbol, e)
        return None


@cached(ttl=300)
def get_aster_ohlcv(symbol: str, interval: str = "1h",
                    limit: int = 500) -> pd.DataFrame:
    """Get OHLCV with taker buy volume and trade count.

    Returns DataFrame indexed by open_time with columns:
    open, high, low, close, volume, taker_buy_base_vol, trades.
    """
    try:
        from trading.execution.aster_client import get_aster_klines
        return get_aster_klines(symbol, interval=interval, limit=limit)
    except Exception as e:
        log.warning("Failed to fetch AsterDex OHLCV for %s: %s", symbol, e)
        return pd.DataFrame()


@cached(ttl=300)
def get_aster_market_summary() -> dict:
    """Get aggregated market intelligence across all tracked symbols.

    Combines funding rates, orderbook pressure, basis regime, and volume flow
    into a single summary dict for the intelligence engine.

    Returns dict with:
        funding_sentiment: average funding rate across tracked symbols
        orderbook_pressure: average bid/ask imbalance (-1 to +1)
        basis_regime: 'contango' or 'backwardation' (average basis sign)
        volume_flow: average taker buy/sell net ratio (-1 to +1)
    """
    summary = {
        "funding_sentiment": 0.0,
        "orderbook_pressure": 0.0,
        "basis_regime": "neutral",
        "volume_flow": 0.0,
    }

    try:
        # Funding rates
        rates = get_funding_rates()
        if rates:
            avg_rate = sum(rates.values()) / len(rates)
            summary["funding_sentiment"] = round(avg_rate, 6)

        # Orderbook imbalance (sample top 3 by market cap)
        imbalances = []
        for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            ob = get_orderbook_imbalance(sym)
            if ob:
                imbalances.append(ob["imbalance"])
        if imbalances:
            summary["orderbook_pressure"] = round(
                sum(imbalances) / len(imbalances), 4
            )

        # Basis spread
        basis_data = get_basis_spread()
        if isinstance(basis_data, list) and basis_data:
            avg_basis = sum(b["basis_pct"] for b in basis_data) / len(basis_data)
            summary["basis_regime"] = "contango" if avg_basis > 0 else "backwardation"

        # Volume flow
        flows = []
        for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            tv = get_taker_volume_ratio(sym)
            if tv:
                flows.append(tv["net_ratio"])
        if flows:
            summary["volume_flow"] = round(sum(flows) / len(flows), 4)

    except Exception as e:
        log.warning("AsterDex market summary partially failed: %s", e)

    return summary


# ---------------------------------------------------------------------------
# Open Interest data
# ---------------------------------------------------------------------------

@cached(ttl=300)
def get_open_interest(symbol: str) -> float | None:
    """Get current open interest for an AsterDex symbol.

    Args:
        symbol: AsterDex symbol (e.g. 'BTCUSDT').

    Returns open interest as float, or None on failure.
    """
    try:
        from trading.execution.aster_client import get_aster_open_interest
        data = get_aster_open_interest(symbol)
        return data.get("openInterest")
    except Exception as e:
        log.warning("Failed to fetch OI for %s: %s", symbol, e)
        return None


@cached(ttl=600)
def get_open_interest_history(symbol: str, period: str = "1h",
                              limit: int = 30) -> list[dict]:
    """Get historical open interest data.

    Args:
        symbol: AsterDex symbol.
        period: Data period (5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d).
        limit: Number of records.

    Returns list of dicts with sumOpenInterest, sumOpenInterestValue, timestamp.
    Empty list on failure.
    """
    try:
        from trading.execution.aster_client import get_aster_open_interest_hist
        return get_aster_open_interest_hist(symbol, period=period, limit=limit)
    except Exception as e:
        log.warning("Failed to fetch OI history for %s: %s", symbol, e)
        return []


@cached(ttl=300)
def get_long_short_ratio(symbol: str, period: str = "1h",
                         limit: int = 30) -> list[dict]:
    """Get top trader long/short account ratio.

    Args:
        symbol: AsterDex symbol.
        period: Data period.
        limit: Number of records.

    Returns list of dicts with longShortRatio, longAccount, shortAccount, timestamp.
    Empty list on failure.
    """
    try:
        from trading.execution.aster_client import get_aster_long_short_ratio
        return get_aster_long_short_ratio(symbol, period=period, limit=limit)
    except Exception as e:
        log.warning("Failed to fetch long/short ratio for %s: %s", symbol, e)
        return []


@cached(ttl=300)
def get_taker_buy_sell_ratio(symbol: str, period: str = "1h",
                             limit: int = 30) -> list[dict]:
    """Get taker buy/sell volume ratio history.

    Returns list of dicts with buySellRatio, buyVol, sellVol, timestamp.
    Empty list on failure.
    """
    try:
        from trading.execution.aster_client import get_aster_taker_buy_sell_volume
        return get_aster_taker_buy_sell_volume(symbol, period=period, limit=limit)
    except Exception as e:
        log.warning("Failed to fetch taker volume for %s: %s", symbol, e)
        return []


# ---------------------------------------------------------------------------
# Enhanced derivatives data surfaces (roadmap Item 3)
# ---------------------------------------------------------------------------

@cached(ttl=300)
def get_funding_surface(symbols: list[str] | None = None) -> dict[str, dict]:
    """Multi-tenor funding rate surface per symbol.

    Computes rolling averages over the historical funding rate series to expose
    the term structure of funding: short-term vs long-term rate and the slope
    (positive slope = longs building leverage, bearish signal for contrarians).

    Returns:
        {symbol: {"rate_1h": float, "rate_8h": float, "rate_24h": float,
                  "slope": float, "z_score": float}}
        slope > 0 means funding is rising (overleveraged longs accumulating).
        z_score is current 8h rate relative to 30-period history.
    """
    target = symbols or _all_aster_symbols()
    result: dict[str, dict] = {}

    for sym in target:
        try:
            history = get_funding_rate_history(sym, limit=100)
            if len(history) < 4:
                continue

            # Rolling averages (funding settles every 8h; 3 per day)
            rate_1h = history[-1]                                       # most recent
            rate_8h = sum(history[-3:]) / min(3, len(history))         # ~1 day
            rate_24h = sum(history[-9:]) / min(9, len(history))        # ~3 days

            # Slope: is funding rising or falling?
            slope = rate_8h - rate_24h

            # Z-score vs 30-period history
            window = history[-30:]
            if len(window) >= 5:
                mean = sum(window) / len(window)
                variance = sum((x - mean) ** 2 for x in window) / len(window)
                std = variance ** 0.5
                z_score = (rate_8h - mean) / std if std > 1e-10 else 0.0
            else:
                z_score = 0.0

            result[sym] = {
                "rate_1h": round(rate_1h, 8),
                "rate_8h": round(rate_8h, 8),
                "rate_24h": round(rate_24h, 8),
                "slope": round(slope, 8),
                "z_score": round(z_score, 3),
            }
        except Exception as e:
            log.debug("Funding surface failed for %s: %s", sym, e)

    return result


@cached(ttl=300)
def get_oi_delta(symbol: str, periods: int = 4) -> dict:
    """Open interest delta — rate of change over recent periods.

    Detects whether smart money is entering or exiting positions.
    Rising OI + rising price = trend confirmation.
    Rising OI + falling price = shorts piling in (bearish).
    Falling OI = position unwinding (trend exhaustion signal).

    Returns:
        {"oi_current": float, "oi_delta_pct": float,
         "oi_trend": "rising"|"falling"|"flat", "periods": int}
    """
    empty = {"oi_current": 0.0, "oi_delta_pct": 0.0, "oi_trend": "flat", "periods": periods}
    try:
        history = get_open_interest_history(symbol, period="1h", limit=periods + 1)
        if len(history) < 2:
            return empty

        # sumOpenInterest is a string in the API response
        def _oi(rec: dict) -> float:
            return float(rec.get("sumOpenInterest", 0) or 0)

        newest = _oi(history[-1])
        oldest = _oi(history[0])
        if oldest <= 0:
            return empty

        delta_pct = (newest - oldest) / oldest
        trend = "rising" if delta_pct > 0.005 else ("falling" if delta_pct < -0.005 else "flat")

        return {
            "oi_current": round(newest, 2),
            "oi_delta_pct": round(delta_pct, 5),
            "oi_trend": trend,
            "periods": periods,
        }
    except Exception as e:
        log.debug("OI delta failed for %s: %s", symbol, e)
        return empty


@cached(ttl=300)
def get_liquidation_estimate(symbol: str) -> dict:
    """Estimate liquidation cluster zones using long/short ratio and current price.

    Uses the top-trader long/short account ratio as a proxy for average leverage
    positioning. Assets with high long/short ratio have a downside liquidation
    cascade risk; low ratio has upside short-squeeze potential.

    Returns:
        {"long_liq_price": float, "short_liq_price": float,
         "liq_asymmetry": float, "long_short_ratio": float}
        liq_asymmetry > 0 → more long liquidation risk (price drop → cascade).
        liq_asymmetry < 0 → more short liquidation risk (price rise → squeeze).
    """
    empty = {
        "long_liq_price": 0.0, "short_liq_price": 0.0,
        "liq_asymmetry": 0.0, "long_short_ratio": 1.0,
    }
    try:
        ls_history = get_long_short_ratio(symbol, period="1h", limit=3)
        if not ls_history:
            return empty

        latest = ls_history[-1]
        ls_ratio = float(latest.get("longShortRatio", 1.0) or 1.0)
        long_pct = float(latest.get("longAccount", 0.5) or 0.5)
        short_pct = float(latest.get("shortAccount", 0.5) or 0.5)

        # Fetch current mark price
        try:
            from trading.execution.aster_client import get_aster_mark_prices
            prices = get_aster_mark_prices()
            current_price = next(
                (float(p.get("markPrice", 0)) for p in (prices if isinstance(prices, list) else [prices])
                 if p.get("symbol") == symbol),
                0.0,
            )
        except Exception:
            current_price = 0.0

        if current_price <= 0:
            return {**empty, "long_short_ratio": ls_ratio}

        # Estimate average leverage from ratio imbalance: more longs → higher avg leverage on long side
        # Heuristic: assume 5x–20x leverage range; skew based on imbalance
        avg_long_lev = 5.0 + long_pct * 15.0   # 5x (balanced) to 20x (all-long)
        avg_short_lev = 5.0 + short_pct * 15.0

        long_liq_price = round(current_price * (1.0 - 1.0 / avg_long_lev), 4)
        short_liq_price = round(current_price * (1.0 + 1.0 / avg_short_lev), 4)

        # Asymmetry: positive = more longs at risk, negative = more shorts at risk
        liq_asymmetry = round(long_pct - short_pct, 4)

        return {
            "long_liq_price": long_liq_price,
            "short_liq_price": short_liq_price,
            "liq_asymmetry": liq_asymmetry,
            "long_short_ratio": round(ls_ratio, 4),
        }
    except Exception as e:
        log.debug("Liquidation estimate failed for %s: %s", symbol, e)
        return empty


@cached(ttl=300)
def get_enhanced_market_data() -> dict:
    """Extended market summary combining base summary + derivatives surfaces.

    Extends get_aster_market_summary() with:
    - funding_surface: multi-tenor funding rates per symbol
    - oi_deltas: OI trend per symbol
    - liquidation_estimates: long/short liq zone per symbol

    Used by the intelligence engine and microstructure strategies for richer
    signal generation without extra API calls (all data sources are cached).
    """
    try:
        base = get_aster_market_summary()
    except Exception:
        base = {}

    symbols = _all_aster_symbols()

    funding_surface = get_funding_surface(symbols)

    oi_deltas: dict[str, dict] = {}
    liq_estimates: dict[str, dict] = {}
    for sym in symbols:
        oi_deltas[sym] = get_oi_delta(sym)
        liq_estimates[sym] = get_liquidation_estimate(sym)

    # Aggregate summary signals
    rising_oi_count = sum(1 for v in oi_deltas.values() if v.get("oi_trend") == "rising")
    high_funding_count = sum(
        1 for v in funding_surface.values() if abs(v.get("z_score", 0)) > 1.5
    )
    long_heavy_count = sum(
        1 for v in liq_estimates.values() if v.get("liq_asymmetry", 0) > 0.1
    )

    return {
        **base,
        "funding_surface": funding_surface,
        "oi_deltas": oi_deltas,
        "liquidation_estimates": liq_estimates,
        "derivatives_summary": {
            "rising_oi_symbols": rising_oi_count,
            "high_funding_symbols": high_funding_count,
            "long_heavy_symbols": long_heavy_count,
            "total_symbols": len(symbols),
        },
    }
