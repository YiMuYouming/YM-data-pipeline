"""R3 item9：StockToday 前复权日 K（pro_bar）字符串数值导致 INVALID_KLINE。

RED 证据（evidence/R3/red-8b-9-probe.json）：
- 688018/300045 的 `pro_bar`(qfq) 原始行把 OHLC/vol/amount 返回为**字符串**
  （"140.33"、"174726.95"、"2551454.10"），主板 003021 同一接口返回数值。
- 适配器只接受 `int/float`：`type(row.get(k)) not in (int, float)` → 整表
  `INVALID_KLINE`，公共 query 落到腾讯后备又因缺 amount 报
  `QUALITY_KLINE_FIELDS` → 用户看到「日K全 gap」。
- bots #3/#9/#18 的 daily(qfq) 10-12~10-26 区间请求正是这条链。

本文件锁定：
1. pro_bar 字符串数值必须按数值解析（不能 INVALID_KLINE），仍保留严格拒绝非法值；
2. 同票同参数 count 路径与 start/end 区间路径结果一致（尾部对齐）；
3. end_date 为今天盘中时（clock 固定盘中）两路仍一致；
4. pro_bar 与 daily 同口径：vol 手→×100、amount 千元→×1000。
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

# 真实 vendor 形状：qfq 前复权行把数值给成字符串（688018/300045 实测）。
STRING_PRO_BAR_ROWS = [
    {"ts_code": "688018.SH", "trade_date": "20261023", "freq": "D",
     "open": "140.33", "high": "156.24", "low": "136.31", "close": "154.36",
     "pre_close": "141.36", "change": "13.00", "pct_chg": "9.20",
     "vol": "174726.95", "amount": "2551454.10", "adj": "qfq",
     "adj_factor": "2.822800", "ma5": "140.54", "ma10": None},
    {"ts_code": "688018.SH", "trade_date": "20261022", "freq": "D",
     "open": "145.13", "high": "146.99", "low": "141.04", "close": "141.36",
     "pre_close": "145.86", "change": "-4.50", "pct_chg": "-3.09",
     "vol": "90870.21", "amount": "1304034.25", "adj": "qfq",
     "adj_factor": "2.822800", "ma5": "133.36", "ma10": None},
    {"ts_code": "688018.SH", "trade_date": "20261021", "freq": "D",
     "open": "142.24", "high": "148.80", "low": "140.02", "close": "145.86",
     "pre_close": "140.72", "change": "5.14", "pct_chg": "3.65",
     "vol": "166799.47", "amount": "2427620.49", "adj": "qfq",
     "adj_factor": "2.822800", "ma5": None, "ma10": None},
]
FLOAT_DAILY_ROWS = [
    {"ts_code": "003021.SZ", "trade_date": "20261026", "open": 93.0, "high": 98.01,
     "low": 91.12, "close": 96.26, "pre_close": 91.73, "vol": 269865.25,
     "amount": 2540133.63664},
    {"ts_code": "003021.SZ", "trade_date": "20261023", "open": 85.0, "high": 91.73,
     "low": 85.0, "close": 91.73, "pre_close": 83.39, "vol": 261792.25,
     "amount": 2350922.64971},
]


class StockTodayQfqBarTests(unittest.TestCase):
    def _provider(self, now: datetime):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = Mock()
        self.response = Mock(status_code=200)
        self.transport.return_value = self.response
        return StockTodayProvider(
            token_loader=lambda: SECRET, post=self.transport,
            budget_path=Path(self.tmp.name) / "budget.sqlite3",
            clock=Mock(time=lambda: now.timestamp(), monotonic=lambda: 1000.0),
        )

    def _call(self, provider, *, rows, period="daily", adjustment="qfq", count=None,
              start_date=None, end_date=None):
        self.response.json.return_value = {"code": 0, "columns": list(rows[0]),
                                           "data": rows, "total": len(rows)}
        params = {"code": rows[0]["ts_code"].split(".")[0], "period": period,
                  "adjustment": adjustment, "source": "stocktoday"}
        if count is not None:
            params["count"] = count
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        return provider.call("stock_kline", params)

    def test_qfq_string_numeric_rows_are_parsed_not_invalid(self):
        provider = self._provider(datetime(2026, 10, 26, 16, 0, tzinfo=TZ_SHANGHAI))
        outcome = self._call(provider, rows=STRING_PRO_BAR_ROWS, count=3)
        self.assertNotEqual("INVALID_KLINE", outcome.error_code)
        bars = outcome.data["bars"]
        self.assertEqual(3, len(bars))
        by_date = {bar["datetime"]: bar for bar in bars}
        latest = by_date["2026-10-23"]
        self.assertEqual(154.36, latest["close"])
        self.assertEqual(17_472_695.0, latest["volume"])
        self.assertAlmostEqual(2_551_454_100.0, latest["amount"], places=3)
        self.assertEqual("share", outcome.data["volume_unit"])
        self.assertEqual("CNY", outcome.data["amount_unit"])

    def test_non_numeric_ohlc_is_still_rejected(self):
        provider = self._provider(datetime(2026, 10, 26, 16, 0, tzinfo=TZ_SHANGHAI))
        broken = dict(STRING_PRO_BAR_ROWS[0])
        broken["close"] = "N/A"
        outcome = self._call(provider, rows=[broken], count=1)
        self.assertEqual("INVALID_KLINE", outcome.error_code)

    def test_count_path_and_range_path_agree(self):
        provider = self._provider(datetime(2026, 10, 26, 16, 0, tzinfo=TZ_SHANGHAI))
        by_count = self._call(provider, rows=STRING_PRO_BAR_ROWS, count=2)
        by_range = self._call(provider, rows=STRING_PRO_BAR_ROWS,
                              start_date="20261012", end_date="20261026")
        self.assertEqual(2, len(by_count.data["bars"]))
        self.assertEqual(3, len(by_range.data["bars"]))
        self.assertEqual(by_range.data["bars"][-2:], by_count.data["bars"])

    def test_today_end_date_intraday_is_consistent(self):
        # clock 固定在盘中 10:05：count 路径不带 end_date 时用 today，
        # 区间路径显式 end_date=20261026，两路必须一致。
        provider = self._provider(datetime(2026, 10, 26, 10, 5, tzinfo=TZ_SHANGHAI))
        by_count = self._call(provider, rows=STRING_PRO_BAR_ROWS, count=2)
        by_range = self._call(provider, rows=STRING_PRO_BAR_ROWS,
                              start_date="20261020", end_date="20261026")
        self.assertNotEqual("INVALID_KLINE", by_count.error_code)
        self.assertNotEqual("INVALID_KLINE", by_range.error_code)
        self.assertEqual(by_range.data["bars"][-2:], by_count.data["bars"])

    def test_float_rows_from_other_vendor_shape_still_work(self):
        provider = self._provider(datetime(2026, 10, 26, 16, 0, tzinfo=TZ_SHANGHAI))
        outcome = self._call(provider, rows=FLOAT_DAILY_ROWS, adjustment="none", count=2)
        self.assertNotEqual("INVALID_KLINE", outcome.error_code)
        by_date = {bar["datetime"]: bar for bar in outcome.data["bars"]}
        self.assertEqual(26_986_525.0, by_date["2026-10-26"]["volume"])
        self.assertAlmostEqual(2_540_133_636.64, by_date["2026-10-26"]["amount"], places=3)
        self.assertEqual(26_179_225.0, by_date["2026-10-23"]["volume"])


if __name__ == "__main__":
    unittest.main()
