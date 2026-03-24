"""Tests for trading.risk.profit_manager — take-profit, trailing stop, ProfitTracker."""

import unittest
from unittest.mock import patch, MagicMock

from trading.risk.profit_manager import (
    TAKE_PROFIT_PCT,
    TRAILING_STOP_ACTIVATE,
    TRAILING_STOP_PCT,
    ProfitTracker,
    check_profit_targets,
)


class TestProfitTracker(unittest.TestCase):
    """Test ProfitTracker watermark tracking (mocked DB)."""

    @patch("trading.risk.profit_manager.load_watermarks", return_value={})
    @patch("trading.risk.profit_manager.save_watermark")
    @patch("trading.risk.profit_manager.delete_watermark")
    def test_update_raises_watermark(self, mock_del, mock_save, mock_load):
        tracker = ProfitTracker()
        tracker.update("BTC/USD", 100.0)
        self.assertEqual(tracker.get_high("BTC/USD"), 100.0)

        tracker.update("BTC/USD", 110.0)
        self.assertEqual(tracker.get_high("BTC/USD"), 110.0)

        # Lower price doesn't lower the watermark
        tracker.update("BTC/USD", 105.0)
        self.assertEqual(tracker.get_high("BTC/USD"), 110.0)

    @patch("trading.risk.profit_manager.load_watermarks", return_value={})
    @patch("trading.risk.profit_manager.save_watermark")
    @patch("trading.risk.profit_manager.delete_watermark")
    def test_remove_clears_symbol(self, mock_del, mock_save, mock_load):
        tracker = ProfitTracker()
        tracker.update("BTC/USD", 100.0)
        tracker.remove("BTC/USD")
        self.assertIsNone(tracker.get_high("BTC/USD"))

    @patch("trading.risk.profit_manager.load_watermarks", return_value={})
    @patch("trading.risk.profit_manager.save_watermark")
    @patch("trading.risk.profit_manager.delete_watermark")
    def test_initialise_from_cost_only_once(self, mock_del, mock_save, mock_load):
        tracker = ProfitTracker()
        tracker.initialise_from_cost("ETH/USD", 50.0)
        self.assertEqual(tracker.get_high("ETH/USD"), 50.0)

        # Second call with different cost should NOT overwrite
        tracker.initialise_from_cost("ETH/USD", 60.0)
        self.assertEqual(tracker.get_high("ETH/USD"), 50.0)

    @patch("trading.risk.profit_manager.load_watermarks", return_value={"BTC/USD": 95000.0})
    @patch("trading.risk.profit_manager.save_watermark")
    @patch("trading.risk.profit_manager.delete_watermark")
    def test_loads_from_db(self, mock_del, mock_save, mock_load):
        tracker = ProfitTracker()
        self.assertEqual(tracker.get_high("BTC/USD"), 95000.0)


class TestCheckProfitTargets(unittest.TestCase):
    """Test the profit check logic with a mocked tracker."""

    def _make_tracker(self):
        with patch("trading.risk.profit_manager.load_watermarks", return_value={}), \
             patch("trading.risk.profit_manager.save_watermark"), \
             patch("trading.risk.profit_manager.delete_watermark"):
            return ProfitTracker()

    @patch("trading.risk.profit_manager.log_action")
    def test_take_profit_triggered(self, mock_log):
        tracker = self._make_tracker()
        positions = [{
            "symbol": "BTC/USD",
            "qty": 0.5,
            "avg_cost": 80000.0,
            "current_price": 96500.0,  # 20.625% gain -> above 20% TP
            "unrealized_pnl_pct": 20.625,
        }]
        with patch("trading.risk.profit_manager.save_watermark"):
            actions = check_profit_targets(positions, tracker)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "take_profit")
        self.assertEqual(actions[0]["symbol"], "BTC/USD")
        self.assertGreaterEqual(actions[0]["pnl_pct"], TAKE_PROFIT_PCT)

    @patch("trading.risk.profit_manager.log_action")
    def test_trailing_stop_triggered(self, mock_log):
        tracker = self._make_tracker()

        # First, push the watermark high
        with patch("trading.risk.profit_manager.save_watermark"):
            tracker.update("ETH/USD", 4500.0)

        # With new params: activation=12%, trail=5%, high watermark=4500
        positions = [{
            "symbol": "ETH/USD",
            "qty": 5.0,
            "avg_cost": 4000.0,
            # Need gain >= 12%: price >= 4480
            # Need drawdown >= 5% from 4500: price <= 4275
            "current_price": 4490.0,  # gain = 12.25%, drawdown = 0.22%
            "unrealized_pnl_pct": 12.25,
        }]

        with patch("trading.risk.profit_manager.save_watermark"):
            actions = check_profit_targets(positions, tracker)

        # 0.22% drawdown < 5% trail -> should NOT trigger
        self.assertEqual(len(actions), 0)

        # Now use a price above 12% gain AND > 5% below high
        # gain >= 12%: price >= 4480
        # drawdown >= 5% from 4500: price <= 4275
        # Both conditions met: price = 4275 → gain = 6.875% (below 12% activation!)
        # Actually impossible with high=4500 and avg_cost=4000 to have both.
        # Push watermark higher first:
        with patch("trading.risk.profit_manager.save_watermark"):
            tracker.update("ETH/USD", 5100.0)  # New high

        # Now: gain >= 12%: price >= 4480, drawdown >= 5% from 5100: price <= 4845
        positions[0]["current_price"] = 4840.0  # gain = 21% (above 12%), drawdown = 5.1%
        with patch("trading.risk.profit_manager.save_watermark"):
            actions = check_profit_targets(positions, tracker)
        # 21% gain > 20% TP → take_profit fires first (before trailing stop check)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "take_profit")

        # Test actual trailing stop: gain >= 12% but < 20%, drawdown >= 5%
        with patch("trading.risk.profit_manager.save_watermark"):
            tracker.update("ETH/USD", 5000.0)  # Reset high
        # gain >= 12%: price >= 4480, drawdown >= 5% from 5000: price <= 4750
        positions[0]["current_price"] = 4700.0  # gain = 17.5%, drawdown = 6%
        with patch("trading.risk.profit_manager.save_watermark"):
            actions = check_profit_targets(positions, tracker)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "trailing_stop")

    @patch("trading.risk.profit_manager.log_action")
    def test_no_action_small_gain(self, mock_log):
        tracker = self._make_tracker()
        positions = [{
            "symbol": "SOL/USD",
            "qty": 10.0,
            "avg_cost": 100.0,
            "current_price": 103.0,  # 3% gain — below both thresholds
            "unrealized_pnl_pct": 3.0,
        }]
        with patch("trading.risk.profit_manager.save_watermark"):
            actions = check_profit_targets(positions, tracker)
        self.assertEqual(len(actions), 0)

    @patch("trading.risk.profit_manager.log_action")
    def test_no_action_loss(self, mock_log):
        tracker = self._make_tracker()
        positions = [{
            "symbol": "UGL",
            "qty": 20.0,
            "avg_cost": 50.0,
            "current_price": 45.0,  # -10% loss
            "unrealized_pnl_pct": -10.0,
        }]
        with patch("trading.risk.profit_manager.save_watermark"):
            actions = check_profit_targets(positions, tracker)
        self.assertEqual(len(actions), 0)


if __name__ == "__main__":
    unittest.main()
