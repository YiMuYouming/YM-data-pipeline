"""test_week_info.py — W1 S7（K3）：单一交易日历提供周信息

看板的周报、周分组、下个交易日判断都从这里取；日历只留管道一份。
"""

import unittest
from pathlib import Path
from datetime import date

from ym_stock_data.trading_calendar import (
    TradeCalendarUnavailable,
    next_trading_day,
    previous_trading_day,
    week_info,
)


class WeekInfoTest(unittest.TestCase):
    def test_week_shape(self):
        info = week_info("2026-09-30")
        self.assertEqual(set(info), {
            "week_label", "iso_year", "iso_week", "trading_days",
            "prev_trading_day", "next_trading_day", "prev_week_last_trading_day",
        })
        self.assertRegex(info["week_label"], r"^\d{4}-W\d{2}$")
        self.assertIsInstance(info["iso_year"], int)
        self.assertIsInstance(info["iso_week"], int)

    def test_trading_days_are_sorted_and_exclude_weekends_and_holidays(self):
        info = week_info("2026-09-30")
        days = info["trading_days"]
        self.assertEqual(days, sorted(days))
        for value in days:
            self.assertLessEqual(date.fromisoformat(value), date(2026, 9, 30))
        self.assertNotIn("2026-09-26", days)   # 周六
        self.assertNotIn("2026-09-27", days)   # 周日

    def test_week_label_is_stable_inside_one_week(self):
        self.assertEqual(week_info("2026-09-28")["week_label"],
                         week_info("2026-09-30")["week_label"])

    def test_prev_week_last_trading_day_precedes_this_week(self):
        info = week_info("2026-09-30")
        self.assertLess(date.fromisoformat(info["prev_week_last_trading_day"]),
                        date.fromisoformat(info["trading_days"][0]))

    def test_neighbours_agree_with_the_scalar_helpers(self):
        info = week_info("2026-09-30")
        self.assertEqual(info["prev_trading_day"],
                         previous_trading_day(date(2026, 9, 30)).isoformat())
        self.assertEqual(info["next_trading_day"],
                         next_trading_day(date(2026, 9, 30)).isoformat())

    def test_holiday_is_not_a_trading_day_in_its_week(self):
        # 2026-10-01~10-07 国庆休市：那一周的 trading_days 必须为空或不含这些日子
        info = week_info("2026-10-02")
        for value in info["trading_days"]:
            self.assertNotIn(value, {"2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"})

    def test_bad_date_raises(self):
        with self.assertRaises(ValueError):
            week_info("not-a-date")


class SingleSourceTest(unittest.TestCase):
    """日历只留管道一份（公共约定 K3）。"""

    DASHBOARD_TABLE = Path(__file__).resolve().parents[2] / "live-dashboard" / "config" / "trading_holidays.json"

    def test_dashboard_local_holiday_table_is_gone(self):
        self.assertFalse(self.DASHBOARD_TABLE.exists(),
                         "live-dashboard/config/trading_holidays.json 应删除，日历只留管道一份")


if __name__ == "__main__":
    unittest.main()
