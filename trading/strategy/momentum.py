"""Crypto Momentum Strategy — buy top performers, sell laggards."""

from trading.config import MOMENTUM, CRYPTO_SYMBOLS
from trading.data.crypto import get_market_data
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register


@register
class MomentumStrategy(Strategy):
    """Rank coins by 7-day return, buy top N, sell bottom N."""

    name = "momentum"

    def __init__(self):
        self.lookback = MOMENTUM["lookback_days"]
        self.top_n = MOMENTUM["top_n"]
        self.entry_threshold = MOMENTUM["entry_threshold"]
        self.exit_threshold = MOMENTUM["exit_threshold"]
        self.coins = MOMENTUM["coins"]
        self._last_data = None

    def generate_signals(self) -> list[Signal]:
        df = get_market_data(self.coins)
        if df.empty:
            return []
        self._last_data = df

        # Sort by 7-day return
        col = "price_change_7d"
        if col not in df.columns:
            return []

        df = df.dropna(subset=[col]).sort_values(col, ascending=False)
        signals = []

        # Top N coins → BUY signals
        for _, row in df.head(self.top_n).iterrows():
            change = row[col] / 100  # Convert percentage to decimal
            if change >= self.entry_threshold:
                alpaca_symbol = CRYPTO_SYMBOLS.get(row["id"])
                if not alpaca_symbol:
                    continue
                signals.append(Signal(
                    strategy=self.name,
                    symbol=alpaca_symbol,
                    action="buy",
                    strength=min(change / 0.20, 1.0),  # Normalize: 20% return = max strength
                    reason=f"{row['name']} up {row[col]:.1f}% in 7d — momentum buy",
                    data={"coin_id": row["id"], "7d_change": row[col], "price": row["current_price"]},
                ))

        # Bottom coins with negative returns → SELL signals
        for _, row in df.tail(self.top_n).iterrows():
            change = row[col] / 100
            if change <= self.exit_threshold:
                alpaca_symbol = CRYPTO_SYMBOLS.get(row["id"])
                if not alpaca_symbol:
                    continue
                signals.append(Signal(
                    strategy=self.name,
                    symbol=alpaca_symbol,
                    action="sell",
                    strength=min(abs(change) / 0.20, 1.0),
                    reason=f"{row['name']} down {row[col]:.1f}% in 7d — momentum sell",
                    data={"coin_id": row["id"], "7d_change": row[col], "price": row["current_price"]},
                ))

        # Everything else → HOLD
        middle_ids = set(df["id"]) - set(df.head(self.top_n)["id"]) - set(df.tail(self.top_n)["id"])
        for coin_id in middle_ids:
            alpaca_symbol = CRYPTO_SYMBOLS.get(coin_id)
            if alpaca_symbol:
                row = df[df["id"] == coin_id].iloc[0]
                signals.append(Signal(
                    strategy=self.name,
                    symbol=alpaca_symbol,
                    action="hold",
                    strength=0.0,
                    reason=f"{row['name']} {row[col]:+.1f}% in 7d — hold",
                    data={"coin_id": coin_id, "7d_change": row[col]},
                ))

        return signals

    def get_market_context(self) -> dict:
        if self._last_data is None:
            self._last_data = get_market_data(self.coins)
        df = self._last_data
        if df.empty:
            return {}
        return {
            "strategy": self.name,
            "coins_tracked": len(df),
            "avg_7d_change": round(df.get("price_change_7d", 0).mean(), 2) if "price_change_7d" in df.columns else None,
            "best_performer": df.iloc[0]["name"] if not df.empty else None,
            "worst_performer": df.iloc[-1]["name"] if not df.empty else None,
        }
