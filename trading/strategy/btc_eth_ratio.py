"""BTC/ETH Ratio Mean Reversion — trade when the ratio deviates from its mean."""

import pandas as pd

from trading.config import BTC_ETH_RATIO
from trading.data.crypto import get_historical_prices
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register


@register
class BTCETHRatioStrategy(Strategy):
    """Buy the underperformer when BTC/ETH ratio z-score exceeds threshold."""

    name = "btc_eth_ratio"

    def __init__(self):
        self.lookback = BTC_ETH_RATIO["lookback_days"]
        self.entry_z = BTC_ETH_RATIO["entry_z"]
        self.exit_z = BTC_ETH_RATIO["exit_z"]
        self.btc_symbol = BTC_ETH_RATIO["btc_symbol"]
        self.eth_symbol = BTC_ETH_RATIO["eth_symbol"]
        self._last_context = None

    def _get_ratio_data(self) -> pd.DataFrame | None:
        """Get BTC/ETH price ratio over the lookback period."""
        try:
            btc = get_historical_prices("bitcoin", days=self.lookback)
            eth = get_historical_prices("ethereum", days=self.lookback)

            if btc.empty or eth.empty:
                return None

            btc_daily = btc["price"].resample("D").last().dropna()
            eth_daily = eth["price"].resample("D").last().dropna()

            combined = pd.DataFrame({"btc": btc_daily, "eth": eth_daily}).dropna()
            if len(combined) < 20:  # Need at least 20 days for stats
                return None

            combined["ratio"] = combined["btc"] / combined["eth"]
            return combined

        except Exception:
            return None

    def generate_signals(self) -> list[Signal]:
        data = self._get_ratio_data()
        if data is None or len(data) < 20:
            return [Signal(
                strategy=self.name,
                symbol=self.btc_symbol,
                action="hold",
                strength=0.0,
                reason="Insufficient data for BTC/ETH ratio analysis",
            )]

        ratio = data["ratio"]
        mean_ratio = ratio.mean()
        std_ratio = ratio.std()

        if std_ratio == 0:
            return []

        current_ratio = ratio.iloc[-1]
        z = (current_ratio - mean_ratio) / std_ratio

        self._last_context = {
            "current_ratio": round(current_ratio, 2),
            "mean_ratio": round(mean_ratio, 2),
            "std_ratio": round(std_ratio, 2),
            "z_score": round(z, 2),
            "btc_price": round(data["btc"].iloc[-1], 2),
            "eth_price": round(data["eth"].iloc[-1], 2),
        }

        signals = []

        if z > self.entry_z:
            # BTC outperforming ETH — ratio too high — buy ETH, reduce BTC
            strength = min((z - self.entry_z) / 2, 1.0)
            signals.append(Signal(
                strategy=self.name,
                symbol=self.eth_symbol,
                action="buy",
                strength=strength,
                reason=f"BTC/ETH ratio z-score {z:.1f} — ETH undervalued, buy ETH",
                data=self._last_context,
            ))
            signals.append(Signal(
                strategy=self.name,
                symbol=self.btc_symbol,
                action="sell",
                strength=strength,
                reason=f"BTC/ETH ratio z-score {z:.1f} — reduce BTC exposure",
                data=self._last_context,
            ))

        elif z < -self.entry_z:
            # ETH outperforming BTC — ratio too low — buy BTC, reduce ETH
            strength = min((abs(z) - self.entry_z) / 2, 1.0)
            signals.append(Signal(
                strategy=self.name,
                symbol=self.btc_symbol,
                action="buy",
                strength=strength,
                reason=f"BTC/ETH ratio z-score {z:.1f} — BTC undervalued, buy BTC",
                data=self._last_context,
            ))
            signals.append(Signal(
                strategy=self.name,
                symbol=self.eth_symbol,
                action="sell",
                strength=strength,
                reason=f"BTC/ETH ratio z-score {z:.1f} — reduce ETH exposure",
                data=self._last_context,
            ))

        else:
            signals.append(Signal(
                strategy=self.name,
                symbol=self.btc_symbol,
                action="hold",
                strength=0.0,
                reason=f"BTC/ETH ratio z-score {z:.1f} — within normal range (±{self.entry_z})",
                data=self._last_context,
            ))

        return signals

    def get_market_context(self) -> dict:
        return {
            "strategy": self.name,
            **(self._last_context or {}),
            "entry_z": self.entry_z,
            "exit_z": self.exit_z,
        }
