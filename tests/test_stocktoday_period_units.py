"""R3 item8b：StockToday 周/月线 volume/amount 量级异常（按数据集单位）。

RED 证据（evidence/R3/red-8b-9-probe.json）：同一只股票同一天的日线和与 vendor
周/月行对照——daily 行 `vol` 单位是手（×100=股）、`amount` 单位是千元（×1000=元），
而 **weekly/monthly 行本身就是股/元**：

- 688018 周线 raw vol=69,109,799、amount=9,629,810,657；
  对应 5 个交易日 daily vol 之和 691,097.99 手 = 69,109,799 股（精确相等）、
  daily amount 之和 9,629,810.657 千元 = 9,629,810,657 元（精确相等）。
- 修复前适配器仍按 daily 的 ×100/×1000 处理，导致周线 6.9e9 股/9.6 万亿元。

本文件锁定：daily 保持 ×100/×1000；weekly/monthly 用 ×1/×1；
pro_bar（前复权日线，与 daily 同口径）保持 ×100/×1000。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import api
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.providers.stocktoday import StockTodayProvider

SECRET = "synthetic-stocktoday-test-token"
TZ_SHANGHAI = timezone(timedelta(hours=8))

DAILY_ROW = {"ts_code": "688018.SH", "trade_date": "20261026", "open": 151.5, "high": 152.8,
             "low": 142.4, "close": 143.33, "pre_close": 154.36, "change": -11.03,
             "pct_chg": -7.1456, "vol": 155025.63, "amount": 2295076.716}
WEEKLY_ROW = {"ts_code": "688018.SH", "trade_date": "20261023", "open": 117.0, "high": 156.24,
              "low": 115.4, "close": 154.36, "pre_close": 118.43, "vol": 69109799.0,
              "amount": 9629810657.0, "pct_chg": 0.3034, "change": 35.93}
MONTHLY_ROW = {"ts_code": "688018.SH", "trade_date": "20260930", "open": 124.99, "high": 127.17,
               "low": 101.05, "close": 115.08, "pre_close": 118.43, "vol": 92811842.0,
               "amount": 10666208009.0, "pct_chg": -0.0283, "change": -3.35}


class StockTodayPeriodUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = Mock()
        self.response = Mock(status_code=200)
        self.response.json.return_value = {"code": 0, "columns": list(DAILY_ROW),
                                           "data": [DAILY_ROW], "total": 1}
        self.transport.return_value = self.response
        self.provider = StockTodayProvider(
            token_loader=lambda: SECRET, post=self.transport,
            budget_path=Path(self.tmp.name) / "budget.sqlite3",
            clock=Mock(time=lambda: datetime(2026, 10, 26, 16, 0, tzinfo=TZ_SHANGHAI).timestamp(),
                       monotonic=lambda: 1000.0),
        )
        patch.object(api, "load_compiled_policy",
                     Mock(return_value=CompiledPolicy(None, "inactive", None))).start()
        self.addCleanup(patch.stopall)

    def _call(self, *, period, rows, count=None, start_date=None, end_date=None, adjustment="none"):
        self.response.json.return_value = {"code": 0, "columns": list(rows[0]),
                                           "data": rows, "total": len(rows)}
        params = {"code": "688018", "period": period, "adjustment": adjustment,
                  "source": "stocktoday"}
        if count is not None:
            params["count"] = count
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        outcome = self.provider.call("stock_kline", params)
        return outcome

    def test_daily_raw_units_are_lots_and_thousand_cny(self):
        outcome = self._call(period="daily", rows=[DAILY_ROW], count=1)
        bar = outcome.data["bars"][0]
        self.assertEqual(15502563.0, bar["volume"])
        self.assertEqual(2295076716.0, bar["amount"])
        self.assertEqual("share", outcome.data["volume_unit"])
        self.assertEqual("CNY", outcome.data["amount_unit"])

    def test_weekly_raw_units_are_already_shares_and_cny(self):
        outcome = self._call(period="weekly", rows=[WEEKLY_ROW], count=1)
        bar = outcome.data["bars"][0]
        self.assertEqual(69_109_799.0, bar["volume"])
        self.assertEqual(9_629_810_657.0, bar["amount"])

    def test_monthly_raw_units_are_already_shares_and_cny(self):
        outcome = self._call(period="monthly", rows=[MONTHLY_ROW], count=1)
        bar = outcome.data["bars"][0]
        self.assertEqual(92_811_842.0, bar["volume"])
        self.assertEqual(10_666_208_009.0, bar["amount"])

    def test_weekly_volume_matches_sum_of_daily_bars(self):
        daily_rows = [
            {"ts_code": "688018.SH", "trade_date": "20261023", "open": 140.33, "high": 156.24,
             "low": 136.31, "close": 154.36, "vol": 174726.95, "amount": 2551454.1},
            {"ts_code": "688018.SH", "trade_date": "20261022", "open": 145.13, "high": 146.99,
             "low": 141.04, "close": 141.36, "vol": 90870.21, "amount": 1304034.25},
        ]
        weekly_row = {"ts_code": "688018.SH", "trade_date": "20261023", "open": 140.33,
                      "high": 156.24, "low": 136.31, "close": 154.36,
                      "vol": 26559716.0, "amount": 3855488595.0}
        daily = self._call(period="daily", rows=daily_rows, count=2)
        weekly = self._call(period="weekly", rows=[weekly_row], count=1)
        self.assertAlmostEqual(sum(b["volume"] for b in daily.data["bars"]),
                               weekly.data["bars"][0]["volume"], delta=1.0)
        self.assertAlmostEqual(sum(b["amount"] for b in daily.data["bars"]),
                               weekly.data["bars"][0]["amount"], delta=1000.0)


if __name__ == "__main__":
    unittest.main()
