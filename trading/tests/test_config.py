"""Tests for trading.config — startup validation."""

import unittest
from unittest.mock import patch

from trading.config import validate_config, ConfigError


class TestValidateConfig(unittest.TestCase):
    """Test startup config validation."""

    @patch("trading.config.ALPACA_API_KEY", "")
    def test_missing_api_key_raises(self):
        with self.assertRaises(ConfigError):
            validate_config(test_api=False)

    @patch("trading.config.ALPACA_API_KEY", "test-key")
    @patch("trading.config.ALPACA_SECRET_KEY", "")
    def test_missing_secret_key_raises(self):
        with self.assertRaises(ConfigError):
            validate_config(test_api=False)

    @patch("trading.config.ALPACA_API_KEY", "test-key")
    @patch("trading.config.ALPACA_SECRET_KEY", "test-secret")
    @patch("trading.config.TRADING_MODE", "invalid")
    def test_invalid_trading_mode_raises(self):
        with self.assertRaises(ConfigError):
            validate_config(test_api=False)

    @patch("trading.config.ALPACA_API_KEY", "test-key")
    @patch("trading.config.ALPACA_SECRET_KEY", "test-secret")
    @patch("trading.config.TRADING_MODE", "live")
    @patch("trading.config.ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    def test_live_mode_paper_url_raises(self):
        with self.assertRaises(ConfigError):
            validate_config(test_api=False)

    @patch("trading.config.ALPACA_API_KEY", "test-key")
    @patch("trading.config.ALPACA_SECRET_KEY", "test-secret")
    @patch("trading.config.TRADING_MODE", "paper")
    @patch("trading.config.FRED_API_KEY", "")
    def test_missing_fred_warns(self):
        warnings = validate_config(test_api=False)
        self.assertTrue(any("FRED" in w for w in warnings))

    @patch("trading.config.ALPACA_API_KEY", "test-key")
    @patch("trading.config.ALPACA_SECRET_KEY", "test-secret")
    @patch("trading.config.TRADING_MODE", "paper")
    @patch("trading.config.FRED_API_KEY", "some-key")
    def test_valid_config_no_warnings(self):
        warnings = validate_config(test_api=False)
        self.assertEqual(len(warnings), 0)


if __name__ == "__main__":
    unittest.main()
