"""Intraday Scalping Strategy — VWAP/RSI + orderbook + funding + MTF + cycle bias.

Signal pipeline per coin:
  1. 5m VWAP/RSI (0.50) — mean-reversion and momentum-continuation setups
  2. Orderbook imbalance (0.30) — live bid/ask pressure
  3. Funding rate (0.20) — extreme funding predicts unwind direction
  4. 1h MTF multiplier — aligned 1h trend boosts (+35%), counter-trend suppresses (×0.25)
  5. 4h cycle bias (+0.15 additive) — quality signal from swing cycle wired in

Covers all 21 configured markets (20 crypto + GOLD). Tier-based leverage caps:
  T1 BTC/ETH/SOL → up to 18x | T2 major alts → up to 12x | T3 small alts/gold → 8x.
Stop 0.5%, take-profit 1.5% (3:1 R:R). Runs via a dedicated 5-minute cycle.
"""

import logging

from trading.config import ASTER_SYMBOLS, CRYPTO_SYMBOLS
from trading.strategy.base import Signal, Strategy
from trading.strategy.indicators import ema, rsi
from trading.strategy.registry import register

log = logging.getLogger(__name__)

SCALP_COINS = list(ASTER_SYMBOLS.keys())  # All 21 configured markets

STOP_PCT = 0.005         # 0.5% stop → 5% margin loss at 10x
TAKE_PROFIT_PCT = 0.015  # 1.5% target → 15% margin gain at 10x

RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
FUNDING_BUY_THRESHOLD = -0.0003
FUNDING_SELL_THRESHOLD = 0.0003
MIN_SIGNAL_SCORE = 0.28

# Tier-based leverage caps: deeper liquidity → higher max leverage allowed
_LEVERAGE_TIERS: dict[str, int] = {
    "bitcoin": 18, "ethereum": 18, "solana": 18,
    "bnb": 12, "xrp": 12, "avalanche-2": 12, "polkadot": 12,
    "chainlink": 12, "dogecoin": 12, "litecoin": 12, "bitcoin-cash": 12,
}


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _fetch_candles(aster_sym: str, interval: str, limit: int):
    try:
        from trading.data.aster import get_aster_ohlcv
        df = get_aster_ohlcv(aster_sym, interval=interval, limit=limit)
        if df is not None and not df.empty and len(df) >= 20:
            return df
        return None
    except Exception as e:
        log.debug("Candle fetch failed for %s %s: %s", aster_sym, interval, e)
        return None


def _calc_vwap(df) -> float:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol_sum = float(df["volume"].sum())
    if vol_sum <= 0:
        return float(df["close"].iloc[-1])
    return float((typical * df["volume"]).sum() / vol_sum)


# ---------------------------------------------------------------------------
# Sub-signal scorers
# ---------------------------------------------------------------------------

def _vwap_rsi_score(df) -> tuple[float, dict]:
    """5m VWAP+RSI score (-1 to +1) and debug fields."""
    closes = df["close"]
    rsi_val = float(rsi(closes, period=14).iloc[-1])
    ema9_val = float(ema(closes, 9).iloc[-1])
    ema21_val = float(ema(closes, 21).iloc[-1])
    price = float(closes.iloc[-1])
    vwap = _calc_vwap(df)

    above_vwap = price > vwap
    uptrend = ema9_val > ema21_val

    if rsi_val < RSI_OVERSOLD and not above_vwap:
        score = min((RSI_OVERSOLD - rsi_val) / RSI_OVERSOLD, 1.0)
    elif rsi_val > RSI_OVERBOUGHT and above_vwap:
        score = -min((rsi_val - RSI_OVERBOUGHT) / (100 - RSI_OVERBOUGHT), 1.0)
    elif 48 <= rsi_val <= 65 and above_vwap and uptrend:
        score = (rsi_val - 48) / 34 * 0.55
    elif 35 <= rsi_val <= 52 and not above_vwap and not uptrend:
        score = -(52 - rsi_val) / 34 * 0.55
    else:
        score = 0.0

    return round(score, 4), {
        "rsi": round(rsi_val, 1),
        "vwap": round(vwap, 2),
        "price": round(price, 2),
        "ema9": round(ema9_val, 2),
        "ema21": round(ema21_val, 2),
        "above_vwap": above_vwap,
        "uptrend": uptrend,
    }


