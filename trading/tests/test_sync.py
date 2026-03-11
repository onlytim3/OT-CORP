"""Tests for trading.execution.sync — FIFO trade pairing with partial quantities."""

import unittest
from unittest.mock import patch, MagicMock


class TestPairTradesLogic(unittest.TestCase):
    """Test the FIFO trade pairing logic with various scenarios."""

    def _run_pair_trades(self, open_trades):
        """Run pair_trades with mocked DB calls."""
        closed_trades = []
        reduced_trades = []

        def mock_close_trade(trade_id, price, pnl):
            closed_trades.append({"id": trade_id, "price": price, "pnl": pnl})

        def mock_reduce_qty(trade_id, new_qty, new_total):
            reduced_trades.append({"id": trade_id, "qty": new_qty, "total": new_total})
            # Update in-memory trade
            for t in open_trades:
                if t["id"] == trade_id:
                    t["qty"] = new_qty
                    t["total"] = new_total

        with patch("trading.execution.sync.get_open_trades", return_value=open_trades), \
             patch("trading.execution.sync.close_trade", side_effect=mock_close_trade), \
             patch("trading.execution.sync._reduce_trade_qty", side_effect=mock_reduce_qty), \
             patch("trading.execution.sync.log_action"), \
             patch("trading.execution.sync.record_outcome"), \
             patch("trading.execution.sync.console"):
            from trading.execution.sync import pair_trades
            paired = pair_trades()

        return paired, closed_trades, reduced_trades

    def test_exact_match_buy_sell(self):
        """Buy 1 BTC, sell 1 BTC — both fully closed."""
        trades = [
            {"id": 1, "symbol": "BTC/USD", "side": "buy", "qty": 1.0,
             "price": 80000.0, "total": 80000.0, "timestamp": "2026-03-10T10:00:00"},
            {"id": 2, "symbol": "BTC/USD", "side": "sell", "qty": 1.0,
             "price": 85000.0, "total": 85000.0, "timestamp": "2026-03-11T10:00:00"},
        ]
        paired, closed, reduced = self._run_pair_trades(trades)
        self.assertEqual(paired, 1)
        # Both buy and sell should be closed
        closed_ids = {c["id"] for c in closed}
        self.assertIn(1, closed_ids)  # buy closed
        self.assertIn(2, closed_ids)  # sell closed

    def test_partial_sell_reduces_buy(self):
        """Buy 2 BTC, sell 1 BTC — buy should be reduced to 1, not fully closed."""
        trades = [
            {"id": 1, "symbol": "BTC/USD", "side": "buy", "qty": 2.0,
             "price": 80000.0, "total": 160000.0, "timestamp": "2026-03-10T10:00:00"},
            {"id": 2, "symbol": "BTC/USD", "side": "sell", "qty": 1.0,
             "price": 85000.0, "total": 85000.0, "timestamp": "2026-03-11T10:00:00"},
        ]
        paired, closed, reduced = self._run_pair_trades(trades)
        self.assertEqual(paired, 1)
        # Buy should be reduced, not closed
        self.assertEqual(len(reduced), 1)
        self.assertEqual(reduced[0]["id"], 1)
        self.assertAlmostEqual(reduced[0]["qty"], 1.0)

    def test_sell_spans_multiple_buys(self):
        """Two buys of 0.5 BTC each, one sell of 1 BTC — both buys closed."""
        trades = [
            {"id": 1, "symbol": "BTC/USD", "side": "buy", "qty": 0.5,
             "price": 78000.0, "total": 39000.0, "timestamp": "2026-03-10T08:00:00"},
            {"id": 2, "symbol": "BTC/USD", "side": "buy", "qty": 0.5,
             "price": 82000.0, "total": 41000.0, "timestamp": "2026-03-10T12:00:00"},
            {"id": 3, "symbol": "BTC/USD", "side": "sell", "qty": 1.0,
             "price": 90000.0, "total": 90000.0, "timestamp": "2026-03-11T10:00:00"},
        ]
        paired, closed, reduced = self._run_pair_trades(trades)
        self.assertEqual(paired, 2)
        closed_ids = {c["id"] for c in closed}
        self.assertIn(1, closed_ids)
        self.assertIn(2, closed_ids)
        self.assertIn(3, closed_ids)  # sell also closed

    def test_no_sells_no_pairing(self):
        """Only buys — nothing to pair."""
        trades = [
            {"id": 1, "symbol": "BTC/USD", "side": "buy", "qty": 1.0,
             "price": 80000.0, "total": 80000.0, "timestamp": "2026-03-10T10:00:00"},
        ]
        paired, closed, reduced = self._run_pair_trades(trades)
        self.assertEqual(paired, 0)

    def test_different_symbols_not_paired(self):
        """BTC buy + ETH sell should not pair."""
        trades = [
            {"id": 1, "symbol": "BTC/USD", "side": "buy", "qty": 1.0,
             "price": 80000.0, "total": 80000.0, "timestamp": "2026-03-10T10:00:00"},
            {"id": 2, "symbol": "ETH/USD", "side": "sell", "qty": 5.0,
             "price": 3500.0, "total": 17500.0, "timestamp": "2026-03-11T10:00:00"},
        ]
        paired, closed, reduced = self._run_pair_trades(trades)
        self.assertEqual(paired, 0)

    def test_pnl_calculation(self):
        """Verify P&L is correctly computed."""
        trades = [
            {"id": 1, "symbol": "ETH/USD", "side": "buy", "qty": 10.0,
             "price": 3000.0, "total": 30000.0, "timestamp": "2026-03-10T10:00:00"},
            {"id": 2, "symbol": "ETH/USD", "side": "sell", "qty": 10.0,
             "price": 3500.0, "total": 35000.0, "timestamp": "2026-03-11T10:00:00"},
        ]
        paired, closed, reduced = self._run_pair_trades(trades)
        self.assertEqual(paired, 1)
        # Find the buy close entry to check P&L
        buy_close = next(c for c in closed if c["id"] == 1)
        expected_pnl = (3500.0 - 3000.0) * 10.0  # $5000 profit
        self.assertAlmostEqual(buy_close["pnl"], expected_pnl)


