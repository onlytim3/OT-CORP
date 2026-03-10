"""DXY Dollar Index Strategy — trade commodities based on dollar strength."""

from trading.config import DXY_DOLLAR
from trading.data.commodities import get_etf_history
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register
from trading.strategy.indicators import sma


@register
class DXYDollarStrategy(Strategy):
    """Long GLD/SLV when dollar weakening (DXY below SMAs), reduce when strengthening."""

    name = "dxy_dollar"

    def __init__(self):
        self.dxy_ticker = DXY_DOLLAR["dxy_ticker"]
        self.sma_fast = DXY_DOLLAR["sma_fast"]
        self.sma_slow = DXY_DOLLAR["sma_slow"]
        self.gold = DXY_DOLLAR["gold_symbol"]
        self.silver = DXY_DOLLAR["silver_symbol"]
        self._last_context = None

    def generate_signals(self) -> list[Signal]:
        try:
            dxy = get_etf_history(self.dxy_ticker, period="3mo")
        except Exception:
            return [Signal(
                strategy=self.name,
                symbol=self.gold,
                action="hold",
                strength=0.0,
                reason="Failed to fetch DXY data",
            )]

        if dxy.empty or len(dxy) < self.sma_slow + 5:
            return [Signal(
                strategy=self.name,
                symbol=self.gold,
                action="hold",
                strength=0.0,
                reason="Insufficient DXY data for SMA analysis",
            )]

        close = dxy["Close"]
        sma_fast = sma(close, self.sma_fast)
        sma_slow_series = sma(close, self.sma_slow)

        current_dxy = close.iloc[-1]
        fast_now = sma_fast.iloc[-1]
        slow_now = sma_slow_series.iloc[-1]

        self._last_context = {
            "dxy": round(float(current_dxy), 2),
            "sma_fast": round(float(fast_now), 2),
            "sma_slow": round(float(slow_now), 2),
            "below_fast": bool(current_dxy < fast_now),
            "below_slow": bool(current_dxy < slow_now),
        }

        signals = []

        if current_dxy < fast_now and current_dxy < slow_now:
            # Dollar weak — below both SMAs → long commodities
            weakness = (slow_now - current_dxy) / slow_now
            strength = min(weakness / 0.05, 1.0)  # 5% below SMA50 = max
            signals.append(Signal(
                strategy=self.name,
                symbol=self.gold,
                action="buy",
                strength=max(strength, 0.3),
                reason=f"DXY {current_dxy:.1f} below SMA({self.sma_fast})={fast_now:.1f} & SMA({self.sma_slow})={slow_now:.1f} — dollar weak, buy gold",
                data={**self._last_context, "commodity": "gold"},
            ))
            signals.append(Signal(
                strategy=self.name,
                symbol=self.silver,
                action="buy",
                strength=max(strength * 0.8, 0.2),  # Silver slightly lower conviction
                reason=f"DXY {current_dxy:.1f} weak — buy silver (beta play on dollar weakness)",
                data={**self._last_context, "commodity": "silver"},
            ))

        elif current_dxy > fast_now and current_dxy > slow_now:
            # Dollar strong — above both SMAs → reduce commodity exposure
            power = (current_dxy - slow_now) / slow_now
            strength = min(power / 0.05, 1.0)
            signals.append(Signal(
                strategy=self.name,
                symbol=self.gold,
                action="sell",
                strength=max(strength, 0.3),
                reason=f"DXY {current_dxy:.1f} above SMA({self.sma_fast})={fast_now:.1f} & SMA({self.sma_slow})={slow_now:.1f} — dollar strong, reduce gold",
                data={**self._last_context, "commodity": "gold"},
            ))
            signals.append(Signal(
                strategy=self.name,
                symbol=self.silver,
                action="sell",
                strength=max(strength * 0.8, 0.2),
                reason=f"DXY {current_dxy:.1f} strong — reduce silver",
                data={**self._last_context, "commodity": "silver"},
            ))

        else:
            # Mixed — DXY between the two SMAs
            signals.append(Signal(
                strategy=self.name,
                symbol=self.gold,
                action="hold",
                strength=0.0,
                reason=f"DXY {current_dxy:.1f} between SMAs — mixed dollar signal",
                data=self._last_context,
            ))

        return signals

    def get_market_context(self) -> dict:
        return {
            "strategy": self.name,
            **(self._last_context or {}),
            "sma_fast_period": self.sma_fast,
            "sma_slow_period": self.sma_slow,
        }
