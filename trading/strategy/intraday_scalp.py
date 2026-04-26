"""Intraday Scalping Strategy — VWAP/RSI + orderbook pressure + funding context.

Generates short-horizon signals on 5-minute candles for BTC/ETH/SOL perpetuals.
Designed to run every 5 minutes via a dedicated scalping cycle, independently of
the 4-hour swing cycle.

Signal logic (three sub-signals combined):
  1. VWAP RSI (weight 0.5): RSI extremes relative to VWAP anchor
     - Oversold below VWAP → BUY (mean reversion)
     - Overbought above VWAP → SELL (mean reversion)
     - EMA9 > EMA21 AND price above VWAP → BUY (momentum continuation)
  2. Orderbook imbalance (weight 0.3): bid vs ask pressure from live book
  3. Funding rate (weight 0.2): extreme funding predicts unwind direction

Each signal carries leverage=10, stop_pct=0.5%, take_profit_pct=1.5% for the
scalp cycle executor to apply.
"""

import logging

import numpy as np

from trading.config import ASTER_SYMBOLS, CRYPTO_SYMBOLS
from trading.strategy.base import Signal, Strategy
from trading.strategy.indicators import ema, rsi
from trading.strategy.registry import register

log = logging.getLogger(__name__)

SCALP_COINS = ["bitcoin", "ethereum", "solana"]

LEVERAGE = 10
STOP_PCT = 0.005        # 0.5% stop → 5% margin loss at 10x
TAKE_PROFIT_PCT = 0.015 # 1.5% target → 15% margin gain at 10x

# Sub-signal thresholds
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
OB_BUY_THRESHOLD = 0.15    # imbalance > 0.15 = net bid pressure (scale: -1 to +1)
OB_SELL_THRESHOLD = -0.15
FUNDING_BUY_THRESHOLD = -0.0003   # funding < -0.03% → shorts paying, buy bias
FUNDING_SELL_THRESHOLD = 0.0003   # funding > +0.03% → longs paying, sell bias

# Minimum combined score to emit a directional signal
MIN_SIGNAL_SCORE = 0.28


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_5m_candles(aster_sym: str, limit: int = 78):
    """Fetch 5-minute OHLCV from AsterDex (~6.5 hours of data)."""
    try:
        from trading.data.aster import get_aster_ohlcv
        df = get_aster_ohlcv(aster_sym, interval="5m", limit=limit)
        if df is not None and not df.empty and len(df) >= 20:
            return df
        return None
    except Exception as e:
        log.debug("5m candle fetch failed for %s: %s", aster_sym, e)
        return None


