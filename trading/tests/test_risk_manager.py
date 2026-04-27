"""Tests for trading.risk.manager — risk checks, position limits, leverage awareness."""

import unittest
from unittest.mock import patch

from trading.risk.manager import RiskManager, LEVERAGE_FACTORS
from trading.strategy.base import Signal


def _make_signal(symbol="BTC/USD", action="buy", strength=0.8):
    return Signal(strategy="test", symbol=symbol, action=action, strength=strength, reason="test")



class TestAccountChecks(unittest.TestCase):
    """Test account-level safety checks."""

    def test_blocked_account_rejects(self):
        rm = RiskManager(100000, account={"trading_blocked": True, "status": "ACTIVE"})
        with patch("trading.risk.manager.get_positions", return_value=[]):
            result = rm.check_trade(_make_signal(), 5000)
        self.assertFalse(result.allowed)
        self.assertIn("blocked", result.reason.lower())

    def test_inactive_account_rejects(self):
        rm = RiskManager(100000, account={"trading_blocked": False, "status": "SUSPENDED"})
        with patch("trading.risk.manager.get_positions", return_value=[]):
            result = rm.check_trade(_make_signal(), 5000)
        self.assertFalse(result.allowed)

    def test_active_account_passes(self):
        rm = RiskManager(100000, account={
            "trading_blocked": False, "status": "ACTIVE",
            "buying_power": 50000, "cash": 50000,
        })
        with patch("trading.risk.manager.get_positions", return_value=[]), \
             patch("trading.risk.manager.get_daily_pnl", return_value=[]), \
             patch("trading.risk.manager.get_trades", return_value=[]), \
             patch("trading.execution.router.get_positions_from_bybit", return_value=[]), \
             patch("trading.risk.manager.compute_volume_ratio", return_value=1.0), \
             patch("trading.risk.manager.compute_volume_trend", return_value=0.0), \
             patch("trading.risk.manager.check_spread", return_value=1.0), \
             patch("trading.risk.manager.check_market_impact", return_value=True):
            result = rm.check_trade(_make_signal("BTC/USD", "buy"), 5000)
        self.assertTrue(result.allowed)


class TestBuyingPower(unittest.TestCase):
    def test_exceeds_buying_power(self):
        rm = RiskManager(100000, account={
            "trading_blocked": False, "status": "ACTIVE",
            "buying_power": 3000,
        })
        with patch("trading.risk.manager.get_positions", return_value=[]):
            result = rm.check_trade(_make_signal(), 5000)
        self.assertFalse(result.allowed)
        self.assertIn("buying power", result.reason.lower())


class TestPositionSize(unittest.TestCase):
    def test_large_position_blocked(self):
        rm = RiskManager(100000, account={
            "trading_blocked": False, "status": "ACTIVE",
            "buying_power": 100000, "cash": 50000,
        })
        # Try to buy 40% of portfolio in BTC (max is 25%)
        with patch("trading.risk.manager.get_positions", return_value=[]), \
             patch("trading.risk.manager.get_daily_pnl", return_value=[]), \
             patch("trading.risk.manager.get_trades", return_value=[]), \
             patch("trading.risk.manager.compute_volume_ratio", return_value=1.0), \
             patch("trading.risk.manager.compute_volume_trend", return_value=0.0), \
             patch("trading.risk.manager.check_spread", return_value=1.0), \
             patch("trading.risk.manager.check_market_impact", return_value=True):
            result = rm.check_trade(_make_signal("BTC/USD", "buy"), 40000)
        self.assertFalse(result.allowed)
        self.assertIn("position size", result.reason.lower())

    def test_leveraged_etf_effective_exposure(self):
        """UGL (2x gold) at $15K notional = $30K effective — should hit 25% limit on $100K portfolio."""
        rm = RiskManager(100000, account={
            "trading_blocked": False, "status": "ACTIVE",
            "buying_power": 50000, "cash": 50000,
        })
        # Existing $10K in UGL + buying $8K more = $18K * 2x = $36K effective > $25K limit
        existing_positions = [
            {"symbol": "UGL", "qty": 200, "current_price": 50.0, "avg_cost": 48.0},
        ]
        with patch("trading.risk.manager.get_positions", return_value=existing_positions), \
             patch("trading.risk.manager.get_daily_pnl", return_value=[]), \
             patch("trading.risk.manager.get_trades", return_value=[]), \
             patch("trading.risk.manager.compute_volume_ratio", return_value=1.0), \
             patch("trading.risk.manager.compute_volume_trend", return_value=0.0), \
             patch("trading.risk.manager.check_spread", return_value=1.0), \
             patch("trading.risk.manager.check_market_impact", return_value=True):
            result = rm.check_trade(_make_signal("UGL", "buy"), 8000)
        self.assertFalse(result.allowed)

    def test_non_leveraged_passes(self):
        rm = RiskManager(100000, account={
            "trading_blocked": False, "status": "ACTIVE",
            "buying_power": 50000, "cash": 50000,
        })
        with patch("trading.risk.manager.get_positions", return_value=[]), \
             patch("trading.risk.manager.get_daily_pnl", return_value=[]), \
             patch("trading.risk.manager.get_trades", return_value=[]), \
             patch("trading.execution.router.get_positions_from_bybit", return_value=[]), \
             patch("trading.risk.manager.compute_volume_ratio", return_value=1.0), \
             patch("trading.risk.manager.compute_volume_trend", return_value=0.0), \
             patch("trading.risk.manager.check_spread", return_value=1.0), \
             patch("trading.risk.manager.check_market_impact", return_value=True):
            result = rm.check_trade(_make_signal("BTC/USD", "buy"), 10000)
        self.assertTrue(result.allowed)


