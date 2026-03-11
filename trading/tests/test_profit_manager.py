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
            "current_price": 92500.0,  # 15.625% gain -> above 15% TP
            "unrealized_pnl_pct": 15.625,
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

        positions = [{
            "symbol": "ETH/USD",
            "qty": 5.0,
            "avg_cost": 4000.0,
            # current_price must be:
            # - above 8% gain from cost (4000 * 1.08 = 4320)
            # - more than 4% below high watermark of 4500 (4500 * 0.96 = 4320)
            "current_price": 4310.0,  # gain = 7.75%... need > 8%
            "unrealized_pnl_pct": 7.75,
        }]
        # Actually, 4310 / 4000 = 7.75% which is below 8% activation.
        # Let's use a price that's above 8% gain but below the trail
        positions[0]["current_price"] = 4330.0  # gain = 8.25%, high=4500, drawdown = 3.78%

        with patch("trading.risk.profit_manager.save_watermark"):
            actions = check_profit_targets(positions, tracker)

        # 3.78% drawdown < 4% trail -> should NOT trigger
        self.assertEqual(len(actions), 0)

        # Now use a price that triggers the trail
        positions[0]["current_price"] = 4310.0  # gain = 7.75% -> below activation, no trigger

        # Use a price above activation with drawdown > 4%
        positions[0]["current_price"] = 4321.0  # gain = 8.025%
        # drawdown = (4500 - 4321) / 4500 = 3.98% -> still below 4%

        positions[0]["current_price"] = 4315.0  # gain = 7.875% -> below 8%, no activation
        # Need: gain >= 8% AND drawdown >= 4%
        # gain >= 8%: price >= 4320
        # drawdown >= 4% from 4500: price <= 4320
        # Exactly at boundary: price = 4320, gain = 8%, drawdown = 4%
        positions[0]["current_price"] = 4320.0
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