class TestSyncPositions(unittest.TestCase):
    """Basic tests for sync_positions flow."""

    @patch("trading.execution.sync.get_positions_from_alpaca")
    @patch("trading.execution.sync.get_open_trades", return_value=[])
    @patch("trading.execution.sync.upsert_position")
    @patch("trading.execution.sync.get_positions", return_value=[])
    @patch("trading.execution.sync.log_action")
    @patch("trading.execution.sync.console")
    def test_sync_empty_positions(self, mock_console, mock_log, mock_get_pos,
                                   mock_upsert, mock_trades, mock_alpaca):
        mock_alpaca.return_value = []
        from trading.execution.sync import sync_positions
        count = sync_positions()
        self.assertEqual(count, 0)

    @patch("trading.execution.sync.get_positions_from_alpaca")
    @patch("trading.execution.sync.get_open_trades", return_value=[
        {"symbol": "BTC/USD", "strategy": "momentum"}
    ])
    @patch("trading.execution.sync.upsert_position")
    @patch("trading.execution.sync.get_positions", return_value=[])
    @patch("trading.execution.sync.log_action")
    @patch("trading.execution.sync.console")
    def test_sync_one_position(self, mock_console, mock_log, mock_get_pos,
                                mock_upsert, mock_trades, mock_alpaca):
        mock_alpaca.return_value = [{
            "symbol": "BTC/USD", "qty": 0.5,
            "avg_cost": 80000.0, "current_price": 85000.0,
            "market_value": 42500.0, "unrealized_pnl": 2500.0,
            "unrealized_pnl_pct": 6.25, "side": "long",
        }]
        from trading.execution.sync import sync_positions
        count = sync_positions()
        self.assertEqual(count, 1)
        mock_upsert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
