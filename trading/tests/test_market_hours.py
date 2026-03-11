"""Tests for trading.execution.market_hours — NYSE hours, holidays, gating."""

import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trading.execution.market_hours import (
    ET,
    UTC,
    _compute_easter,
    _nth_weekday,
    _observe,
    can_trade_now,
    get_nyse_holidays,
    is_crypto_symbol,
    is_etf_symbol,
    is_market_open,
    next_market_open,
)


class TestEasterComputation(unittest.TestCase):
    """Verify the Anonymous Gregorian Easter algorithm against known dates."""

    KNOWN_EASTERS = {
        2024: date(2024, 3, 31),
        2025: date(2025, 4, 20),
        2026: date(2026, 4, 5),
        2027: date(2027, 3, 28),
        2028: date(2028, 4, 16),
        2030: date(2030, 4, 21),
    }

    def test_known_easter_dates(self):
        for year, expected in self.KNOWN_EASTERS.items():
            with self.subTest(year=year):
                self.assertEqual(_compute_easter(year), expected)


class TestObserve(unittest.TestCase):
    """Test Saturday/Sunday observation shifts."""

    def test_saturday_observed_friday(self):
        # July 4, 2026 is Saturday -> observed Friday July 3
        self.assertEqual(_observe(date(2026, 7, 4)), date(2026, 7, 3))

    def test_sunday_observed_monday(self):
        # Jan 1, 2028 is Saturday -> observed Dec 31, 2027
        self.assertEqual(_observe(date(2028, 1, 1)), date(2027, 12, 31))

    def test_weekday_unchanged(self):
        # June 19, 2026 is Friday -> no shift
        self.assertEqual(_observe(date(2026, 6, 19)), date(2026, 6, 19))


class TestNthWeekday(unittest.TestCase):
    def test_third_monday_jan_2026(self):
        # MLK Day 2026 = 3rd Monday of January = Jan 19
        result = _nth_weekday(2026, 1, 0, 3)
        self.assertEqual(result, date(2026, 1, 19))
        self.assertEqual(result.weekday(), 0)  # Monday

    def test_fourth_thursday_nov_2026(self):
        # Thanksgiving 2026 = 4th Thursday of November = Nov 26
        result = _nth_weekday(2026, 11, 3, 4)
        self.assertEqual(result, date(2026, 11, 26))
        self.assertEqual(result.weekday(), 3)  # Thursday


class TestNYSEHolidays(unittest.TestCase):
    def test_2026_has_ten_holidays(self):
        holidays = get_nyse_holidays(2026)
        self.assertEqual(len(holidays), 10)

    def test_2027_has_ten_holidays(self):
        holidays = get_nyse_holidays(2027)
        self.assertEqual(len(holidays), 10)

    def test_good_friday_2026(self):
        holidays = get_nyse_holidays(2026)
        easter = _compute_easter(2026)
        good_friday = easter - timedelta(days=2)
        self.assertIn(good_friday, holidays)

    def test_caching(self):
        h1 = get_nyse_holidays(2026)
        h2 = get_nyse_holidays(2026)
        self.assertIs(h1, h2)  # Same object — cached


class TestIsMarketOpen(unittest.TestCase):
    def test_weekday_during_hours(self):
        # Wednesday 2026-03-11 at 10:00 AM ET
        dt = datetime(2026, 3, 11, 10, 0, tzinfo=ET).astimezone(UTC)
        self.assertTrue(is_market_open(dt))

    def test_weekday_before_open(self):
        dt = datetime(2026, 3, 11, 8, 0, tzinfo=ET).astimezone(UTC)
        self.assertFalse(is_market_open(dt))

    def test_weekday_after_close(self):
        dt = datetime(2026, 3, 11, 16, 30, tzinfo=ET).astimezone(UTC)
        self.assertFalse(is_market_open(dt))

    def test_weekend_saturday(self):
        dt = datetime(2026, 3, 14, 12, 0, tzinfo=ET).astimezone(UTC)
        self.assertFalse(is_market_open(dt))

    def test_holiday(self):
        # Christmas 2026 is Friday Dec 25
        dt = datetime(2026, 12, 25, 12, 0, tzinfo=ET).astimezone(UTC)
        self.assertFalse(is_market_open(dt))


class TestSymbolClassification(unittest.TestCase):
    def test_crypto_symbol(self):
        self.assertTrue(is_crypto_symbol("BTC/USD"))
        self.assertTrue(is_crypto_symbol("ETH/USDT"))

    def test_etf_symbol(self):
        self.assertTrue(is_etf_symbol("UGL"))
        self.assertTrue(is_etf_symbol("SPY"))

    def test_crypto_not_etf(self):
        self.assertFalse(is_etf_symbol("BTC/USD"))

    def test_etf_not_crypto(self):
        self.assertFalse(is_crypto_symbol("UGL"))


class TestCanTradeNow(unittest.TestCase):
    def test_crypto_always_tradeable(self):
        # Even on a weekend at midnight
        dt = datetime(2026, 3, 14, 3, 0, tzinfo=UTC)
        allowed, reason = can_trade_now("BTC/USD", dt)
        self.assertTrue(allowed)
        self.assertIn("24/7", reason)

    def test_etf_during_market_hours(self):
        dt = datetime(2026, 3, 11, 10, 0, tzinfo=ET).astimezone(UTC)
        allowed, reason = can_trade_now("UGL", dt)
        self.assertTrue(allowed)

    def test_etf_outside_market_hours(self):
        dt = datetime(2026, 3, 11, 20, 0, tzinfo=ET).astimezone(UTC)
        allowed, reason = can_trade_now("UGL", dt)
        self.assertFalse(allowed)
        self.assertIn("queue", reason.lower())


class TestNextMarketOpen(unittest.TestCase):
    def test_returns_future_datetime(self):
        now = datetime(2026, 3, 11, 20, 0, tzinfo=UTC)
        nmo = next_market_open(now)
        self.assertGreater(nmo, now)
        self.assertEqual(nmo.tzinfo, UTC)

    def test_skips_weekend(self):
        # Friday after close -> should return Monday 9:30 AM ET
        friday_evening = datetime(2026, 3, 13, 21, 0, tzinfo=UTC)
        nmo = next_market_open(friday_evening)
        nmo_et = nmo.astimezone(ET)
        self.assertEqual(nmo_et.weekday(), 0)  # Monday
        self.assertEqual(nmo_et.time(), time(9, 30))


if __name__ == "__main__":
    unittest.main()
