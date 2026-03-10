"""RSI Divergence Strategy — detect bullish/bearish divergences on crypto OHLC."""

from trading.config import RSI_DIVERGENCE, CRYPTO_SYMBOLS
from trading.data.crypto import get_ohlc
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register
from trading.strategy.indicators import rsi, detect_divergence


@register
class RSIDivergenceStrategy(Strategy):
    """Buy on bullish RSI divergence (oversold), sell on bearish (overbought)."""

    name = "rsi_divergence"

    def __init__(self):
        self.rsi_period = RSI_DIVERGENCE["rsi_period"]
        self.ohlc_days = RSI_DIVERGENCE["ohlc_days"]
        self.lookback = RSI_DIVERGENCE["divergence_lookback"]
        self.oversold = RSI_DIVERGENCE["min_rsi_oversold"]
        self.overbought = RSI_DIVERGENCE["min_rsi_overbought"]
        self.coins = RSI_DIVERGENCE["coins"]
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
                if ohlc.empty or len(ohlc) < self.rsi_period + self.lookback:
                    signals.append(Signal(
                        strategy=self.name, symbol=alpaca_symbol, action="hold",
                        strength=0.0, reason=f"{coin_id} OHLC data too short ({len(ohlc) if not ohlc.empty else 0} candles)",
                    ))
                    continue

                rsi_series = rsi(ohlc["close"], self.rsi_period)
                rsi_valid = rsi_series.dropna()
                if len(rsi_valid) < self.lookback:
                    signals.append(Signal(
                        strategy=self.name, symbol=alpaca_symbol, action="hold",
                        strength=0.0, reason=f"{coin_id} insufficient RSI data ({len(rsi_valid)} valid points)",
                    ))
                    continue

                current_rsi = rsi_valid.iloc[-1]
                divergence = detect_divergence(ohlc["close"].iloc[-len(rsi_valid):], rsi_valid, self.lookback)

                context_data[coin_id] = {
                    "rsi": round(float(current_rsi), 1),
                    "divergence": divergence,
                    "price": round(float(ohlc["close"].iloc[-1]), 2),
                }

                if divergence == "bullish" and current_rsi < self.oversold:
                    strength = min((self.oversold - current_rsi) / self.oversold, 1.0)
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="buy",
                        strength=strength,
                        reason=f"{coin_id} RSI bullish divergence at {current_rsi:.0f} — oversold reversal",
                        data={"rsi": round(float(current_rsi), 1), "divergence": "bullish", "coin": coin_id},
                    ))
                elif divergence == "bearish" and current_rsi > self.overbought:
                    strength = min((current_rsi - self.overbought) / (100 - self.overbought), 1.0)
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="sell",
                        strength=strength,
                        reason=f"{coin_id} RSI bearish divergence at {current_rsi:.0f} — overbought reversal",
                        data={"rsi": round(float(current_rsi), 1), "divergence": "bearish", "coin": coin_id},
                    ))
                else:
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="hold",
                        strength=0.0,
                        reason=f"{coin_id} RSI {current_rsi:.0f} — no divergence detected",
                        data={"rsi": round(float(current_rsi), 1), "coin": coin_id},
                    ))

            except Exception as e:
                sym = CRYPTO_SYMBOLS.get(coin_id, "BTC/USD")
                signals.append(Signal(
                    strategy=self.name, symbol=sym, action="hold",
                    strength=0.0, reason=f"{coin_id} RSI error: {e}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
