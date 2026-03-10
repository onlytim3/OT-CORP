"""Bollinger Band Squeeze Strategy — trade breakouts after volatility contraction."""

from trading.config import BOLLINGER_SQUEEZE, CRYPTO_SYMBOLS
from trading.data.crypto import get_ohlc
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register
from trading.strategy.indicators import bollinger_bands


@register
class BollingerSqueezeStrategy(Strategy):
    """Buy/sell on breakout after Bollinger Band squeeze (low volatility → expansion)."""

    name = "bollinger_squeeze"

    def __init__(self):
        self.bb_period = BOLLINGER_SQUEEZE["bb_period"]
        self.bb_std = BOLLINGER_SQUEEZE["bb_std"]
        self.squeeze_pctl = BOLLINGER_SQUEEZE["squeeze_percentile"]
        self.ohlc_days = BOLLINGER_SQUEEZE["ohlc_days"]
        self.coins = BOLLINGER_SQUEEZE["coins"]
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
                if ohlc.empty or len(ohlc) < self.bb_period + 10:
                    signals.append(Signal(
                        strategy=self.name, symbol=alpaca_symbol, action="hold",
                        strength=0.0, reason=f"{coin_id} OHLC data too short ({len(ohlc) if not ohlc.empty else 0} candles)",
                    ))
                    continue

                close = ohlc["close"]
                upper, middle, lower, bandwidth = bollinger_bands(
                    close, self.bb_period, self.bb_std
                )

                # Drop NaN values from SMA warmup
                bw_valid = bandwidth.dropna()
                if len(bw_valid) < 20:
                    signals.append(Signal(
                        strategy=self.name, symbol=alpaca_symbol, action="hold",
                        strength=0.0, reason=f"{coin_id} insufficient BB data ({len(bw_valid)} valid points)",
                    ))
                    continue

                current_bw = float(bw_valid.iloc[-1])
                prev_bw = float(bw_valid.iloc[-2])
                current_price = float(close.iloc[-1])
                upper_now = float(upper.iloc[-1])
                lower_now = float(lower.iloc[-1])
                middle_now = float(middle.iloc[-1])

                # Calculate percentile of current bandwidth in history
                percentile = float((bw_valid < current_bw).mean() * 100)

                # Was squeezed (previous bandwidth was low)?
                prev_percentile = float((bw_valid.iloc[:-1] < prev_bw).mean() * 100)
                was_squeezed = bool(prev_percentile <= self.squeeze_pctl)

                context_data[coin_id] = {
                    "price": round(current_price, 2),
                    "upper": round(upper_now, 2),
                    "lower": round(lower_now, 2),
                    "middle": round(middle_now, 2),
                    "bandwidth_pctl": round(percentile, 1),
                    "was_squeezed": was_squeezed,
                }

                # Breakout after squeeze
                if was_squeezed and current_price > upper_now:
                    band_width = upper_now - middle_now
                    strength = min((current_price - upper_now) / band_width, 1.0) if band_width > 0 else 0.5
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="buy",
                        strength=max(strength, 0.4),
                        reason=f"{coin_id} Bollinger squeeze breakout UP — bandwidth was {prev_percentile:.0f}th pctl",
                        data={**context_data[coin_id], "breakout": "up", "coin": coin_id},
                    ))
                elif was_squeezed and current_price < lower_now:
                    band_width = middle_now - lower_now
                    strength = min((lower_now - current_price) / band_width, 1.0) if band_width > 0 else 0.5
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="sell",
                        strength=max(strength, 0.4),
                        reason=f"{coin_id} Bollinger squeeze breakout DOWN — bandwidth was {prev_percentile:.0f}th pctl",
                        data={**context_data[coin_id], "breakout": "down", "coin": coin_id},
                    ))
                else:
                    state = "squeezed" if percentile <= self.squeeze_pctl else "normal"
                    signals.append(Signal(
                        strategy=self.name,
                        symbol=alpaca_symbol,
                        action="hold",
                        strength=0.0,
                        reason=f"{coin_id} BB {state} — bandwidth {percentile:.0f}th pctl, no breakout",
                        data={**context_data[coin_id], "coin": coin_id},
                    ))

            except Exception as e:
                sym = CRYPTO_SYMBOLS.get(coin_id, "BTC/USD")
                signals.append(Signal(
                    strategy=self.name, symbol=sym, action="hold",
                    strength=0.0, reason=f"{coin_id} BB error: {e}",
                ))

        self._last_context = context_data
        return signals

    def get_market_context(self) -> dict:
        return {"strategy": self.name, "coins": self._last_context}
