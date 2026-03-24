"""Tests for trading.config — startup validation."""

import unittest
from unittest.mock import patch

from trading.config import validate_config, ConfigError


# All tests need valid ASTER keys to pass the first gate
_ASTER_MOCKS = {
    "trading.config.ASTER_API_KEY": "test-aster-key",
    "trading.config.ASTER_API_SECRET": "test-aster-secret",
}


class TestValidateConfig(unittest.TestCase):
    """Test startup config validation."""

    def test_missing_aster_api_key_raises(self):
        with patch("trading.config.ASTER_API_KEY", ""), \
             patch("trading.config.ASTER_API_SECRET", "test-secret"):
            with self.assertRaises(ConfigError):
                validate_config(test_api=False)

    def test_missing_aster_secret_raises(self):
        with patch("trading.config.ASTER_API_KEY", "test-key"), \
             patch("trading.config.ASTER_API_SECRET", ""):
            with self.assertRaises(ConfigError):
                validate_config(test_api=False)

    @patch("trading.config.TRADING_MODE", "invalid")
    def test_invalid_trading_mode_raises(self):
        with patch.dict("os.environ", {}, clear=False), \
             patch("trading.config.ASTER_API_KEY", "k"), \
             patch("trading.config.ASTER_API_SECRET", "s"):
            with self.assertRaises(ConfigError):
                validate_config(test_api=False)

    @patch("trading.config.TRADING_MODE", "live")
    @patch("trading.config.ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    def test_live_mode_paper_url_raises(self):
        with patch("trading.config.ASTER_API_KEY", "k"), \
             patch("trading.config.ASTER_API_SECRET", "s"):
            with self.assertRaises(ConfigError):
                validate_config(test_api=False)

    @patch("trading.config.TRADING_MODE", "paper")
    @patch("trading.config.FRED_API_KEY", "")
    def test_missing_fred_warns(self):
        with patch("trading.config.ASTER_API_KEY", "k"), \
             patch("trading.config.ASTER_API_SECRET", "s"):
            warnings = validate_config(test_api=False)
            self.assertTrue(any("FRED" in w for w in warnings))

    @patch("trading.config.TRADING_MODE", "paper")
    @patch("trading.config.FRED_API_KEY", "some-key")
    @patch("trading.config.ALPACA_API_KEY", "some-key")
    def test_valid_config_no_warnings(self):
        with patch("trading.config.ASTER_API_KEY", "k"), \
             patch("trading.config.ASTER_API_SECRET", "s"):
            warnings = validate_config(test_api=False)
            self.assertEqual(len(warnings), 0)


if __name__ == "__main__":
    unittest.main()
