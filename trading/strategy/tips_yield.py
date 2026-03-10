"""TIPS Yield Strategy — trade gold based on real interest rate movements."""

from trading.config import TIPS_YIELD, FRED_API_KEY
from trading.data.commodities import get_fred_series
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register
from trading.strategy.indicators import z_score as calc_z_score


@register
class TIPSYieldStrategy(Strategy):
    """Long GLD when real rates are dropping (z-score negative), reduce when rising."""

    name = "tips_yield"

    def __init__(self):
        self.series_id = TIPS_YIELD["fred_series"]
        self.lookback = TIPS_YIELD["lookback_days"]
        self.z_threshold = TIPS_YIELD["z_threshold"]
        self.gold_symbol = TIPS_YIELD["gold_symbol"]
        self._last_context = None

    def generate_signals(self) -> list[Signal]:
        # Skip if no FRED API key configured
        if not FRED_API_KEY:
            return [Signal(
                strategy=self.name,
                symbol=self.gold_symbol,
                action="hold",
                strength=0.0,
                reason="FRED_API_KEY not configured — skipping TIPS yield strategy",
            )]

        try:
            df = get_fred_series(self.series_id, limit=self.lookback + 30)
        except Exception:
            return [Signal(
                strategy=self.name,
                symbol=self.gold_symbol,
                action="hold",
                strength=0.0,
                reason="Failed to fetch FRED TIPS yield data",
            )]

        if df.empty or len(df) < 20:
            return [Signal(
                strategy=self.name,
                symbol=self.gold_symbol,
                action="hold",
                strength=0.0,
                reason="Insufficient TIPS yield data",
            )]

        # Calculate z-score of TIPS yield changes
        yields = df["value"]
        z_series = calc_z_score(yields, min(self.lookback, len(yields) - 1))
        current_z = z_series.iloc[-1]
        current_yield = yields.iloc[-1]

        # Rate of change (30-day)
        roc = yields.iloc[-1] - yields.iloc[0] if len(yields) > 1 else 0

        self._last_context = {
            "tips_yield": round(current_yield, 3),
            "z_score": round(current_z, 2) if not (current_z != current_z) else 0,
            "yield_roc_30d": round(roc, 3),
            "lookback": self.lookback,
        }

        # Handle NaN z-score
        if current_z != current_z:  # NaN check
            return [Signal(
                strategy=self.name,
                symbol=self.gold_symbol,
                action="hold",
                strength=0.0,
                reason="TIPS yield z-score unavailable",
                data=self._last_context,
            )]

        signals = []

        if current_z < -self.z_threshold:
            # Real rates dropping → gold bullish
            strength = min(abs(current_z) / 3.0, 1.0)
            signals.append(Signal(
                strategy=self.name,
                symbol=self.gold_symbol,
                action="buy",
                strength=strength,
                reason=f"TIPS yield z-score {current_z:.1f} — real rates falling, buy gold",
                data=self._last_context,
            ))
        elif current_z > self.z_threshold:
            # Real rates rising → gold bearish
            strength = min(current_z / 3.0, 1.0)
            signals.append(Signal(
                strategy=self.name,
                symbol=self.gold_symbol,
                action="sell",
                strength=strength,
                reason=f"TIPS yield z-score {current_z:.1f} — real rates rising, reduce gold",
                data=self._last_context,
            ))
        else:
            signals.append(Signal(
                strategy=self.name,
                symbol=self.gold_symbol,
                action="hold",
                strength=0.0,
                reason=f"TIPS yield z-score {current_z:.1f} — neutral range",
                data=self._last_context,
            ))

        return signals

    def get_market_context(self) -> dict:
        return {
            "strategy": self.name,
            **(self._last_context or {}),
            "z_threshold": self.z_threshold,
        }
