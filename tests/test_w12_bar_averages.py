import unittest
from ym_stock_data import indicators


class BarAveragesTest(unittest.TestCase):
    def test_same_window_price_and_amount_averages(self):
        bars = [{"close": i, "amount": i * 100} for i in range(1, 22)]
        self.assertEqual(indicators.daily_bar_averages(bars),
                         {"ma5": 19, "ma20": 11.5, "mean20_amount": 1150})

    def test_missing_amount_is_not_zero(self):
        bars = [{"close": 10, "amount": 100} for _ in range(21)]
        bars[-1]["amount"] = None
        with self.assertRaises(ValueError):
            indicators.daily_bar_averages(bars)
