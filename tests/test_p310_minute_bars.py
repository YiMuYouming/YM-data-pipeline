"""P10/#6#8/D27-05：1 分钟线盘中契约。

改前 RED（2026-10-27 真实会话）：
1. 盘中默认只回最近 48 根（近 09:36 起），缺开盘段，且没有截断标记；
2. 未完成的末根照常返回且没有 incomplete 标记（末桶时间还晚于抓取时间）；
3. 分钟 K 在 _meta.freshness 里标成 historical 而不是盘中；
4. 腾讯分时给的是每分钟采样价，O=H=L=C，但没有元数据说明"采样价，不是区间高低"。

本测试钉住：
- 盘中默认返回当日 09:30 起的全部已完成分钟（默认 count=None，不再 48 根）；
- 截断时 truncated=true + total_bars/returned_bars/dropped_bars；
- 未完成末根标 incomplete=true，且 last_close/MA 按已完成分钟；
- 09:30 之前的集合竞价采样被剔除并计数；
- 腾讯行带 bar_semantics（采样价/右端桶标签）元数据；
- _meta.freshness 盘中为 intraday，收盘后仍按最近已完成交易日；data_as_of 取
  已完成末根，不取未完成桶。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ym_stock_data import api
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.local import LocalProvider

DAY = "2026-10-27"


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ_SHANGHAI)


def tencent_bars(start="09:25", end="10:20", price=16.0):
    """腾讯分时的形状：每分钟一个采样价，O=H=L=C 全等于采样价。"""
    began = at(f"{DAY}T{start}:00")
    finished = at(f"{DAY}T{end}:00")
    bars = []
    stamp = began
    step = 0
    while stamp <= finished:
        current = round(price + step * 0.01, 4)
        bars.append({"time": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                     "open": current, "high": current, "low": current, "close": current,
                     "vol": 1000.0, "amount": current * 1000.0,
                     "vwap": current, "avg_price": current})
        stamp += timedelta(minutes=1)
        step += 1
    return bars


class MinuteBarTests(unittest.TestCase):
    def run_kline(self, params, now="2026-10-27T10:20:30", bars=None):
        source = tencent_bars() if bars is None else bars
        with patch("ym_stock_data.sources.pytdx._fetch_tencent_kline",
                   return_value=source) as fetch, \
                patch("ym_stock_data.providers.local._now", return_value=at(now)):
            data = LocalProvider._http_kline(provider="tencent", params={
                "code": "603716", "period": "1m", "adjustment": "none",
                "start_date": DAY.replace("-", ""), "end_date": DAY.replace("-", ""),
                **params})
        return data, fetch

    def test_default_returns_every_completed_minute_not_48_bars(self):
        data, fetch = self.run_kline({})
        self.assertFalse(fetch.call_args.kwargs["count"],
                         "1m 默认不能再用 48 根的隐式截断")
        # 09:30-10:20 共 51 根，加 09:25 竞价 1 根（会被剔除）。
        self.assertEqual(51, len(data["bars"]))
        self.assertEqual("2026-10-27 09:30:00", data["bars"][0]["datetime"])
        self.assertFalse(data["truncated"])
        self.assertGreaterEqual(data["total_bars"], len(data["bars"]))

    def test_unfinished_last_bucket_is_marked_incomplete(self):
        data, _ = self.run_kline({})
        # now=10:20:30 → 10:20 这一分钟还没走完（10:21 才收）。
        self.assertTrue(data["bars"][-1]["incomplete"])
        self.assertEqual("2026-10-27 10:20:00", data["bars"][-1]["datetime"])
        self.assertEqual(1, data["incomplete_bars"])
        self.assertEqual(50, data["completed_bars"])
        # 已完成末根才是 last_close，MA 也只用已完成分钟。
        self.assertEqual(data["bars"][-2]["close"], data["last_close"])
        self.assertEqual({"label": "bucket_start"}, {
            k: v for k, v in data["bar_semantics"].items() if k == "label"})
        self.assertIn("incomplete", data["bar_semantics"]["incomplete_rule"])

    def test_count_request_marks_truncated_with_totals(self):
        data, _ = self.run_kline({"count": 10})
        self.assertEqual(10, len(data["bars"]))
        self.assertTrue(data["truncated"])
        self.assertEqual(51, data["total_bars"])
        self.assertEqual(10, data["returned_bars"])
        self.assertEqual(41, data["dropped_bars"])

    def test_pre_session_samples_are_dropped_and_counted(self):
        data, _ = self.run_kline({})
        # 09:25–09:29 的集合竞价采样（5 根）全部剔除，只留 09:30 起的连续竞价。
        self.assertEqual(5, data["pre_session_bars_dropped"])
        self.assertTrue(all(bar["datetime"][11:16] >= "09:30" for bar in data["bars"]))

    def test_tencent_sample_price_is_documented_not_interval_high_low(self):
        data, _ = self.run_kline({})
        bar = data["bars"][0]
        self.assertEqual(bar["open"], bar["high"])
        self.assertEqual(bar["high"], bar["low"])
        semantics = data["bar_semantics"]
        self.assertEqual("tencent_minute_sample", semantics["price_kind"])
        self.assertIn("采样价", semantics["high_low_note"])
        self.assertIn("不是", semantics["high_low_note"])

    def test_after_close_nothing_is_incomplete(self):
        data, _ = self.run_kline({}, now="2026-10-27T15:30:00")
        self.assertEqual(0, data["incomplete_bars"])
        self.assertEqual(len(data["bars"]), data["completed_bars"])


class ProviderNeutralAnnotationTests(unittest.TestCase):
    """窗口/完成度标注对非腾讯分钟源（如 TDX 真实 OHLC）同样生效。"""

    def test_pytdx_style_bars_get_window_and_incomplete_marker(self):
        bars = [{"datetime": f"2026-10-27 09:{minute:02d}", "open": 16.0,
                 "high": 16.2, "low": 15.9, "close": 16.1, "volume": 1000.0,
                 "amount": 16000.0} for minute in range(30, 59)]
        from ym_stock_data.providers.local import annotate_minute_bars
        annotated = annotate_minute_bars({"bars": bars}, {"period": "1m"}, source="pytdx",
                                         now=at("2026-10-27T10:00:30"))
        self.assertEqual(29, annotated["completed_bars"])
        self.assertEqual(0, annotated["incomplete_bars"])
        self.assertEqual("bar_ohlc", annotated["bar_semantics"]["price_kind"])


class FreshnessTests(unittest.TestCase):
    def stamp(self, text):
        return at(text)

    def test_intraday_minute_kline_freshness_is_intraday(self):
        with patch.object(api, "_now_shanghai", return_value=at("2026-10-27T10:20:00")):
            freshness = api._data_freshness(
                "stock_kline", {"period": "1m", "start_date": "20261027",
                                "end_date": "20261027"},
                self.stamp("2026-10-27T10:20:00"), 300)
        self.assertEqual("intraday", freshness["status"])
        self.assertEqual("same_session_minutes", freshness.get("window"))

    def test_explicit_historical_minute_query_stays_historical(self):
        with patch.object(api, "_now_shanghai", return_value=at("2026-10-27T10:20:00")):
            freshness = api._data_freshness(
                "stock_kline", {"period": "1m", "start_date": "20261024",
                                "end_date": "20261024"},
                self.stamp("2026-10-24T10:20:00"), 300)
        self.assertEqual("historical", freshness["status"])

    def test_data_as_of_ignores_incomplete_last_bucket(self):
        data = {"bars": [
            {"datetime": "2026-10-27 10:19:00", "close": 16.1},
            {"datetime": "2026-10-27 10:20:00", "close": 16.2, "incomplete": True},
        ]}
        stamp = api._data_as_of("stock_kline", {"period": "1m"}, data)
        self.assertEqual(at("2026-10-27T10:19:00"), stamp)


if __name__ == "__main__":
    unittest.main()