class TestCryptoExposure(unittest.TestCase):
    def test_crypto_cap_enforced(self):
        rm = RiskManager(100000, account={
            "trading_blocked": False, "status": "ACTIVE",
            "buying_power": 50000, "cash": 50000,
        })
        # Already 65K in crypto, trying to add 10K more -> 75K > 70K cap
        existing = [
            {"symbol": "BTC/USD", "qty": 0.7, "current_price": 80000.0, "avg_cost": 78000.0},
            {"symbol": "ETH/USD", "qty": 3.0, "current_price": 3000.0, "avg_cost": 2800.0},
        ]
        with patch("trading.risk.manager.get_positions", return_value=existing), \
             patch("trading.risk.manager.get_daily_pnl", return_value=[]), \
             patch("trading.risk.manager.get_trades", return_value=[]), \
             patch("trading.risk.manager.compute_volume_ratio", return_value=1.0), \
             patch("trading.risk.manager.compute_volume_trend", return_value=0.0), \
             patch("trading.risk.manager.check_spread", return_value=1.0), \
             patch("trading.risk.manager.check_market_impact", return_value=True):
            result = rm.check_trade(_make_signal("SOL/USD", "buy"), 10000)
        self.assertFalse(result.allowed)
        self.assertIn("crypto", result.reason.lower())


class TestCorrelationGroup(unittest.TestCase):
    def test_correlated_group_limit(self):
        rm = RiskManager(100000, account={
            "trading_blocked": False, "status": "ACTIVE",
            "buying_power": 50000, "cash": 50000,
        })
        # 45K in BTC ecosystem, trying to add 10K LTC -> 55K > 50K cap
        existing = [
            {"symbol": "BTC/USD", "qty": 0.5, "current_price": 90000.0, "avg_cost": 85000.0},
        ]
        with patch("trading.risk.manager.get_positions", return_value=existing), \
             patch("trading.risk.manager.get_daily_pnl", return_value=[]), \
             patch("trading.risk.manager.get_trades", return_value=[]), \
             patch("trading.risk.manager.compute_volume_ratio", return_value=1.0), \
             patch("trading.risk.manager.compute_volume_trend", return_value=0.0), \
             patch("trading.risk.manager.check_spread", return_value=1.0), \
             patch("trading.risk.manager.check_market_impact", return_value=True):
            result = rm.check_trade(_make_signal("LTC/USD", "buy"), 10000)
        self.assertFalse(result.allowed)
        self.assertIn("correlated", result.reason.lower())


class TestCashReserve(unittest.TestCase):
    def test_cash_reserve_enforced(self):
        rm = RiskManager(100000, account={
            "trading_blocked": False, "status": "ACTIVE",
            "buying_power": 8000, "cash": 8000,
        })
        # 8K cash, min reserve = 5K (5% of 100K), buying 5K would leave 3K < 5K
        with patch("trading.risk.manager.get_positions", return_value=[]), \
             patch("trading.risk.manager.get_daily_pnl", return_value=[]), \
             patch("trading.risk.manager.get_trades", return_value=[]), \
             patch("trading.risk.manager.compute_volume_ratio", return_value=1.0), \
             patch("trading.risk.manager.compute_volume_trend", return_value=0.0), \
             patch("trading.risk.manager.check_spread", return_value=1.0), \
             patch("trading.risk.manager.check_market_impact", return_value=True):
            result = rm.check_trade(_make_signal("BTC/USD", "buy"), 5000)
        self.assertFalse(result.allowed)
        self.assertIn("cash", result.reason.lower())


class TestTradeCount(unittest.TestCase):
    def test_max_trades_per_day(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades = [{"timestamp": f"{today}T{i:02d}:{j:02d}:00"} for i in range(24) for j in range(0, 60, 20)]  # 72 trades

        rm = RiskManager(100000, account={
            "trading_blocked": False, "status": "ACTIVE",
            "buying_power": 50000, "cash": 50000,
        })
        with patch("trading.risk.manager.get_positions", return_value=[]), \
             patch("trading.risk.manager.get_daily_pnl", return_value=[]), \
             patch("trading.risk.manager.get_trades", return_value=trades), \
             patch("trading.risk.manager.compute_volume_ratio", return_value=1.0), \
             patch("trading.risk.manager.compute_volume_trend", return_value=0.0), \
             patch("trading.risk.manager.check_spread", return_value=1.0), \
             patch("trading.risk.manager.check_market_impact", return_value=True):
            result = rm.check_trade(_make_signal("BTC/USD", "buy"), 5000)
        self.assertFalse(result.allowed)
        self.assertIn("trades today", result.reason.lower())


class TestLeverageFactors(unittest.TestCase):
    def test_ugl_is_2x(self):
        self.assertEqual(LEVERAGE_FACTORS.get("UGL"), 2.0)

    def test_agq_is_2x(self):
        self.assertEqual(LEVERAGE_FACTORS.get("AGQ"), 2.0)

    def test_btc_has_no_leverage(self):
        self.assertEqual(LEVERAGE_FACTORS.get("BTC/USD", 1.0), 1.0)


if __name__ == "__main__":
    unittest.main()