def _ob_score(aster_sym: str) -> float:
    """Orderbook imbalance score (-1 to +1). 0.0 on failure."""
    try:
        from trading.data.aster import get_orderbook_imbalance
        ob = get_orderbook_imbalance(aster_sym, depth=20)
        return float(ob["imbalance"]) if ob else 0.0
    except Exception as e:
        log.debug("OB score failed for %s: %s", aster_sym, e)
        return 0.0


def _funding_score(aster_sym: str) -> tuple[float, float | None]:
    """Funding rate score (-1 to +1) and average funding value."""
    try:
        from trading.data.aster import get_funding_rate_history
        hist = get_funding_rate_history(aster_sym, limit=5)
        if not hist:
            return 0.0, None
        recent = hist[-3:] if len(hist) >= 3 else hist
        avg = sum(recent) / len(recent)
        if avg > FUNDING_SELL_THRESHOLD:
            score = -min(avg / 0.001, 1.0)
        elif avg < FUNDING_BUY_THRESHOLD:
            score = min(-avg / 0.001, 1.0)
        else:
            score = 0.0
        return round(score, 4), round(avg, 7)
    except Exception as e:
        log.debug("Funding score failed for %s: %s", aster_sym, e)
        return 0.0, None


def _get_1h_bias(aster_sym: str) -> float:
    """1h trend direction: +1 strongly bullish, -1 strongly bearish, 0 neutral.

    Used as a multiplier gate — aligned 5m signals are boosted, counter-trend
    signals are heavily suppressed (×0.25) to avoid fighting the larger trend.
    """
    try:
        df = _fetch_candles(aster_sym, "1h", 50)
        if df is None:
            return 0.0
        closes = df["close"]
        rsi_1h = float(rsi(closes, period=14).iloc[-1])
        ema9_1h = float(ema(closes, 9).iloc[-1])
        ema21_1h = float(ema(closes, 21).iloc[-1])
        if rsi_1h > 55 and ema9_1h > ema21_1h:
            return round(min((rsi_1h - 55) / 45.0, 1.0), 3)
        elif rsi_1h < 45 and ema9_1h < ema21_1h:
            return round(-min((45 - rsi_1h) / 45.0, 1.0), 3)
        return 0.0
    except Exception as e:
        log.debug("1h bias failed for %s: %s", aster_sym, e)
        return 0.0


def _get_cycle_bias(alpaca_sym: str) -> float:
    """Directional bias published by the last 4h swing cycle. Range -1 to +1.

    Returns 0.0 when the stored value is missing or older than 6 hours.
    A 4h BUY on BTC with strength 0.8 contributes +0.12 to the scalp combined
    score (cycle_bias × 0.15 weight), tipping borderline setups over threshold
    and nudging aligned signals toward the 18x leverage tier.
    """
    try:
        import json
        import time
        from trading.db.store import get_setting
        raw = get_setting("scalp_cycle_bias", "{}")
        data = json.loads(raw)
        coin = alpaca_sym.split("/")[0]  # "BTC", "ETH", "SOL"
        entry = data.get(coin)
        if not entry:
            return 0.0
        if time.time() - entry.get("ts", 0) > 21600:  # expire after 6h
            return 0.0
        action = entry.get("action", "hold")
        strength = float(entry.get("strength", 0.0))
        if action == "buy":
            return strength
        if action == "sell":
            return -strength
        return 0.0
    except Exception as e:
        log.debug("Cycle bias read failed for %s: %s", alpaca_sym, e)
        return 0.0


