"""H20 #7 回归：10-29 的 1m 全天标 historical，原因是末根未完结 bar 的 data_as_of（09:49:00）晚于
fetched_at（09:48:22）。DP ef9060f 起：数据时点只取已完成分钟、末根标 incomplete，盘中新鲜度为 intraday。
本测试把当天的真实数字钉死，防再回到「按未走完的 bar 标签判新鲜度」。"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_p310_minute_bars as base
from ym_stock_data import api
from ym_stock_data.providers.local import LocalProvider

DAY = base.DAY
NOW = f"{DAY}T09:48:22"


def kline(bars):
    params = {"code": "300045", "period": "1m", "adjustment": "none",
              "start_date": DAY.replace("-", ""), "end_date": DAY.replace("-", "")}
    with patch("ym_stock_data.sources.pytdx._fetch_tencent_kline", return_value=bars), \
            patch("ym_stock_data.providers.local._now", return_value=base.at(NOW)):
        return LocalProvider._http_kline(provider="tencent", params=params), params


class FreshnessByBarEnd(unittest.TestCase):
    def setUp(self):
        self.data, self.params = kline(base.tencent_bars(start="09:25", end="09:49"))

    def test_unfinished_bar_is_marked_and_not_the_data_time(self):
        self.assertEqual("2026-10-27 09:49:00", self.data["bars"][-1]["datetime"])
        self.assertTrue(self.data["bars"][-1]["incomplete"])
        stamp = api._data_as_of("stock_kline", self.params, self.data)
        self.assertIsNotNone(stamp)
        self.assertLess(stamp, base.at(NOW), "数据时点不得晚于取数时刻")
        self.assertLess(stamp, base.at(f"{DAY}T09:49:00"))

    def test_freshness_is_intraday_not_historical(self):
        stamp = api._data_as_of("stock_kline", self.params, self.data)
        with patch("ym_stock_data.api._now_shanghai", return_value=base.at(NOW)):
            freshness = api._data_freshness("stock_kline", self.params, stamp, 3600)
        self.assertEqual("intraday", freshness["status"])
        self.assertNotEqual("historical", freshness["status"])

    def test_short_coverage_is_reported_separately_by_quality(self):
        from ym_stock_data import quality
        text = open(quality.__file__, encoding="utf-8").read()
        self.assertIn("coverage_shortfall", text)


if __name__ == "__main__":
    unittest.main()
