"""EMA Crossover Strategy — buy/sell on fast/slow EMA crosses with trend filter."""

from trading.config import EMA_CROSSOVER, CRYPTO_SYMBOLS
from trading.data.crypto import get_ohlc
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register
from trading.strategy.indicators import ema


@register
class EMACrossoverStrategy(Strategy):
    """Buy when EMA(8) crosses above EMA(21) with EMA(50) trend confirmation."""

    name = "ema_crossover"

    def __init__(self):
        self.fast = EMA_CROSSOVER["fast_period"]
        self.slow = EMA_CROSSOVER["slow_period"]
        self.trend = EMA_CROSSOVER["trend_period"]
        self.ohlc_days = EMA_CROSSOVER["ohlc_days"]
        self.coins = EMA_CROSSOVER["coins"]
        self._last_context = {}

    def generate_signals(self) -> list[Signal]:
        signals = []
        context_data = {}

        for coin_id in self.coins:
            try:
                alpaca_symbol = CRYPTO_SYMBOLS.get(coin_id)
                if not alpaca_symbol:
                    continue

                ohlc = get_ohlc(coin_id, self.ohlc_days)
                if ohlc.empty or len(ohlc) < self.trend + 2:
                    signals.append(Signal(
                        strategy=self.name, symbol=alpaca_symbol, action="hold",
                        strength=0.0, reason=f"{coin_id} OHLC data too short ({len(ohlc) if not ohlc.empty else 0} candles)",
                    ))
                    continue

                close = ohlc["close"]
                ema_fast = ema(close, self.fast)
                ema_slow = ema(close, self.slow)
                ema_trend = ema(close, self.trend)

                # Drop NaN warmup values — EMA needs period candles to stabilize
                valid_start = self.trend  # Longest EMA period
                if len(close) <= valid_start + 2:
                    signals.append(Signal(
                        strategy=self.name, symbol=alpaca_symbol, action="hold",
                        strength=0.0, reason=f"{coin_id} insufficient data for EMA({self.trend})",
                    ))
                    continue

                current_price = float(close.iloc[-1])
                fast_now = float(ema_fast.iloc[-1])
                fast_prev = float(ema_fast.iloc[-2])
                slow_now = float(ema_slow.iloc[-1])
                slow_prev = float(ema_slow.iloc[-2])
                trend_now = float(ema_trend.iloc[-1])

                context_data[coin_id] = {
                    "price": round(current_price, 2),
                    "ema_fast": round(fast_now, 2),
                    "ema_slow": round(slow_now, 2),
                    "ema_trend": round(trend_now, 2),
                }

                # Bullish crossover: fast crosses above slow, price above trend
                crossed_up = fast_prev <= slow_prev and fast_now > slow_now
                # Bearish crossover: fast crosses below slow, price below trend
                crossed_down = fast_prev >= slow_prev and fast_now < slow_now

                if crossed_up and current_price > trend_now:
                    gap_pct = (current_price - trend_now) / trend_now
                    strength = min(gap_pct / 0.10, 1.0)
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="buy",
                        strength=max(strength, 0.3),
                        reason=f"{coin_id} EMA({self.fast}) crossed above EMA({self.slow}) — bullish trend",
                        data={**context_data[coin_id], "crossover": "bullish", "coin": coin_id},
                    ))
                elif crossed_down and current_price < trend_now:
                    gap_pct = (trend_now - current_price) / trend_now
                    strength = min(gap_pct / 0.10, 1.0)
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="sell",
                        strength=max(strength, 0.3),
                        reason=f"{coin_id} EMA({self.fast}) crossed below EMA({self.slow}) — bearish trend",
                        data={**context_data[coin_id], "crossover": "bearish", "coin": coin_id},
                    ))
                else:
                    position = "above" if fast_now > slow_now else "below"
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="hold",
                        strength=0.0,
                        reason=f"{coin_id} EMA({self.fast}) {position} EMA({self.slow}) — no crossover",
                        data={**context_data[coin_id], "coin": coin_id},
                    ))

            except Exception as e:
                sym = CRYPTO_SYMBOLS.get(coin_id, "BTC/USD")
                signals.append(Signal(
                    strategy=self.name, symbol=sym, action="hold",
                    strength=0.0, reason=f"{coin_id} EMA error: {e}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