def _adaptive_leverage(abs_score: float, coin_id: str = "") -> int:
    """Scale leverage with signal conviction, capped by per-coin liquidity tier.

    Low conviction (0.28–0.50)  → 8x  (protect capital on weaker setups)
    Moderate conviction (0.50–0.70) → 12x
    High conviction (> 0.70)    → 18x  (press hard when edge is clear)

    Tier caps (from _LEVERAGE_TIERS): T1 BTC/ETH/SOL → 18x, T2 majors → 12x,
    T3 small alts + commodities → 8x max.
    """
    tier_max = _LEVERAGE_TIERS.get(coin_id, 8)
    if abs_score >= 0.70:
        return min(18, tier_max)
    elif abs_score >= 0.50:
        return min(12, tier_max)
    return min(8, tier_max)


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

@register
class IntradayScalpStrategy(Strategy):
    """5m scalping: VWAP/RSI + orderbook + funding + 1h MTF + 4h cycle bias."""

    name = "intraday_scalp"

    def generate_signals(self) -> list[Signal]:
        signals = []
        for coin_id in SCALP_COINS:
            aster_sym = ASTER_SYMBOLS.get(coin_id)
            alpaca_sym = CRYPTO_SYMBOLS.get(coin_id)
            if not aster_sym or not alpaca_sym:
                continue
            try:
                signals.append(self._evaluate_coin(coin_id, aster_sym, alpaca_sym))
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
        df_5m = _fetch_candles(aster_sym, "5m", 78)
        if df_5m is None:
            return Signal(
                strategy=self.name, symbol=alpaca_sym, action="hold",
                strength=0.0, reason=f"{coin_id} 5m data unavailable",
                data={"scalp": True, "error": "data_unavailable"},
            )

        # --- Step 1: 5m sub-signals ---
        vr_score, vr_debug = _vwap_rsi_score(df_5m)
        ob = _ob_score(aster_sym)
        f_score, avg_funding = _funding_score(aster_sym)
        combined = vr_score * 0.50 + ob * 0.30 + f_score * 0.20

        # --- Step 2: 1h MTF multiplier ---
        bias_1h = _get_1h_bias(aster_sym)
        if bias_1h != 0.0:
            same_dir = (combined >= 0) == (bias_1h >= 0)
            combined *= (1.0 + abs(bias_1h) * 0.35) if same_dir else 0.25

        # --- Step 3: 4h cycle bias (additive) ---
        cycle_bias = _get_cycle_bias(alpaca_sym)
        combined += cycle_bias * 0.15

        # --- Step 4: gate and adaptive leverage ---
        if combined > MIN_SIGNAL_SCORE:
            action = "buy"
        elif combined < -MIN_SIGNAL_SCORE:
            action = "sell"
        else:
            action = "hold"

        strength = round(min(abs(combined), 1.0), 3)
        leverage = _adaptive_leverage(abs(combined), coin_id)

        # --- Reason string (shows all layers for easy log reading) ---
        h1_label = "bull" if bias_1h > 0.1 else "bear" if bias_1h < -0.1 else "neut"
        cyc_label = "↑" if cycle_bias > 0.05 else "↓" if cycle_bias < -0.05 else "~"
        funding_str = f"{avg_funding:.4%}" if avg_funding is not None else "n/a"
        reason = (
            f"{coin_id} scalp {action}: RSI={vr_debug['rsi']:.0f} "
            f"{'▲' if vr_debug['above_vwap'] else '▼'}VWAP "
            f"1h={h1_label} cyc={cyc_label} OB={ob:+.2f} fund={funding_str} "
            f"→ {combined:+.2f} @ {leverage}x"
        )

        return Signal(
            strategy=self.name,
            symbol=alpaca_sym,
            action=action,
            strength=strength,
            reason=reason,
            data={
                "leverage": leverage,
                "stop_pct": STOP_PCT,
                "take_profit_pct": TAKE_PROFIT_PCT,
                "scalp": True,
                **vr_debug,
                "ob_imbalance": round(ob, 3),
                "avg_funding": avg_funding,
                "bias_1h": bias_1h,
                "cycle_bias": round(cycle_bias, 3),
                "vr_score": vr_score,
                "ob_score": round(ob, 3),
                "funding_score": f_score,
                "combined_score": round(combined, 4),
            },
        )
