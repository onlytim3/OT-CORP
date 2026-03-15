"""Backtesting engine -- run strategies against historical data without live API calls.

The engine fetches all historical data upfront, then uses unittest.mock.patch to
temporarily replace data functions during strategy.generate_signals(). This means
strategies require zero modifications to be backtested.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from trading.data.commodities import get_etf_history, get_fred_series
from trading.data.crypto import get_historical_prices, get_market_data, get_ohlc, get_prices
from trading.data.sentiment import get_fear_greed
from trading.strategy.base import Signal
from trading.strategy.registry import get_strategy

logger = logging.getLogger(__name__)
console = Console()

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    """Container for all backtest outputs."""

    strategy_name: str
    trades: list[dict] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)
    portfolio_values: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    start_date: str = ""
    end_date: str = ""
    starting_capital: float = 0.0


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------


class Backtester:
    """Simulates strategy execution over historical data day-by-day.

    The engine patches the live data functions so that strategies see only
    the data available up to the current simulation date. Orders generated
    on day T are filled at day T+1 open price, with a configurable
    commission applied to every fill.

    Leverage support:
        When ``leverage > 1``, positions are margined. P&L is amplified by the
        leverage factor. Positions are liquidated when unrealized loss exceeds
        the maintenance margin (``1 / leverage * 0.8`` of notional).
    """

    def __init__(
        self,
        starting_capital: float = 100_000,
        commission_pct: float = 0.001,
        leverage: int = 1,
    ) -> None:
        self.starting_capital = starting_capital
        self.commission_pct = commission_pct
        self.leverage = max(1, leverage)

        # Mutable state -- reset on each run
        self._cash: float = 0.0
        self._positions: dict[str, dict] = {}  # symbol -> {qty, avg_cost, margin}
        self._trade_log: list[dict] = []
        self._daily_values: list[dict] = []
        self._all_signals: list[dict] = []
        self._liquidations: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        strategy_name: str,
        historical_data: dict,
        start_date: str,
        end_date: str,
    ) -> BacktestResult:
        """Run a strategy over *historical_data* from *start_date* to *end_date*.

        Parameters
        ----------
        strategy_name:
            Registered strategy name (e.g. ``"ema_crossover"``).
        historical_data:
            Pre-fetched data keyed by type:
            - ``"ohlc"``:  ``dict[coin_id, DataFrame]`` with timestamp index
              and columns open/high/low/close.
            - ``"prices"``: ``dict[coin_id, float]`` latest prices per step.
            - ``"fear_greed"``: ``list[dict]`` daily F&G values with keys
              ``value``, ``value_classification``, ``timestamp``.
            - ``"etf_history"``: ``dict[symbol, DataFrame]`` with OHLCV data.
            - ``"historical_prices"``: ``dict[coin_id, DataFrame]`` with
              timestamp index and columns price, volume.
            - ``"market_data"``: ``DataFrame`` with coin market data.
            - ``"fred_series"``: ``dict[series_id, DataFrame]`` with FRED data.
        start_date:
            ISO date string for first simulation day (inclusive).
        end_date:
            ISO date string for last simulation day (inclusive).

        Returns
        -------
        BacktestResult
        """
        strategy = get_strategy(strategy_name)
        if strategy is None:
            raise ValueError(f"Unknown strategy: {strategy_name!r}")

        # Reset state
        self._cash = self.starting_capital
        self._positions = {}
        self._trade_log = []
        self._daily_values = []
        self._all_signals = []
        self._liquidations = 0

        dt_start = pd.Timestamp(start_date, tz="UTC")
        dt_end = pd.Timestamp(end_date, tz="UTC")
        date_range = pd.date_range(dt_start, dt_end, freq="D", tz="UTC")

        # Pending orders from previous day's signals -- filled at today's open
        pending_orders: list[dict] = []

        for current_date in date_range:
            # 1. Fill pending orders at today's open price
            for order in pending_orders:
                price = self._get_open_price(
                    order["symbol"], current_date, historical_data
                )
                if price is not None:
                    trade = self._simulate_order(
                        symbol=order["symbol"],
                        side=order["side"],
                        price=price,
                        notional=order["notional"],
                        date=current_date,
                    )
                    if trade:
                        self._trade_log.append(trade)
            pending_orders = []

            # 2. Build mocked data functions scoped to current_date
            patches = self._build_patches(historical_data, current_date)

            # 3. Generate signals with patched data layer
            try:
                with _apply_patches(patches):
                    signals = strategy.generate_signals()
            except Exception as exc:
                logger.warning(
                    "Strategy %s raised on %s: %s",
                    strategy_name,
                    current_date.date(),
                    exc,
                )
                signals = []

            # 4. Record signals and queue orders for next day
            for sig in signals:
                sig_dict = {
                    "date": str(current_date.date()),
                    "symbol": sig.symbol,
                    "action": sig.action,
                    "strength": sig.strength,
                    "reason": sig.reason,
                }
                self._all_signals.append(sig_dict)

                if sig.is_actionable:
                    # Size the order based on signal strength
                    allocation = self._cash * 0.1 * sig.strength
                    if allocation > 1.0:
                        pending_orders.append(
                            {
                                "symbol": sig.symbol,
                                "side": sig.action,
                                "notional": allocation,
                            }
                        )

            # 5. Check for liquidations (leveraged positions)
            if self.leverage > 1:
                self._check_liquidations(current_date, historical_data)

            # 6. Snapshot portfolio value at end of day
            self._record_daily_value(current_date, historical_data)

        # Fill any remaining pending orders on the last day
        # (no next day to fill, skip)

        metrics = self._calculate_metrics()

        return BacktestResult(
            strategy_name=strategy_name,
            trades=list(self._trade_log),
            signals=list(self._all_signals),
            portfolio_values=list(self._daily_values),
            metrics=metrics,
            start_date=start_date,
            end_date=end_date,
            starting_capital=self.starting_capital,
        )

    # ------------------------------------------------------------------
    # Order simulation
    # ------------------------------------------------------------------

    def _simulate_order(
        self,
        symbol: str,
        side: str,
        price: float,
        notional: float,
        date: pd.Timestamp | None = None,
    ) -> dict | None:
        """Simulate a buy or sell at *price* for a given notional amount.

        With leverage > 1, the margin required is ``notional / leverage``.
        P&L is computed on the full notional, amplifying gains and losses.

        Returns a trade record dict or ``None`` if the order cannot be filled.
        """
        commission = notional * self.commission_pct
        qty = (notional - commission) / price if price > 0 else 0.0

        if qty <= 0:
            return None

        trade: dict = {
            "date": str(date.date()) if date is not None else "",
            "symbol": symbol,
            "side": side,
            "price": round(price, 6),
            "qty": round(qty, 8),
            "notional": round(notional, 2),
            "commission": round(commission, 2),
            "leverage": self.leverage,
        }

        if side == "buy":
            # With leverage, only margin is deducted from cash
            margin_required = notional / self.leverage
            if margin_required > self._cash:
                # Scale down to what we can afford
                margin_required = self._cash
                notional = margin_required * self.leverage
                commission = notional * self.commission_pct
                qty = (notional - commission) / price if price > 0 else 0.0
                if qty <= 0:
                    return None
                trade["notional"] = round(notional, 2)
                trade["qty"] = round(qty, 8)
                trade["commission"] = round(commission, 2)

            self._cash -= margin_required
            pos = self._positions.setdefault(
                symbol, {"qty": 0.0, "avg_cost": 0.0, "margin": 0.0}
            )
            total_cost = pos["avg_cost"] * pos["qty"] + price * qty
            pos["qty"] += qty
            pos["avg_cost"] = total_cost / pos["qty"] if pos["qty"] else 0.0
            pos["margin"] = pos.get("margin", 0.0) + margin_required
            trade["margin_used"] = round(margin_required, 2)

        elif side == "sell":
            pos = self._positions.get(symbol)
            if pos is None or pos["qty"] <= 0:
                return None  # Nothing to sell

            sell_qty = min(qty, pos["qty"])
            sell_fraction = sell_qty / pos["qty"] if pos["qty"] > 0 else 1.0

            # P&L is on the full leveraged notional
            price_change_pct = (price - pos["avg_cost"]) / pos["avg_cost"] if pos["avg_cost"] > 0 else 0
            leveraged_pnl = pos.get("margin", sell_qty * pos["avg_cost"] / self.leverage) * sell_fraction * price_change_pct * self.leverage

            # Return margin + leveraged P&L
            margin_returned = pos.get("margin", 0.0) * sell_fraction
            proceeds = margin_returned + leveraged_pnl
            commission = abs(proceeds) * self.commission_pct
            self._cash += proceeds - commission

            trade["qty"] = round(sell_qty, 8)
            trade["notional"] = round(sell_qty * price, 2)
            trade["commission"] = round(commission, 2)
            trade["pnl"] = round(leveraged_pnl, 2)
            trade["margin_returned"] = round(margin_returned, 2)

            pos["qty"] -= sell_qty
            pos["margin"] = pos.get("margin", 0.0) * (1 - sell_fraction)
            if pos["qty"] <= 1e-12:
                del self._positions[symbol]

        return trade

    def _check_liquidations(
        self, date: pd.Timestamp, historical_data: dict
    ) -> None:
        """Liquidate positions where unrealized loss exceeds maintenance margin.

        Maintenance margin = 80% of initial margin. If the position's
        unrealized P&L drops below -maintenance_margin, the position is
        force-closed at current price with the margin fully lost.
        """
        to_liquidate = []
        for symbol, pos in list(self._positions.items()):
            price = self._get_open_price(symbol, date, historical_data)
            if price is None or pos["avg_cost"] <= 0:
                continue

            margin = pos.get("margin", 0.0)
            if margin <= 0:
                continue

            # Unrealized P&L on the leveraged position
            price_change_pct = (price - pos["avg_cost"]) / pos["avg_cost"]
            unrealized_pnl = margin * price_change_pct * self.leverage

            # Maintenance margin = 80% of initial margin
            maintenance = margin * 0.8
            if unrealized_pnl < -maintenance:
                to_liquidate.append((symbol, pos, price))

        for symbol, pos, price in to_liquidate:
            margin_lost = pos.get("margin", 0.0)
            self._trade_log.append({
                "date": str(date.date()),
                "symbol": symbol,
                "side": "liquidation",
                "price": round(price, 6),
                "qty": round(pos["qty"], 8),
                "notional": round(pos["qty"] * price, 2),
                "commission": 0,
                "pnl": round(-margin_lost, 2),
                "leverage": self.leverage,
            })
            # Margin is already deducted from cash; it's lost
            del self._positions[symbol]
            self._liquidations += 1

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _calculate_metrics(self) -> dict:
        """Derive performance metrics from completed trades and daily values."""
        trades = self._trade_log
        daily = self._daily_values

        total_trades = len(trades)

        # P&L from closed (sell) trades
        sell_trades = [t for t in trades if t["side"] == "sell" and "pnl" in t]
        wins = [t for t in sell_trades if t["pnl"] > 0]
        losses = [t for t in sell_trades if t["pnl"] <= 0]

        win_rate = len(wins) / len(sell_trades) if sell_trades else 0.0
        total_pnl = sum(t["pnl"] for t in sell_trades)
        avg_trade_pnl = total_pnl / len(sell_trades) if sell_trades else 0.0

        # Sharpe ratio from daily portfolio returns
        sharpe_ratio = 0.0
        if len(daily) >= 2:
            values = pd.Series([d["value"] for d in daily], dtype=float)
            daily_returns = values.pct_change().dropna()
            if len(daily_returns) > 1 and daily_returns.std() > 0:
                # Annualized for crypto (365 trading days)
                sharpe_ratio = float(
                    daily_returns.mean() / daily_returns.std() * np.sqrt(365)
                )

        # Max drawdown from portfolio values
        max_drawdown = 0.0
        if len(daily) >= 2:
            values = pd.Series([d["value"] for d in daily], dtype=float)
            cummax = values.cummax()
            drawdowns = (values - cummax) / cummax
            max_drawdown = float(drawdowns.min())

        # Calmar ratio = annualized return / |max drawdown|
        calmar_ratio = 0.0
        if len(daily) >= 2 and max_drawdown < 0:
            total_days = len(daily)
            total_return_pct = (daily[-1]["value"] - daily[0]["value"]) / daily[0]["value"]
            ann_return = total_return_pct * (365 / total_days) if total_days > 0 else 0
            calmar_ratio = ann_return / abs(max_drawdown) if max_drawdown != 0 else 0

        closed_trades = [t for t in trades if t["side"] in ("sell", "liquidation") and "pnl" in t]
        liquidation_trades = [t for t in trades if t.get("side") == "liquidation"]

        return {
            "total_trades": total_trades,
            "closed_trades": len(sell_trades),
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "avg_trade_pnl": round(avg_trade_pnl, 2),
            "sharpe_ratio": round(sharpe_ratio, 4),
            "max_drawdown": round(max_drawdown, 4),
            "calmar_ratio": round(calmar_ratio, 4),
            "best_trade": round(max((t["pnl"] for t in sell_trades), default=0.0), 2),
            "worst_trade": round(min((t["pnl"] for t in sell_trades), default=0.0), 2),
            "leverage": self.leverage,
            "liquidations": self._liquidations,
            "liquidation_losses": round(sum(t["pnl"] for t in liquidation_trades), 2),
        }

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tz_compat(idx: pd.DatetimeIndex, ts: pd.Timestamp) -> pd.Timestamp:
        """Ensure *ts* matches the timezone of *idx* for safe comparison."""
        if idx.tz is not None and ts.tz is None:
            return ts.tz_localize(idx.tz)
        if idx.tz is None and ts.tz is not None:
            return ts.tz_localize(None)
        return ts

    def _get_open_price(
        self,
        symbol: str,
        date: pd.Timestamp,
        historical_data: dict,
    ) -> float | None:
        """Look up the open price for *symbol* on *date*.

        Falls back to close of previous day, then to any available price.
        """
        # Try OHLC data first (crypto coins use coin_id keys like "bitcoin")
        ohlc_data = historical_data.get("ohlc", {})
        for coin_id, df in ohlc_data.items():
            if self._symbol_matches_coin(symbol, coin_id):
                compat_date = self._tz_compat(df.index, date)
                mask = df.index.normalize() == compat_date.normalize()
                if mask.any():
                    return float(df.loc[mask, "open"].iloc[0])
                prior = df[df.index.normalize() <= compat_date.normalize()]
                if not prior.empty:
                    return float(prior["close"].iloc[-1])

        # Try ETF history (keyed by ticker symbol like "GLD")
        etf_data = historical_data.get("etf_history", {})
        etf_symbol = symbol.replace("/USD", "")  # "GLD" stays "GLD"
        for key, df in etf_data.items():
            if key.upper() == etf_symbol.upper() or key.upper() == symbol.upper():
                compat_date = self._tz_compat(df.index, date)
                mask = df.index.normalize() == compat_date.normalize()
                open_col = "Open" if "Open" in df.columns else "open"
                close_col = "Close" if "Close" in df.columns else "close"
                if mask.any() and open_col in df.columns:
                    return float(df.loc[mask, open_col].iloc[0])
                prior = df[df.index.normalize() <= compat_date.normalize()]
                if not prior.empty and close_col in df.columns:
                    return float(prior[close_col].iloc[-1])

        # Fallback: static prices dict
        prices = historical_data.get("prices", {})
        for coin_id, price_val in prices.items():
            if self._symbol_matches_coin(symbol, coin_id):
                if isinstance(price_val, dict):
                    return price_val.get("usd", price_val.get("price"))
                return float(price_val)

        return None

    @staticmethod
    def _symbol_matches_coin(symbol: str, coin_id: str) -> bool:
        """Check if an Alpaca-style symbol matches a CoinGecko coin_id."""
        mapping = {
            "bitcoin": ("BTC/USD", "BTC"),
            "ethereum": ("ETH/USD", "ETH"),
            "solana": ("SOL/USD", "SOL"),
            "cardano": ("ADA/USD", "ADA"),
            "avalanche-2": ("AVAX/USD", "AVAX"),
            "polkadot": ("DOT/USD", "DOT"),
            "chainlink": ("LINK/USD", "LINK"),
            "dogecoin": ("DOGE/USD", "DOGE"),
            "polygon": ("MATIC/USD", "MATIC"),
            "litecoin": ("LTC/USD", "LTC"),
        }
        known = mapping.get(coin_id.lower(), ())
        if symbol in known:
            return True
        # Fallback: check if coin_id is a substring of the symbol
        return coin_id.lower() in symbol.lower()

    def _record_daily_value(
        self,
        date: pd.Timestamp,
        historical_data: dict,
    ) -> None:
        """Snapshot current portfolio value at end of day.

        With leverage, portfolio value = cash + sum(margin + unrealized_pnl)
        where unrealized_pnl = margin * price_change_pct * leverage.
        """
        positions_value = 0.0
        for symbol, pos in self._positions.items():
            price = self._get_open_price(symbol, date, historical_data)
            if price is not None and pos["avg_cost"] > 0:
                margin = pos.get("margin", pos["qty"] * pos["avg_cost"] / self.leverage)
                price_change_pct = (price - pos["avg_cost"]) / pos["avg_cost"]
                unrealized_pnl = margin * price_change_pct * self.leverage
                positions_value += margin + unrealized_pnl
            elif price is not None:
                positions_value += pos["qty"] * price

        total = self._cash + positions_value
        self._daily_values.append(
            {
                "date": str(date.date()),
                "value": round(total, 2),
                "cash": round(self._cash, 2),
                "positions_value": round(positions_value, 2),
            }
        )

    # ------------------------------------------------------------------
    # Mock construction
    # ------------------------------------------------------------------

    def _build_patches(
        self,
        historical_data: dict,
        current_date: pd.Timestamp,
    ) -> dict[str, object]:
        """Return a mapping of dotted import paths to replacement callables.

        Each replacement slices *historical_data* so that the strategy only
        sees data up to and including *current_date*.
        """
        patches: dict[str, object] = {}

        def _compat(df: pd.DataFrame, ts: pd.Timestamp) -> pd.Timestamp:
            """Make *ts* tz-compatible with *df*'s index."""
            if df.index.tz is not None and ts.tz is None:
                return ts.tz_localize(df.index.tz)
            if df.index.tz is None and ts.tz is not None:
                return ts.tz_localize(None)
            return ts

        ohlc_data = historical_data.get("ohlc", {})
        historical_prices_data = historical_data.get("historical_prices", {})
        market_data_df = historical_data.get("market_data")
        prices_data = historical_data.get("prices", {})
        fg_list = historical_data.get("fear_greed", [])
        etf_data = historical_data.get("etf_history", {})
        fred_data = historical_data.get("fred_series", {})

        # Helper: add patch for data module AND all strategy modules that import it
        def _add_patch(func_name: str, data_module: str, mock_fn: object,
                       strategy_modules: list[str]) -> None:
            patches[f"{data_module}.{func_name}"] = mock_fn
            for smod in strategy_modules:
                patches[f"trading.strategy.{smod}.{func_name}"] = mock_fn

        # -- trading.data.crypto.get_ohlc -----------------------------------
        if ohlc_data:

            def mock_get_ohlc(coin_id: str, days: int = 30) -> pd.DataFrame:
                df = ohlc_data.get(coin_id, pd.DataFrame())
                if df.empty:
                    return df
                cd = _compat(df, current_date)
                sliced = df[df.index <= cd]
                # Live mode returns hourly bars (~180 candles per 30 days).
                # Backtest uses daily bars — serve 4x to compensate so
                # EMA(50), Bollinger(20), RSI(14) etc. have enough data.
                effective = max(days * 4, 120)
                if len(sliced) > effective:
                    sliced = sliced.tail(effective)
                return sliced

            _add_patch("get_ohlc", "trading.data.crypto", mock_get_ohlc,
                       ["rsi_divergence", "hmm_regime", "pairs_trading",
                        "kalman_trend", "cross_asset_momentum",
                        "regime_mean_reversion", "garch_volatility",
                        "breakout_detection", "factor_crypto"])

        # -- trading.data.crypto.get_historical_prices ----------------------
        if historical_prices_data:

            def mock_get_historical_prices(
                coin_id: str, days: int = 90
            ) -> pd.DataFrame:
                df = historical_prices_data.get(coin_id, pd.DataFrame())
                if df.empty:
                    return df
                cd = _compat(df, current_date)
                sliced = df[df.index <= cd]
                if days and len(sliced) > days:
                    sliced = sliced.tail(days)
                return sliced

            _add_patch("get_historical_prices", "trading.data.crypto",
                       mock_get_historical_prices, ["factor_crypto"])

        # -- trading.data.crypto.get_prices ---------------------------------
        if prices_data:

            def mock_get_prices(coin_ids: list[str] | None = None) -> dict:
                result = {}
                source = prices_data
                ids = coin_ids or list(source.keys())
                for cid in ids:
                    if cid in source:
                        val = source[cid]
                        if isinstance(val, (int, float)):
                            result[cid] = {
                                "usd": val,
                                "usd_24h_change": 0.0,
                                "usd_24h_vol": 0.0,
                            }
                        else:
                            result[cid] = val
                return result

            _add_patch("get_prices", "trading.data.crypto", mock_get_prices, [])

        # -- trading.data.crypto.get_market_data ----------------------------
        if market_data_df is not None:

            def mock_get_market_data(
                coin_ids: list[str] | None = None,
            ) -> pd.DataFrame:
                if isinstance(market_data_df, pd.DataFrame):
                    if coin_ids:
                        return market_data_df[
                            market_data_df["id"].isin(coin_ids)
                        ].copy()
                    return market_data_df.copy()
                return pd.DataFrame()

            _add_patch("get_market_data", "trading.data.crypto",
                       mock_get_market_data, [])

        # -- trading.data.sentiment.get_fear_greed --------------------------
        if fg_list:

            def mock_get_fear_greed(limit: int = 30) -> dict:
                # Filter to entries up to current_date
                cd_naive = current_date.tz_localize(None) if current_date.tz else current_date
                valid = []
                for entry in fg_list:
                    ts = entry.get("timestamp")
                    if ts is not None:
                        entry_date = pd.Timestamp(int(ts), unit="s")
                        if entry_date.normalize() <= cd_naive.normalize():
                            valid.append(entry)
                    else:
                        valid.append(entry)

                if not valid:
                    valid = fg_list[:1]  # At least return something

                # Sort descending by timestamp (newest first), same as live API
                valid = sorted(
                    valid,
                    key=lambda x: int(x.get("timestamp", 0)),
                    reverse=True,
                )
                valid = valid[:limit]

                current = {
                    "value": int(valid[0]["value"]),
                    "classification": valid[0].get(
                        "value_classification", "Neutral"
                    ),
                    "timestamp": valid[0].get("timestamp", ""),
                }

                history = pd.DataFrame(valid)
                history["value"] = history["value"].astype(int)
                if "timestamp" in history.columns:
                    history["timestamp"] = pd.to_datetime(
                        history["timestamp"].astype(int), unit="s"
                    )
                    history.set_index("timestamp", inplace=True)
                    history.sort_index(inplace=True)

                return {
                    "current": current,
                    "history": history[
                        [c for c in ("value", "value_classification") if c in history.columns]
                    ],
                }

            _add_patch("get_fear_greed", "trading.data.sentiment",
                       mock_get_fear_greed, [])

        # -- trading.data.commodities.get_etf_history -----------------------
        if etf_data:

            def mock_get_etf_history(
                symbol: str, period: str = "3mo"
            ) -> pd.DataFrame:
                df = etf_data.get(symbol, pd.DataFrame())
                if df.empty:
                    return df
                cd = _compat(df, current_date)
                sliced = df[df.index <= cd]
                return sliced

            _add_patch("get_etf_history", "trading.data.commodities",
                       mock_get_etf_history, ["dxy_dollar", "cross_asset_momentum"])

        # -- trading.data.commodities.get_fred_series -----------------------
        if fred_data:

            def mock_get_fred_series(
                series_id: str, limit: int = 90
            ) -> pd.DataFrame:
                df = fred_data.get(series_id, pd.DataFrame())
                if df.empty:
                    return df
                cd = _compat(df, current_date)
                sliced = df[df.index <= cd]
                if limit and len(sliced) > limit:
                    sliced = sliced.tail(limit)
                return sliced

            _add_patch("get_fred_series", "trading.data.commodities",
                       mock_get_fred_series, [])

        # -- trading.data.aster (AsterDex derivatives data) ------------------
        # Derive realistic derivatives data from OHLC price action so that
        # funding rates, basis, order books, and volume correlate with actual
        # market movements.  This replaces the old random-noise mocks.
        from trading.config import ASTER_SYMBOLS

        # Reverse map: BTCUSDT -> bitcoin
        _aster_to_coin = {v: k for k, v in ASTER_SYMBOLS.items()}

        def _price_momentum(coin_id: str, lookback: int = 5) -> float:
            """Return recent price momentum (-1..+1) from OHLC data."""
            df = ohlc_data.get(coin_id, pd.DataFrame())
            if df.empty:
                return 0.0
            cd = _compat(df, current_date)
            sliced = df[df.index <= cd].tail(lookback + 1)
            if len(sliced) < 2:
                return 0.0
            ret = (sliced["close"].iloc[-1] - sliced["close"].iloc[0]) / sliced["close"].iloc[0]
            return float(np.clip(ret * 10, -1, 1))  # Scale so ±10% maps to ±1

        def _current_price(coin_id: str) -> float:
            """Return the latest close price from OHLC."""
            df = ohlc_data.get(coin_id, pd.DataFrame())
            if df.empty:
                return 0.0
            cd = _compat(df, current_date)
            sliced = df[df.index <= cd]
            return float(sliced["close"].iloc[-1]) if not sliced.empty else 0.0

        def _volatility(coin_id: str, lookback: int = 20) -> float:
            """Return recent realized volatility (annualized std of returns)."""
            df = ohlc_data.get(coin_id, pd.DataFrame())
            if df.empty:
                return 0.02
            cd = _compat(df, current_date)
            sliced = df[df.index <= cd].tail(lookback + 1)
            if len(sliced) < 3:
                return 0.02
            rets = sliced["close"].pct_change().dropna()
            return float(rets.std()) if len(rets) > 0 else 0.02

        def _coin_for_symbol(symbol: str) -> str:
            """Map AsterDex symbol to coin_id."""
            return _aster_to_coin.get(symbol, "bitcoin")

        # --- Funding rates: dict[str, float] (coin_id -> rate) ---
        def mock_get_funding_rates(symbols=None):
            from trading.config import ASTER_SYMBOLS as _AS
            target_coins = list(_AS.keys())[:6]  # Top 6
            result = {}
            for coin_id in target_coins:
                mom = _price_momentum(coin_id, lookback=3)
                # Funding positive in uptrends (longs pay shorts), negative in downtrends
                rate = mom * 0.0005 + np.random.normal(0, 0.0001)
                result[coin_id] = float(np.clip(rate, -0.003, 0.003))
            return result

        # --- Funding rate history: list[float] ---
        def mock_get_funding_rate_history(symbol, limit=100):
            coin_id = _coin_for_symbol(symbol)
            df = ohlc_data.get(coin_id, pd.DataFrame())
            if df.empty:
                return [np.random.normal(0.0001, 0.0003) for _ in range(limit)]
            cd = _compat(df, current_date)
            sliced = df[df.index <= cd].tail(limit + 1)
            if len(sliced) < 3:
                return [np.random.normal(0.0001, 0.0003) for _ in range(limit)]
            rets = sliced["close"].pct_change().dropna()
            # Funding tracks momentum: positive returns → positive funding
            rates = []
            for r in rets:
                rate = float(r * 0.05 + np.random.normal(0, 0.0002))
                rates.append(float(np.clip(rate, -0.005, 0.005)))
            # Pad to requested limit
            while len(rates) < limit:
                rates.insert(0, float(np.random.normal(0.0001, 0.0003)))
            return rates[-limit:]

        # --- Order book imbalance: dict ---
        def mock_get_orderbook_imbalance(symbol, depth=20):
            coin_id = _coin_for_symbol(symbol)
            price = _current_price(coin_id)
            if price <= 0:
                price = 50000.0 if "BTC" in symbol else 3000.0
            mom = _price_momentum(coin_id, lookback=3)
            # Bid-heavy in uptrends, ask-heavy in downtrends
            base_bid = 100 + mom * 40
            base_ask = 100 - mom * 40
            bid_vol = max(10, base_bid + np.random.normal(0, 15))
            ask_vol = max(10, base_ask + np.random.normal(0, 15))
            total = bid_vol + ask_vol
            return {
                "bid_volume": round(bid_vol, 4),
                "ask_volume": round(ask_vol, 4),
                "imbalance": round((bid_vol - ask_vol) / total, 4),
                "spread_bps": round(abs(np.random.normal(3, 2)), 2),
                "mid_price": round(price, 2),
            }

        # --- Basis spread: dict or list[dict] ---
        def mock_get_basis_spread(symbol=None):
            def _single(s):
                coin_id = _coin_for_symbol(s)
                price = _current_price(coin_id)
                if price <= 0:
                    price = 50000.0 if "BTC" in s else 3000.0
                mom = _price_momentum(coin_id, lookback=5)
                # Contango in uptrends, backwardation in downtrends
                basis_pct = mom * 0.15 + np.random.normal(0, 0.05)
                mark = price * (1 + basis_pct / 100)
                rate = mom * 0.0005 + np.random.normal(0, 0.0001)
                return {
                    "symbol": s,
                    "markPrice": round(mark, 2),
                    "indexPrice": round(price, 2),
                    "basis_pct": round(basis_pct, 4),
                    "fundingRate": round(float(np.clip(rate, -0.003, 0.003)), 6),
                }
            if symbol:
                return _single(symbol)
            tracked = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT"]
            return [_single(s) for s in tracked]

        # --- Taker volume ratio: dict ---
        def mock_get_taker_volume_ratio(symbol, interval="1h", limit=24):
            coin_id = _coin_for_symbol(symbol)
            mom = _price_momentum(coin_id, lookback=3)
            # Buy-heavy in uptrends, sell-heavy in downtrends
            buy_ratio = 0.5 + mom * 0.12 + np.random.normal(0, 0.03)
            buy_ratio = float(np.clip(buy_ratio, 0.25, 0.75))
            return {
                "symbol": symbol,
                "buy_ratio": round(buy_ratio, 4),
                "sell_ratio": round(1 - buy_ratio, 4),
                "net_ratio": round(buy_ratio * 2 - 1, 4),
                "periods": limit,
            }

        # --- AsterDex OHLCV: pd.DataFrame ---
        def mock_get_aster_ohlcv(symbol, interval="1h", limit=500):
            coin_id = _coin_for_symbol(symbol)
            df = ohlc_data.get(coin_id, pd.DataFrame())
            if df.empty:
                return pd.DataFrame()
            cd = _compat(df, current_date)
            sliced = df[df.index <= cd].tail(limit)
            if sliced.empty:
                return pd.DataFrame()
            # Add taker buy volume (correlated with price direction)
            result = sliced.copy()
            if "volume" not in result.columns:
                result["volume"] = 1000.0
            rets = result["close"].pct_change().fillna(0)
            # Taker buy ratio higher when price goes up
            taker_ratio = 0.5 + rets.clip(-0.1, 0.1) * 3
            result["taker_buy_base_vol"] = result["volume"] * taker_ratio
            if "trades" not in result.columns:
                result["trades"] = 100
            return result

        # --- AsterDex klines (from aster_client): pd.DataFrame ---
        def mock_get_aster_klines(symbol, interval="1d", limit=500):
            return mock_get_aster_ohlcv(symbol, interval=interval, limit=limit)

        # --- AsterDex order book (from aster_client): dict ---
        def mock_get_aster_orderbook(symbol, limit=50):
            coin_id = _coin_for_symbol(symbol)
            price = _current_price(coin_id)
            if price <= 0:
                price = 50000.0 if "BTC" in symbol else 3000.0
            mom = _price_momentum(coin_id, lookback=3)
            spread = price * 0.0002  # 2 bps spread
            bids = []
            asks = []
            for i in range(min(limit, 50)):
                bid_p = price - spread * (i + 1)
                ask_p = price + spread * (i + 1)
                # Bid volume higher in uptrends (support), diminishing with depth
                bid_q = max(0.001, (1.0 - i * 0.015) * (1 + mom * 0.3) + np.random.normal(0, 0.1))
                ask_q = max(0.001, (1.0 - i * 0.015) * (1 - mom * 0.3) + np.random.normal(0, 0.1))
                bids.append((round(bid_p, 2), round(bid_q, 4)))
                asks.append((round(ask_p, 2), round(ask_q, 4)))
            return {"bids": bids, "asks": asks}

        # --- AsterDex open interest (from aster_client): dict ---
        def mock_get_aster_open_interest(symbol):
            coin_id = _coin_for_symbol(symbol)
            vol = _volatility(coin_id)
            price = _current_price(coin_id)
            # OI higher with higher volatility and price
            base_oi = price * 1000 * (1 + vol * 10)
            return {"openInterest": round(base_oi + np.random.normal(0, base_oi * 0.05), 2),
                    "symbol": symbol}

        # --- AsterDex mark prices (from aster_client): list or dict ---
        def mock_get_aster_mark_prices(symbol=None):
            if symbol:
                coin_id = _coin_for_symbol(symbol)
                price = _current_price(coin_id)
                if price <= 0:
                    price = 50000.0 if "BTC" in symbol else 3000.0
                mom = _price_momentum(coin_id, lookback=5)
                basis = mom * 0.15 / 100
                return {
                    "symbol": symbol,
                    "markPrice": round(price * (1 + basis), 2),
                    "indexPrice": round(price, 2),
                    "lastFundingRate": round(float(mom * 0.0005), 6),
                    "nextFundingTime": int(current_date.timestamp() * 1000) + 28800000,
                }
            # All symbols
            tracked = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT"]
            results = []
            for s in tracked:
                results.append(mock_get_aster_mark_prices(s))
            return results

        # --- Market summary ---
        def mock_get_aster_market_summary():
            btc_mom = _price_momentum("bitcoin", lookback=5)
            return {
                "funding_sentiment": round(btc_mom * 0.0005, 6),
                "orderbook_pressure": round(btc_mom * 0.2, 4),
                "basis_regime": "contango" if btc_mom > 0 else "backwardation",
                "volume_flow": round(btc_mom * 0.3, 4),
            }

        # All strategy modules that import from trading.data.aster or aster_client
        _aster_strategies = [
            "funding_arb", "liquidation_cascade", "basis_zscore",
            "funding_term_structure", "taker_divergence", "cross_basis_rv",
            "oi_price_divergence", "whale_flow", "cross_asset_momentum",
            "multi_factor_rank", "meme_momentum", "volatility_regime",
            "equity_crypto_correlation", "gold_crypto_hedge",
        ]

        # Patch trading.data.aster functions
        _add_patch("get_funding_rates", "trading.data.aster",
                   mock_get_funding_rates, _aster_strategies)
        _add_patch("get_funding_rate_history", "trading.data.aster",
                   mock_get_funding_rate_history, _aster_strategies)
        _add_patch("get_orderbook_imbalance", "trading.data.aster",
                   mock_get_orderbook_imbalance, _aster_strategies)
        _add_patch("get_basis_spread", "trading.data.aster",
                   mock_get_basis_spread, _aster_strategies)
        _add_patch("get_taker_volume_ratio", "trading.data.aster",
                   mock_get_taker_volume_ratio, _aster_strategies)
        _add_patch("get_aster_ohlcv", "trading.data.aster",
                   mock_get_aster_ohlcv, _aster_strategies)
        _add_patch("get_aster_market_summary", "trading.data.aster",
                   mock_get_aster_market_summary, _aster_strategies)
        _add_patch("get_open_interest", "trading.data.aster",
                   lambda s: mock_get_aster_open_interest(s).get("openInterest"),
                   _aster_strategies)

        # Patch trading.execution.aster_client functions (some strategies import directly)
        _add_patch("get_aster_klines", "trading.execution.aster_client",
                   mock_get_aster_klines, _aster_strategies)
        _add_patch("get_aster_orderbook", "trading.execution.aster_client",
                   mock_get_aster_orderbook, _aster_strategies)
        _add_patch("get_aster_open_interest", "trading.execution.aster_client",
                   mock_get_aster_open_interest, _aster_strategies)
        _add_patch("get_aster_mark_prices", "trading.execution.aster_client",
                   mock_get_aster_mark_prices, _aster_strategies)

        # _public_get is used by oi_price_divergence to fetch OI directly
        def mock_public_get(endpoint, params=None):
            params = params or {}
            if "openInterest" in endpoint:
                symbol = params.get("symbol", "BTCUSDT")
                return mock_get_aster_open_interest(symbol)
            if "klines" in endpoint:
                symbol = params.get("symbol", "BTCUSDT")
                return []  # klines go through get_aster_klines
            return {}

        _add_patch("_public_get", "trading.execution.aster_client",
                   mock_public_get, _aster_strategies)

        return patches


