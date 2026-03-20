"""Tests for system upgrades — sync error handling, rate limiter, cache, leverage validation."""

import time
import unittest
from unittest.mock import patch, MagicMock


class TestSyncErrorHandling(unittest.TestCase):
    """Position sync failures must raise SyncError, not silently return 0."""

    def test_sync_positions_raises_on_broker_failure(self):
        from trading.execution.sync import sync_positions, SyncError

        with patch("trading.execution.sync.get_positions_from_alpaca",
                   side_effect=ConnectionError("API unreachable")), \
             patch("trading.execution.sync.log_action"):
            with self.assertRaises(SyncError):
                sync_positions()

    def test_run_sync_propagates_sync_error(self):
        from trading.execution.sync import run_sync, SyncError

        with patch("trading.execution.sync.get_positions_from_alpaca",
                   side_effect=ConnectionError("API unreachable")), \
             patch("trading.execution.sync.log_action"):
            with self.assertRaises(SyncError):
                run_sync()

    def test_sync_positions_succeeds_on_empty_positions(self):
        from trading.execution.sync import sync_positions

        with patch("trading.execution.sync.get_positions_from_alpaca", return_value=[]), \
             patch("trading.execution.sync.get_open_trades", return_value=[]), \
             patch("trading.execution.sync.get_positions", return_value=[]), \
             patch("trading.execution.sync.log_action"):
            result = sync_positions()
        self.assertEqual(result, 0)


class TestCachePruning(unittest.TestCase):
    """Cache should prune expired entries, not wipe everything."""

    def test_clear_cache_retains_fresh_entries(self):
        from trading.data.cache import _cache, clear_cache

        # Insert a fresh entry
        _cache["fresh_key"] = (time.time(), "fresh_value")
        # Insert an expired entry (10 minutes old)
        _cache["expired_key"] = (time.time() - 600, "old_value")

        clear_cache()

        self.assertIn("fresh_key", _cache)
        self.assertNotIn("expired_key", _cache)

        # Cleanup
        _cache.clear()

    def test_force_clear_wipes_everything(self):
        from trading.data.cache import _cache, force_clear_cache

        _cache["key1"] = (time.time(), "val1")
        _cache["key2"] = (time.time(), "val2")

        force_clear_cache()

        self.assertEqual(len(_cache), 0)


class TestRateLimiter(unittest.TestCase):
    """Rate limiter should throttle requests."""

    def test_acquire_within_limit(self):
        from trading.execution.aster_client import _RateLimiter

        limiter = _RateLimiter(max_requests=10, window_seconds=1.0)

        # Should not block for 5 requests
        start = time.time()
        for _ in range(5):
            limiter.acquire()
        elapsed = time.time() - start

        self.assertLess(elapsed, 0.5)
        self.assertEqual(limiter.current_count, 5)

    def test_current_count_resets(self):
        from trading.execution.aster_client import _RateLimiter

        limiter = _RateLimiter(max_requests=100, window_seconds=0.1)

        for _ in range(5):
            limiter.acquire()
        self.assertEqual(limiter.current_count, 5)

        time.sleep(0.15)
        self.assertEqual(limiter.current_count, 0)


class TestLeverageValidation(unittest.TestCase):
    """Leverage profile validation should warn about dangerous configs."""

    @patch("trading.config.LEVERAGE_PROFILE", "greedy")
    def test_greedy_profile_warns(self):
        from trading.config import validate_leverage_profile
        warnings = validate_leverage_profile()
        self.assertTrue(any("greedy" in w.lower() for w in warnings))

    @patch("trading.config.LEVERAGE_PROFILE", "conservative")
    def test_conservative_no_leverage_warnings(self):
        from trading.config import validate_leverage_profile
        warnings = validate_leverage_profile()
        # Conservative should have no high-leverage warnings
        high_lev_warnings = [w for w in warnings if "LEVERAGE WARNING" in w and "liquidation" in w]
        self.assertEqual(len(high_lev_warnings), 0)


if __name__ == "__main__":
    unittest.main()