def _calc_vwap(df) -> float:
    """Rolling VWAP over the candle window."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol_sum = float(df["volume"].sum())
    if vol_sum <= 0:
        return float(df["close"].iloc[-1])
    return float((typical * df["volume"]).sum() / vol_sum)


def _vwap_rsi_score(df) -> tuple[float, dict]:
    """Return (score, debug_data). Score: -1 to +1, positive = buy bias."""
    closes = df["close"]
    rsi_val = float(rsi(closes, period=14).iloc[-1])
    ema9_val = float(ema(closes, 9).iloc[-1])
    ema21_val = float(ema(closes, 21).iloc[-1])
    price = float(closes.iloc[-1])
    vwap = _calc_vwap(df)

    above_vwap = price > vwap
    uptrend = ema9_val > ema21_val

    if rsi_val < RSI_OVERSOLD and not above_vwap:
        # Oversold below VWAP — strong mean-reversion buy
        score = min((RSI_OVERSOLD - rsi_val) / RSI_OVERSOLD, 1.0)
    elif rsi_val > RSI_OVERBOUGHT and above_vwap:
        # Overbought above VWAP — strong mean-reversion sell
        score = -min((rsi_val - RSI_OVERBOUGHT) / (100 - RSI_OVERBOUGHT), 1.0)
    elif 48 <= rsi_val <= 65 and above_vwap and uptrend:
        # Momentum continuation up — moderate buy
        score = (rsi_val - 48) / 34 * 0.55
    elif 35 <= rsi_val <= 52 and not above_vwap and not uptrend:
        # Momentum continuation down — moderate sell
        score = -(52 - rsi_val) / 34 * 0.55
    else:
        score = 0.0

    debug = {
        "rsi": round(rsi_val, 1),
        "vwap": round(vwap, 2),
        "price": round(price, 2),
        "ema9": round(ema9_val, 2),
        "ema21": round(ema21_val, 2),
        "above_vwap": above_vwap,
        "uptrend": uptrend,
    }
    return round(score, 4), debug


def _ob_score(aster_sym: str) -> float:
    """Return orderbook imbalance score (-1 to +1). 0.0 on failure."""
    try:
        from trading.data.aster import get_orderbook_imbalance
        ob = get_orderbook_imbalance(aster_sym, depth=20)
        if ob is None:
            return 0.0
        # imbalance field is already -1 to +1 (positive = more bids)
        return float(ob["imbalance"])
    except Exception as e:
        log.debug("OB imbalance fetch failed for %s: %s", aster_sym, e)
        return 0.0


def _funding_score(aster_sym: str) -> tuple[float, float | None]:
    """Return (score, avg_funding). Score: -1 to +1, positive = buy bias."""
    try:
        from trading.data.aster import get_funding_rate_history
        hist = get_funding_rate_history(aster_sym, limit=5)
        if not hist:
            return 0.0, None
        recent = hist[-3:] if len(hist) >= 3 else hist
        avg = sum(recent) / len(recent)
        if avg > FUNDING_SELL_THRESHOLD:
            # Longs paying — expect unwind → SELL bias
            score = -min(avg / 0.001, 1.0)
        elif avg < FUNDING_BUY_THRESHOLD:
            # Shorts paying — expect squeeze → BUY bias
            score = min(-avg / 0.001, 1.0)
        else:
            score = 0.0
        return round(score, 4), round(avg, 7)
    except Exception as e:
        log.debug("Funding score failed for %s: %s", aster_sym, e)
        return 0.0, None


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

@register
class IntradayScalpStrategy(Strategy):
    """5-minute scalping using VWAP/RSI + orderbook imbalance + funding rate."""

    name = "intraday_scalp"

    def generate_signals(self) -> list[Signal]:
        signals = []
        for coin_id in SCALP_COINS:
            aster_sym = ASTER_SYMBOLS.get(coin_id)
            alpaca_sym = CRYPTO_SYMBOLS.get(coin_id)
            if not aster_sym or not alpaca_sym:
                continue
            try:
                sig = self._evaluate_coin(coin_id, aster_sym, alpaca_sym)
                signals.append(sig)
            except Exception as exc:
                log.error("intraday_scalp error for %s: %s", coin_id, exc)
                signals.append(Signal(
                    strategy=self.name, symbol=alpaca_sym, action="hold",
                    strength=0.0, reason=f"{coin_id} scalp error: {exc}",
                ))
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": SCALP_COINS}

    def _evaluate_coin(self, coin_id: str, aster_sym: str, alpaca_sym: str) -> Signal:
        df = _fetch_5m_candles(aster_sym)
        if df is None:
            return Signal(
                strategy=self.name, symbol=alpaca_sym, action="hold",
                strength=0.0, reason=f"{coin_id} 5m data unavailable",
                data={"scalp": True, "error": "data_unavailable"},
            )

        vr_score, vr_debug = _vwap_rsi_score(df)
        ob = _ob_score(aster_sym)
        f_score, avg_funding = _funding_score(aster_sym)

        combined = vr_score * 0.5 + ob * 0.3 + f_score * 0.2

        if combined > MIN_SIGNAL_SCORE:
            action = "buy"
        elif combined < -MIN_SIGNAL_SCORE:
            action = "sell"
        else:
            action = "hold"

        strength = round(min(abs(combined), 1.0), 3)

        funding_str = f"{avg_funding:.4%}" if avg_funding is not None else "n/a"
        reason = (
            f"{coin_id} scalp {action}: RSI={vr_debug['rsi']:.0f} "
            f"{'▲' if vr_debug['above_vwap'] else '▼'}VWAP "
            f"OB={ob:+.2f} fund={funding_str} → score={combined:+.2f}"
        )

        return Signal(
            strategy=self.name,
            symbol=alpaca_sym,
            action=action,
            strength=strength,
            reason=reason,
            data={
                "leverage": LEVERAGE,
                "stop_pct": STOP_PCT,
                "take_profit_pct": TAKE_PROFIT_PCT,
                "scalp": True,
                **vr_debug,
                "ob_imbalance": round(ob, 3),
                "avg_funding": avg_funding,
                "vr_score": vr_score,
                "ob_score": round(ob, 3),
                "funding_score": f_score,
                "combined_score": round(combined, 4),
            },
        )