# ---------------------------------------------------------------------------
# Patch context manager
# ---------------------------------------------------------------------------


class _apply_patches:
    """Context manager that applies multiple ``unittest.mock.patch`` objects.

    Patches that target non-existent attributes (e.g. lazy imports inside
    functions) are silently skipped so they don't crash the entire patching
    context for all strategies.
    """

    def __init__(self, targets: dict[str, object]) -> None:
        self._patchers = []
        for target, replacement in targets.items():
            try:
                p = patch(target, replacement)
                self._patchers.append(p)
            except AttributeError:
                # Target module doesn't have this attribute at module level
                # (strategy uses lazy import inside a function) — skip it.
                pass

    def __enter__(self) -> None:
        started = []
        for p in self._patchers:
            try:
                p.start()
                started.append(p)
            except AttributeError:
                pass
        self._patchers = started

    def __exit__(self, *exc: object) -> None:
        for p in self._patchers:
            p.stop()


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------


def _fetch_historical_data(
    strategy_name: str,
    days: int,
) -> dict:
    """Fetch all historical data a strategy might need.

    This calls the *real* live data functions once, upfront, to build the
    full dataset that will later be sliced per-day during the simulation.
    """
    data: dict = {}
    console.print(f"[bold]Fetching historical data for [cyan]{strategy_name}[/] ({days} days)...[/]")

    # -- Crypto OHLC --------------------------------------------------------
    # Determine which coins to fetch based on strategy config
    from trading.config import CRYPTO_SYMBOLS

    default_coins = list(CRYPTO_SYMBOLS.keys())

    try:
        ohlc = {}
        for coin_id in default_coins:
            try:
                ohlc[coin_id] = get_ohlc(coin_id, days)
                console.print(f"  OHLC {coin_id}: {len(ohlc[coin_id])} candles")
            except Exception as exc:
                logger.debug("Could not fetch OHLC for %s: %s", coin_id, exc)
        data["ohlc"] = ohlc
    except Exception as exc:
        logger.warning("OHLC fetch failed: %s", exc)

    # -- Crypto historical prices -------------------------------------------
    try:
        hp = {}
        for coin_id in default_coins:
            try:
                hp[coin_id] = get_historical_prices(coin_id, days)
                console.print(f"  Historical prices {coin_id}: {len(hp[coin_id])} rows")
            except Exception as exc:
                logger.debug("Could not fetch historical prices for %s: %s", coin_id, exc)
        data["historical_prices"] = hp
    except Exception as exc:
        logger.warning("Historical prices fetch failed: %s", exc)

    # -- Current prices snapshot --------------------------------------------
    try:
        data["prices"] = get_prices(default_coins)
        console.print(f"  Prices: {len(data['prices'])} coins")
    except Exception as exc:
        logger.warning("Prices fetch failed: %s", exc)

    # -- Market data (for momentum) -----------------------------------------
    try:
        data["market_data"] = get_market_data(default_coins)
        console.print(f"  Market data: {len(data['market_data'])} rows")
    except Exception as exc:
        logger.warning("Market data fetch failed: %s", exc)

    # -- Fear & Greed -------------------------------------------------------
    try:
        fg = get_fear_greed(limit=min(days, 1000))
        # Convert to list-of-dicts format the engine expects
        history = fg.get("history")
        if history is not None and not history.empty:
            fg_records = []
            for ts, row in history.iterrows():
                fg_records.append(
                    {
                        "value": str(row["value"]),
                        "value_classification": row.get("value_classification", ""),
                        "timestamp": str(int(ts.timestamp())),
                    }
                )
            data["fear_greed"] = fg_records
            console.print(f"  Fear & Greed: {len(fg_records)} days")
    except Exception as exc:
        logger.warning("Fear & Greed fetch failed: %s", exc)

    # -- ETF history --------------------------------------------------------
    try:
        # Include leveraged ETFs (UGL, AGQ) and DXY index used by strategies
        etf_symbols = ["GLD", "SLV", "USO", "UNG", "UGL", "AGQ", "DX-Y.NYB"]
        if days <= 180:
            period = "6mo"
        elif days <= 365:
            period = "1y"
        else:
            period = "2y"
        etfs = {}
        for sym in etf_symbols:
            try:
                etfs[sym] = get_etf_history(sym, period=period)
                console.print(f"  ETF {sym}: {len(etfs[sym])} rows")
            except Exception as exc:
                logger.debug("Could not fetch ETF %s: %s", sym, exc)
        data["etf_history"] = etfs
    except Exception as exc:
        logger.warning("ETF history fetch failed: %s", exc)

    # -- FRED series --------------------------------------------------------
    try:
        fred_ids = ["DGS10", "DFII10"]  # 10y Treasury, 10y TIPS
        fred = {}
        for sid in fred_ids:
            try:
                fred[sid] = get_fred_series(sid, limit=min(days + 60, 1000))
                console.print(f"  FRED {sid}: {len(fred[sid])} rows")
            except Exception as exc:
                logger.debug("Could not fetch FRED %s: %s", sid, exc)
        data["fred_series"] = fred
    except Exception as exc:
        logger.warning("FRED series fetch failed: %s", exc)

    return data


