"""Tests for trading.config — startup validation."""

import unittest
from unittest.mock import patch

from trading.config import validate_config, ConfigError


# All tests need valid BYBIT keys to pass the first gate
_ASTER_MOCKS = {
    "trading.config.BYBIT_API_KEY": "test-bybit-key",
    "trading.config.BYBIT_API_SECRET": "test-bybit-secret",
}


class TestValidateConfig(unittest.TestCase):
    """Test startup config validation."""

    def test_missing_bybit_api_key_raises(self):
        with patch("trading.config.BYBIT_API_KEY", ""), \
             patch("trading.config.BYBIT_API_SECRET", "test-secret"):
            with self.assertRaises(ConfigError):
                validate_config(test_api=False)

    def test_missing_bybit_secret_raises(self):
        with patch("trading.config.BYBIT_API_KEY", "test-key"), \
             patch("trading.config.BYBIT_API_SECRET", ""):
            with self.assertRaises(ConfigError):
                validate_config(test_api=False)

    @patch("trading.config.TRADING_MODE", "invalid")
    def test_invalid_trading_mode_raises(self):
        with patch.dict("os.environ", {}, clear=False), \
             patch("trading.config.BYBIT_API_KEY", "k"), \
             patch("trading.config.BYBIT_API_SECRET", "s"):
            with self.assertRaises(ConfigError):
                validate_config(test_api=False)

    @patch("trading.config.TRADING_MODE", "live")
    @patch("trading.config.ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    def test_live_mode_paper_url_raises(self):
        with patch("trading.config.BYBIT_API_KEY", "k"), \
             patch("trading.config.BYBIT_API_SECRET", "s"):
            with self.assertRaises(ConfigError):
                validate_config(test_api=False)

    @patch("trading.config.TRADING_MODE", "paper")
    @patch("trading.config.FRED_API_KEY", "")
    def test_missing_fred_warns(self):
        with patch("trading.config.BYBIT_API_KEY", "k"), \
             patch("trading.config.BYBIT_API_SECRET", "s"):
            warnings = validate_config(test_api=False)
            self.assertTrue(any("FRED" in w for w in warnings))

    @patch("trading.config.TRADING_MODE", "paper")
    @patch("trading.config.FRED_API_KEY", "some-key")
    @patch("trading.config.ALPACA_API_KEY", "some-key")
    def test_valid_config_no_warnings(self):
        with patch("trading.config.BYBIT_API_KEY", "k"), \
             patch("trading.config.BYBIT_API_SECRET", "s"):
            warnings = validate_config(test_api=False)
            self.assertEqual(len(warnings), 0)


class TestPreflightCheck(unittest.TestCase):
    """Test strategy deployment pre-flight checks."""

    @patch("trading.strategy.registry._discovered", True)
    @patch("trading.strategy.registry._load_errors", {})
    def test_preflight_all_strategies_loaded(self):
        from trading.strategy.registry import preflight_check, _registry
        enabled = {"strat_a": True, "strat_b": True, "strat_c": False}

        # Create minimal mock strategy classes
        class MockA:
            name = "strat_a"
        class MockB:
            name = "strat_b"

        with patch("trading.config.STRATEGY_ENABLED", enabled), \
             patch.dict(_registry, {"strat_a": MockA, "strat_b": MockB}, clear=True):
            result = preflight_check()
            self.assertTrue(result.passed)
            self.assertEqual(sorted(result.loaded), ["strat_a", "strat_b"])
            self.assertEqual(result.missing, [])
            self.assertEqual(result.failed, {})

    @patch("trading.strategy.registry._discovered", True)
    @patch("trading.strategy.registry._load_errors", {})
    def test_preflight_detects_missing_strategy(self):
        from trading.strategy.registry import preflight_check, _registry
        enabled = {"strat_a": True, "strat_missing": True}

        class MockA:
            name = "strat_a"

        with patch("trading.config.STRATEGY_ENABLED", enabled), \
             patch.dict(_registry, {"strat_a": MockA}, clear=True):
            result = preflight_check()
            self.assertFalse(result.passed)
            self.assertEqual(result.loaded, ["strat_a"])
            self.assertEqual(result.missing, ["strat_missing"])

    @patch("trading.strategy.registry._discovered", True)
    def test_preflight_detects_broken_import(self):
        from trading.strategy.registry import preflight_check, _registry
        enabled = {"strat_a": True, "strat_broken": True}

        class MockA:
            name = "strat_a"

        with patch("trading.config.STRATEGY_ENABLED", enabled), \
             patch.dict(_registry, {"strat_a": MockA}, clear=True), \
             patch("trading.strategy.registry._load_errors", {"strat_broken": "SyntaxError: bad"}):
            result = preflight_check()
            self.assertFalse(result.passed)
            self.assertEqual(result.loaded, ["strat_a"])
            self.assertEqual(result.missing, [])
            self.assertEqual(result.failed, {"strat_broken": "SyntaxError: bad"})

    @patch("trading.config.TRADING_MODE", "paper")
    @patch("trading.config.FRED_API_KEY", "k")
    @patch("trading.config.ALPACA_API_KEY", "k")
    def test_validate_config_rejects_missing_strategy_file(self):
        from pathlib import Path
        original_exists = Path.exists

        def mock_exists(self_path):
            if self_path.name == "fake_missing.py":
                return False
            return original_exists(self_path)

        enabled = {"fake_missing": True}
        with patch("trading.config.BYBIT_API_KEY", "k"), \
             patch("trading.config.BYBIT_API_SECRET", "s"), \
             patch("trading.config.STRATEGY_ENABLED", enabled), \
             patch.object(Path, "exists", mock_exists):
            with self.assertRaises(ConfigError) as ctx:
                validate_config(test_api=False)
            self.assertIn("fake_missing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
