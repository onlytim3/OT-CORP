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
    """

    def __init__(
        self,
        starting_capital: float = 100_000,
        commission_pct: float = 0.001,
    ) -> None:
        self.starting_capital = starting_capital
        self.commission_pct = commission_pct

        # Mutable state -- reset on each run
        self._cash: float = 0.0
        self._positions: dict[str, dict] = {}  # symbol -> {qty, avg_cost}
        self._trade_log: list[dict] = []
        self._daily_values: list[dict] = []
        self._all_signals: list[dict] = []

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

        dt_start = pd.Timestamp(start_date)
        dt_end = pd.Timestamp(end_date)
        date_range = pd.date_range(dt_start, dt_end, freq="D")

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

            # 5. Snapshot portfolio value at end of day
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
        }

        if side == "buy":
            if notional > self._cash:
                # Not enough cash -- scale down
                notional = self._cash
                commission = notional * self.commission_pct
                qty = (notional - commission) / price if price > 0 else 0.0
                if qty <= 0:
                    return None
                trade["notional"] = round(notional, 2)
                trade["qty"] = round(qty, 8)
                trade["commission"] = round(commission, 2)

            self._cash -= notional
            pos = self._positions.setdefault(symbol, {"qty": 0.0, "avg_cost": 0.0})
            total_cost = pos["avg_cost"] * pos["qty"] + price * qty
            pos["qty"] += qty
            pos["avg_cost"] = total_cost / pos["qty"] if pos["qty"] else 0.0

        elif side == "sell":
            pos = self._positions.get(symbol)
            if pos is None or pos["qty"] <= 0:
                return None  # Nothing to sell

            sell_qty = min(qty, pos["qty"])
            proceeds = sell_qty * price
            commission = proceeds * self.commission_pct
            self._cash += proceeds - commission

            pnl = (price - pos["avg_cost"]) * sell_qty
            trade["qty"] = round(sell_qty, 8)
            trade["notional"] = round(proceeds, 2)
            trade["commission"] = round(commission, 2)
            trade["pnl"] = round(pnl, 2)

            pos["qty"] -= sell_qty
            if pos["qty"] <= 1e-12:
                del self._positions[symbol]

        return trade

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

        return {
            "total_trades": total_trades,
            "closed_trades": len(sell_trades),
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "avg_trade_pnl": round(avg_trade_pnl, 2),
            "sharpe_ratio": round(sharpe_ratio, 4),
            "max_drawdown": round(max_drawdown, 4),
            "best_trade": round(max((t["pnl"] for t in sell_trades), default=0.0), 2),
            "worst_trade": round(min((t["pnl"] for t in sell_trades), default=0.0), 2),
        }

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

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
            # Match symbol heuristically -- strategies use Alpaca symbols like
            # "BTC/USD" but data is keyed by CoinGecko ids like "bitcoin".
            if self._symbol_matches_coin(symbol, coin_id):
                mask = df.index.normalize() == date.normalize()
                if mask.any():
                    return float(df.loc[mask, "open"].iloc[0])
                # Fall back to closest prior day
                prior = df[df.index.normalize() <= date.normalize()]
                if not prior.empty:
                    return float(prior["close"].iloc[-1])

        # Try ETF history (keyed by ticker symbol like "GLD")
        etf_data = historical_data.get("etf_history", {})
        etf_symbol = symbol.replace("/USD", "")  # "GLD" stays "GLD"
        for key, df in etf_data.items():
            if key.upper() == etf_symbol.upper() or key.upper() == symbol.upper():
                mask = df.index.normalize() == date.normalize()
                open_col = "Open" if "Open" in df.columns else "open"
                close_col = "Close" if "Close" in df.columns else "close"
                if mask.any() and open_col in df.columns:
                    return float(df.loc[mask, open_col].iloc[0])
                prior = df[df.index.normalize() <= date.normalize()]
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
        """Snapshot current portfolio value at end of day."""
        positions_value = 0.0
        for symbol, pos in self._positions.items():
            price = self._get_open_price(symbol, date, historical_data)
            if price is not None:
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

        ohlc_data = historical_data.get("ohlc", {})
        historical_prices_data = historical_data.get("historical_prices", {})
        market_data_df = historical_data.get("market_data")
        prices_data = historical_data.get("prices", {})
        fg_list = historical_data.get("fear_greed", [])
        etf_data = historical_data.get("etf_history", {})
        fred_data = historical_data.get("fred_series", {})

        # -- trading.data.crypto.get_ohlc -----------------------------------
        if ohlc_data:

            def mock_get_ohlc(coin_id: str, days: int = 30) -> pd.DataFrame:
                df = ohlc_data.get(coin_id, pd.DataFrame())
                if df.empty:
                    return df
                sliced = df[df.index <= current_date]
                if days and len(sliced) > days:
                    sliced = sliced.tail(days)
                return sliced

            patches["trading.data.crypto.get_ohlc"] = mock_get_ohlc

        # -- trading.data.crypto.get_historical_prices ----------------------
        if historical_prices_data:

            def mock_get_historical_prices(
                coin_id: str, days: int = 90
            ) -> pd.DataFrame:
                df = historical_prices_data.get(coin_id, pd.DataFrame())
                if df.empty:
                    return df
                sliced = df[df.index <= current_date]
                if days and len(sliced) > days:
                    sliced = sliced.tail(days)
                return sliced

            patches["trading.data.crypto.get_historical_prices"] = (
                mock_get_historical_prices
            )

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

            patches["trading.data.crypto.get_prices"] = mock_get_prices

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

            patches["trading.data.crypto.get_market_data"] = mock_get_market_data

        # -- trading.data.sentiment.get_fear_greed --------------------------
        if fg_list:

            def mock_get_fear_greed(limit: int = 30) -> dict:
                # Filter to entries up to current_date
                valid = []
                for entry in fg_list:
                    ts = entry.get("timestamp")
                    if ts is not None:
                        entry_date = pd.Timestamp(int(ts), unit="s")
                        if entry_date.normalize() <= current_date.normalize():
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

            patches["trading.data.sentiment.get_fear_greed"] = mock_get_fear_greed

        # -- trading.data.commodities.get_etf_history -----------------------
        if etf_data:

            def mock_get_etf_history(
                symbol: str, period: str = "3mo"
            ) -> pd.DataFrame:
                df = etf_data.get(symbol, pd.DataFrame())
                if df.empty:
                    return df
                sliced = df[df.index <= current_date]
                return sliced

            patches["trading.data.commodities.get_etf_history"] = (
                mock_get_etf_history
            )

        # -- trading.data.commodities.get_fred_series -----------------------
        if fred_data:

            def mock_get_fred_series(
                series_id: str, limit: int = 90
            ) -> pd.DataFrame:
                df = fred_data.get(series_id, pd.DataFrame())
                if df.empty:
                    return df
                sliced = df[df.index <= current_date]
                if limit and len(sliced) > limit:
                    sliced = sliced.tail(limit)
                return sliced

            patches["trading.data.commodities.get_fred_series"] = (
                mock_get_fred_series
            )

        return patches


# ---------------------------------------------------------------------------
# Patch context manager
# ---------------------------------------------------------------------------


class _apply_patches:
    """Context manager that applies multiple ``unittest.mock.patch`` objects."""

    def __init__(self, targets: dict[str, object]) -> None:
        self._patchers = []
        for target, replacement in targets.items():
            self._patchers.append(patch(target, replacement))

    def __enter__(self) -> None:
        for p in self._patchers:
            p.start()

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
    from trading.config import MOMENTUM

    default_coins = MOMENTUM.get("coins", ["bitcoin", "ethereum"])

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
        fg = get_fear_greed(limit=min(days, 365))
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
        etf_symbols = ["GLD", "SLV", "USO", "UNG"]
        period = "6mo" if days <= 180 else "1y"
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
                fred[sid] = get_fred_series(sid, limit=days)
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
    )

    console.print(
        f"\n[bold]Running backtest:[/] {strategy_name} "
        f"from {start_date} to {end_date} "
        f"(capital: ${starting_capital:,.0f})\n"
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

    summary.add_row("Date Range", f"{result.start_date} to {result.end_date}")
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
    summary.add_row("Max Drawdown", f"{m.get('max_drawdown', 0) * 100:.2f}%")

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