def run_backtest(
    strategy_name: str,
    days: int = 90,
    starting_capital: float = 100_000,
    leverage: int = 1,
) -> BacktestResult:
    """High-level convenience function: fetch data, run backtest, print report.

    Parameters
    ----------
    strategy_name:
        Name of a registered strategy (e.g. ``"ema_crossover"``).
    days:
        How many days of history to simulate over.
    starting_capital:
        Initial cash balance.
    leverage:
        Leverage multiplier (1x = no leverage).

    Returns
    -------
    BacktestResult
    """
    historical_data = _fetch_historical_data(strategy_name, days)

    end_date = datetime.utcnow().date() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    backtester = Backtester(
        starting_capital=starting_capital,
        commission_pct=0.001,
        leverage=leverage,
    )

    console.print(
        f"\n[bold]Running backtest:[/] {strategy_name} "
        f"from {start_date} to {end_date} "
        f"(capital: ${starting_capital:,.0f}, leverage: {leverage}x)\n"
    )

    result = backtester.run(
        strategy_name=strategy_name,
        historical_data=historical_data,
        start_date=str(start_date),
        end_date=str(end_date),
    )

    print_backtest_report(result)
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_backtest_report(result: BacktestResult) -> None:
    """Print a formatted backtest report using rich tables."""
    m = result.metrics

    # -- Summary table -------------------------------------------------------
    summary = Table(title=f"Backtest Report: {result.strategy_name}", show_lines=True)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")

    ending_capital = (
        result.portfolio_values[-1]["value"] if result.portfolio_values else result.starting_capital
    )
    total_return_pct = (
        (ending_capital - result.starting_capital) / result.starting_capital * 100
        if result.starting_capital
        else 0.0
    )

    lev = result.metrics.get("leverage", 1)
    summary.add_row("Date Range", f"{result.start_date} to {result.end_date}")
    summary.add_row("Leverage", f"{lev}x")
    summary.add_row("Starting Capital", f"${result.starting_capital:,.2f}")
    summary.add_row("Ending Capital", f"${ending_capital:,.2f}")
    summary.add_row(
        "Total Return",
        f"${m.get('total_pnl', 0):+,.2f} ({total_return_pct:+.2f}%)",
    )
    summary.add_row("Total Trades", str(m.get("total_trades", 0)))
    summary.add_row("Closed Trades", str(m.get("closed_trades", 0)))
    summary.add_row("Win Rate", f"{m.get('win_rate', 0) * 100:.1f}%")
    summary.add_row("Avg Trade P&L", f"${m.get('avg_trade_pnl', 0):+,.2f}")
    summary.add_row("Best Trade", f"${m.get('best_trade', 0):+,.2f}")
    summary.add_row("Worst Trade", f"${m.get('worst_trade', 0):+,.2f}")
    summary.add_row("Sharpe Ratio", f"{m.get('sharpe_ratio', 0):.4f}")
    summary.add_row("Calmar Ratio", f"{m.get('calmar_ratio', 0):.4f}")
    summary.add_row("Max Drawdown", f"{m.get('max_drawdown', 0) * 100:.2f}%")
    if m.get("liquidations", 0) > 0:
        summary.add_row("Liquidations", f"[red]{m['liquidations']}[/red]")
        summary.add_row("Liquidation Losses", f"[red]${m.get('liquidation_losses', 0):+,.2f}[/red]")

    console.print(summary)

    # -- Top 5 best trades ---------------------------------------------------
    sell_trades = [t for t in result.trades if t.get("side") == "sell" and "pnl" in t]

    if sell_trades:
        best = sorted(sell_trades, key=lambda t: t["pnl"], reverse=True)[:5]
        best_table = Table(title="Top 5 Best Trades", show_lines=True)
        best_table.add_column("Date")
        best_table.add_column("Symbol")
        best_table.add_column("Price", justify="right")
        best_table.add_column("Qty", justify="right")
        best_table.add_column("P&L", justify="right", style="green")

        for t in best:
            best_table.add_row(
                t.get("date", ""),
                t["symbol"],
                f"${t['price']:,.2f}",
                f"{t['qty']:.6f}",
                f"${t['pnl']:+,.2f}",
            )
        console.print(best_table)

        # -- Top 5 worst trades ----------------------------------------------
        worst = sorted(sell_trades, key=lambda t: t["pnl"])[:5]
        worst_table = Table(title="Top 5 Worst Trades", show_lines=True)
        worst_table.add_column("Date")
        worst_table.add_column("Symbol")
        worst_table.add_column("Price", justify="right")
        worst_table.add_column("Qty", justify="right")
        worst_table.add_column("P&L", justify="right", style="red")

        for t in worst:
            worst_table.add_row(
                t.get("date", ""),
                t["symbol"],
                f"${t['price']:,.2f}",
                f"{t['qty']:.6f}",
                f"${t['pnl']:+,.2f}",
            )
        console.print(worst_table)

    # -- Monthly returns breakdown -------------------------------------------
    if result.portfolio_values:
        df = pd.DataFrame(result.portfolio_values)
        df["date"] = pd.to_datetime(df["date"])
        df["month"] = df["date"].dt.to_period("M")

        monthly_table = Table(title="Monthly Returns", show_lines=True)
        monthly_table.add_column("Month")
        monthly_table.add_column("Start Value", justify="right")
        monthly_table.add_column("End Value", justify="right")
        monthly_table.add_column("Return", justify="right")

        for month, group in df.groupby("month"):
            start_val = group["value"].iloc[0]
            end_val = group["value"].iloc[-1]
            ret_pct = (end_val - start_val) / start_val * 100 if start_val else 0.0
            color = "green" if ret_pct >= 0 else "red"
            monthly_table.add_row(
                str(month),
                f"${start_val:,.2f}",
                f"${end_val:,.2f}",
                f"[{color}]{ret_pct:+.2f}%[/{color}]",
            )

        console.print(monthly_table)

    # -- Signal summary ------------------------------------------------------
    if result.signals:
        signal_df = pd.DataFrame(result.signals)
        counts = signal_df["action"].value_counts()
        sig_table = Table(title="Signal Summary", show_lines=True)
        sig_table.add_column("Action")
        sig_table.add_column("Count", justify="right")
        for action, count in counts.items():
            sig_table.add_row(str(action), str(count))
        sig_table.add_row("[bold]Total[/]", f"[bold]{len(result.signals)}[/]")
        console.print(sig_table)
