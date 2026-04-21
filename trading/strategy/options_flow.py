"""Options Flow Strategy — Deribit put/call ratio + DVOL + 25d skew signals.

Generates mean-reversion signals when options market shows extreme positioning.
Put/call ratio > 1.5 = extreme fear → contrarian buy.
Put/call ratio < 0.6 = extreme greed → contrarian sell.
High DVOL (>70) = crash risk → dampens buys.
Strong put skew (>7) = tail risk hedging → bearish lean.
"""

import logging

from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

_COOLDOWN_SECONDS = 6 * 3600  # 6h between signals per symbol
_last_signal: dict[str, float] = {}


@register
class OptionsFlowStrategy(Strategy):
    """Mean-reversion signals from Deribit options market positioning."""

    name = "options_flow"

    # Thresholds
    PCR_BEAR_EXTREME = 1.5   # P/C > this → contrarian buy
    PCR_BEAR_STRONG = 1.2
    PCR_BULL_EXTREME = 0.6   # P/C < this → contrarian sell
    PCR_BULL_STRONG = 0.8
    DVOL_HIGH = 70            # High vol → dampen bullish signals
    DVOL_CRASH = 85           # Extreme vol → skip bullish entirely
    SKEW_BEAR = 7             # Put skew > this → bearish lean

    def generate_signals(self) -> list[Signal]:
        try:
            from trading.data.options import get_options_summary
        except ImportError:
            return []

        signals = []
        import time
        now = time.time()

        for currency in ("BTC", "ETH"):
            symbol_map = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
            symbol = symbol_map[currency]

            # Cooldown check
            if now - _last_signal.get(symbol, 0) < _COOLDOWN_SECONDS:
                continue

            try:
                opts = get_options_summary(currency)
            except Exception as e:
                log.debug("Options data fetch failed for %s: %s", currency, e)
                continue

            pcr = opts.get("put_call_ratio")
            dvol = opts.get("dvol")
            skew = opts.get("skew_25d")

            if pcr is None:
                continue

            action = None
            strength = 0.0
            reason_parts = [f"P/C={pcr:.2f}"]
            if dvol is not None:
                reason_parts.append(f"DVOL={dvol:.0f}")
            if skew is not None:
                reason_parts.append(f"skew={skew:.1f}")

            if pcr >= self.PCR_BEAR_EXTREME:
                # Extreme fear — contrarian buy
                if dvol and dvol >= self.DVOL_CRASH:
                    # Crash risk too high — skip
                    continue
                strength = min(0.8, 0.5 + (pcr - self.PCR_BEAR_EXTREME) * 0.3)
                if dvol and dvol >= self.DVOL_HIGH:
                    strength *= 0.6  # dampen in high vol
                action = "buy"
                reason_parts.append("extreme fear — contrarian long")

            elif pcr >= self.PCR_BEAR_STRONG:
                strength = 0.45
                if dvol and dvol >= self.DVOL_HIGH:
                    strength *= 0.7
                action = "buy"
                reason_parts.append("elevated put hedging — mild contrarian long")

            elif pcr <= self.PCR_BULL_EXTREME:
                # Extreme greed — contrarian sell
                strength = min(0.75, 0.5 + (self.PCR_BULL_EXTREME - pcr) * 0.5)
                if skew and skew > self.SKEW_BEAR:
                    strength = min(strength + 0.1, 0.9)  # confirm with skew
                action = "sell"
                reason_parts.append("extreme greed / low hedging — contrarian short")

            elif pcr <= self.PCR_BULL_STRONG:
                strength = 0.4
                action = "sell"
                reason_parts.append("greed positioning — mild contrarian short")

            elif skew and skew > self.SKEW_BEAR:
                # Skew alone signals tail risk even with neutral P/C
                strength = 0.4
                action = "sell"
                reason_parts.append(f"put skew={skew:.1f} signals tail risk hedging")

            if action:
                _last_signal[symbol] = now
                signals.append(Signal(
                    strategy=self.name,
                    symbol=symbol,
                    action=action,
                    strength=round(strength, 3),
                    reason=f"Options flow [{currency}]: {', '.join(reason_parts)}",
                    data={
                        "currency": currency,
                        "put_call_ratio": pcr,
                        "dvol": dvol,
                        "skew_25d": skew,
                    },
                ))

        return signals

    def get_market_context(self) -> dict:
        try:
            from trading.data.options import get_options_market_data
            return get_options_market_data()
        except Exception:
            return {}
