"""v2.0 MVP tests for the sidecar data pipeline."""

import json
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TZ_SH = timezone(timedelta(hours=8))


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TZ_SH)


class V2MvpTests(unittest.TestCase):
    def test_realtime_market_calls_source_directly_and_adds_meta(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "上证指数": {"最新价": 3020.1, "涨跌幅": 0.8},
            "成交额": 8112.5,
            "_meta": {
                "data_type": "index",
                "source": "pytdx",
                "fetched_at": "2026-06-03T09:30:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.pytdx.fetch_index", return_value=raw) as fetch_index, \
             patch("ym_stock_data.v2.adapters.fetch_v1", side_effect=AssertionError("v2 must not call v1 fetch route")):
            result = resolve("realtime_market", _now=ts("2026-06-03T09:30:20+08:00"))

        fetch_index.assert_called_once_with()
        self.assertEqual(result["data"]["上证指数"]["最新价"], 3020.1)
        self.assertEqual(result["_meta"]["intent"], "realtime_market")
        self.assertEqual(result["_meta"]["source"], "pytdx")
        self.assertEqual(result["_meta"]["source_chain"], ["pytdx"])
        self.assertEqual(result["_meta"]["data_scope"], "PyTDX实时行情口径")
        self.assertEqual(result["_meta"]["confidence"], "normal")
        self.assertFalse(result["_meta"]["error"])

    def test_realtime_market_marks_stale_data(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "上证指数": {"最新价": 3020.1},
            "_meta": {
                "data_type": "index",
                "source": "pytdx",
                "fetched_at": "2026-06-03T09:30:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.pytdx.fetch_index", return_value=raw), \
             patch("ym_stock_data.v2.adapters.fetch_v1", side_effect=AssertionError("v2 must not call v1 fetch route")):
            result = resolve("realtime_market", _now=ts("2026-06-03T09:31:10+08:00"))

        self.assertEqual(result["_meta"]["confidence"], "stale")
        self.assertIn("超过阈值", result["_meta"]["warn"])
        self.assertGreater(result["_meta"]["age_sec"], result["_meta"]["staleness_sec"])

    def test_review_sentiment_runs_unique_policy_queries(self):
        from ym_stock_data.v2 import resolve

        def fake_fetch(query_str, limit=50):
            return {
                "datas": [{"query": query_str, "limit": limit, "value": 1}],
                "row_count": 1,
                "_source": "openapi",
                "_meta": {
                    "data_type": "iwencai",
                    "source": "iwencai",
                    "fetched_at": "2026-06-03T15:10:00+08:00",
                },
            }

        with patch("ym_stock_data.sources.iwencai.query", side_effect=fake_fetch) as query, \
             patch("ym_stock_data.v2.adapters.fetch_v1", side_effect=AssertionError("v2 must not call v1 fetch route")):
            result = resolve("review_sentiment", _now=ts("2026-06-03T15:15:00+08:00"))

        queries = [call.args[0] for call in query.call_args_list]
        self.assertGreaterEqual(len(queries), 5)
        self.assertEqual(len(queries), len(set(queries)))
        self.assertIn("昨日涨停 今日涨跌幅 非st", queries)
        self.assertIn("今日连板 股票简称 连板数 非st", queries)
        self.assertEqual(result["data"]["query_count"], len(queries))
        self.assertEqual(result["_meta"]["intent"], "review_sentiment")
        self.assertEqual(result["_meta"]["source"], "iwencai")
        self.assertEqual(result["_meta"]["source_chain"], ["iwencai", "openapi"])
        self.assertEqual(result["_meta"]["data_scope"], "问财口径")
        self.assertEqual(result["_meta"]["queries"], queries)

    def test_review_sentiment_allows_single_query_override(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "datas": [{"股票简称": "测试股份", "涨跌幅": 3.2}],
            "row_count": 1,
            "_source": "openapi",
            "_meta": {
                "data_type": "iwencai",
                "source": "iwencai",
                "fetched_at": "2026-06-03T15:10:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.iwencai.query", return_value=raw) as query, \
             patch("ym_stock_data.v2.adapters.fetch_v1", side_effect=AssertionError("v2 must not call v1 fetch route")):
            result = resolve("review_sentiment", query="昨日涨停 今日涨跌幅 非st", _now=ts("2026-06-03T15:15:00+08:00"))

        query.assert_called_once_with("昨日涨停 今日涨跌幅 非st", limit=50)
        self.assertEqual(result["data"]["query_count"], 1)
        self.assertEqual(result["_meta"]["queries"], ["昨日涨停 今日涨跌幅 非st"])

    def test_review_sentiment_matches_v1_iwencai_signature(self):
        from ym_stock_data.v2 import resolve

        def fake_iwencai_query(query_str, limit=50, page=1):
            return {
                "datas": [{"query": query_str, "limit": limit, "page": page}],
                "row_count": 1,
                "_source": "openapi",
            }

        with patch("ym_stock_data.sources.iwencai.query", side_effect=fake_iwencai_query), \
             patch("ym_stock_data.v2.adapters.fetch_v1", side_effect=AssertionError("v2 must not call v1 fetch route")):
            result = resolve("review_sentiment", query="昨日涨停 今日涨跌幅 非st", _now=ts("2026-06-03T15:15:00+08:00"))

        first = result["data"]["queries"][0]["result"]
        self.assertNotIn("error", first)
        self.assertEqual(first["datas"][0]["query"], "昨日涨停 今日涨跌幅 非st")
        for key in ("涨停收益均值", "红盘率", "炸板率", "最高板"):
            self.assertIn(key, result["data"])

    def test_stock_snapshot_calls_source_directly_and_adds_meta(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "002475": {
                "最新价": 31.2,
                "涨幅": "+3.18%",
                "量比": "1.42",
                "换手": "2.10",
                "MA5_d": 30.1,
                "MA10_d": 29.8,
                "MA20_d": 28.6,
                "MA10_60m": 30.4,
                "MA10_60m_dir": "向上",
                "is_strong": True,
            },
            "_meta": {
                "data_type": "quotes",
                "source": "pytdx",
                "fetched_at": "2026-06-04T09:45:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.pytdx.fetch_quotes", return_value=raw) as fetch_quotes, \
             patch("ym_stock_data.v2.adapters.fetch_v1", side_effect=AssertionError("v2 must not call v1 fetch route")):
            result = resolve("stock_snapshot", codes=["002475"], _now=ts("2026-06-04T09:45:20+08:00"))

        fetch_quotes.assert_called_once_with(["002475"])
        self.assertEqual(result["data"]["002475"]["最新价"], 31.2)
        self.assertEqual(result["_meta"]["intent"], "stock_snapshot")
        self.assertEqual(result["_meta"]["source"], "pytdx")
        self.assertEqual(result["_meta"]["source_chain"], ["pytdx"])
        self.assertEqual(result["_meta"]["data_scope"], "PyTDX个股实时行情口径")
        self.assertEqual(result["_meta"]["confidence"], "normal")
        self.assertFalse(result["_meta"]["error"])

    def test_stock_snapshot_requires_codes(self):
        from ym_stock_data.v2 import resolve

        with self.assertRaisesRegex(ValueError, "codes"):
            resolve("stock_snapshot")

    def test_stock_snapshot_marks_stale_quotes(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "002475": {"最新价": 31.2},
            "_meta": {
                "data_type": "quotes",
                "source": "pytdx",
                "fetched_at": "2026-06-04T09:45:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.pytdx.fetch_quotes", return_value=raw), \
             patch("ym_stock_data.v2.adapters.fetch_v1", side_effect=AssertionError("v2 must not call v1 fetch route")):
            result = resolve("stock_snapshot", codes=["002475"], _now=ts("2026-06-04T09:46:10+08:00"))

        self.assertEqual(result["_meta"]["confidence"], "stale")
        self.assertIn("超过阈值", result["_meta"]["warn"])

    def test_sector_index_calls_ths_881_source_by_code(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "items": [{
                "code": "881124",
                "name": "消费电子",
                "change_pct": -0.89,
                "main_net_inflow_yi": -14.12,
            }],
            "by_code": {"881124": {"code": "881124", "name": "消费电子"}},
            "by_name": {"消费电子": {"code": "881124", "name": "消费电子"}},
            "missing": [],
            "_meta": {
                "data_type": "sector_index",
                "source": "ths_industry",
                "fetched_at": "2026-06-04T10:00:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.ths_industry.fetch_sector_index", return_value=raw) as fetch_sector_index, \
             patch("ym_stock_data.sources.pytdx.fetch_sector", side_effect=AssertionError("sector_index must not use TDX sector line")), \
             patch("ym_stock_data.v2.adapters.fetch_v1", side_effect=AssertionError("v2 must not call v1 fetch route")):
            result = resolve("sector_index", codes=["881124"], _now=ts("2026-06-04T10:00:20+08:00"))

        fetch_sector_index.assert_called_once_with(codes=["881124"], names=None)
        self.assertEqual(result["data"]["items"][0]["code"], "881124")
        self.assertEqual(result["data"]["items"][0]["main_net_inflow_yi"], -14.12)
        self.assertEqual(result["_meta"]["intent"], "sector_index")
        self.assertEqual(result["_meta"]["source"], "ths_industry")
        self.assertEqual(result["_meta"]["data_scope"], "同花顺881行业板块口径")
        self.assertEqual(result["_meta"]["confidence"], "normal")

    def test_sector_index_supports_name_lookup(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "items": [
                {"code": "881124", "name": "消费电子", "change_pct": -0.89},
                {"code": "881129", "name": "通信设备", "change_pct": 1.25},
            ],
            "by_code": {},
            "by_name": {},
            "missing": [],
            "_meta": {
                "data_type": "sector_index",
                "source": "ths_industry",
                "fetched_at": "2026-06-04T10:00:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.ths_industry.fetch_sector_index", return_value=raw) as fetch_sector_index:
            result = resolve("sector_index", names=["消费电子", "通信设备"], _now=ts("2026-06-04T10:00:20+08:00"))

        fetch_sector_index.assert_called_once_with(codes=None, names=["消费电子", "通信设备"])
        self.assertEqual([item["code"] for item in result["data"]["items"]], ["881124", "881129"])

    def test_sector_index_rejects_non_ths_codes(self):
        from ym_stock_data.v2 import resolve

        with self.assertRaisesRegex(ValueError, "881"):
            resolve("sector_index", codes=["931494"])

    def test_stock_kline_calls_source_directly_and_adds_meta(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "code": "002475",
            "total_bars": 3,
            "last_close": 31.2,
            "mas": {"MA5": 30.4, "MA10": 29.9, "MA20": 28.7},
            "bars": [
                {"time": "2026-06-02 15:00", "open": 30.1, "high": 31.0, "low": 29.8, "close": 30.8, "vol": 1000},
                {"time": "2026-06-03 15:00", "open": 30.8, "high": 31.5, "low": 30.5, "close": 31.0, "vol": 1200},
                {"time": "2026-06-04 15:00", "open": 31.0, "high": 31.8, "low": 30.9, "close": 31.2, "vol": 1300},
            ],
            "_meta": {
                "data_type": "kline",
                "source": "pytdx",
                "fetched_at": "2026-06-04T15:01:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.pytdx.fetch_kline", return_value=raw) as fetch_kline, \
             patch("ym_stock_data.v2.adapters.fetch_v1", side_effect=AssertionError("v2 must not call v1 fetch route")):
            result = resolve("stock_kline", code="002475", period="daily", _now=ts("2026-06-04T15:01:20+08:00"))

        fetch_kline.assert_called_once_with("002475", period="daily")
        self.assertEqual(result["data"]["code"], "002475")
        self.assertEqual(result["data"]["period"], "daily")
        self.assertEqual(result["data"]["last_close"], 31.2)
        self.assertEqual(result["data"]["mas"]["MA10"], 29.9)
        self.assertEqual(result["_meta"]["intent"], "stock_kline")
        self.assertEqual(result["_meta"]["source"], "pytdx")
        self.assertEqual(result["_meta"]["source_chain"], ["pytdx"])
        self.assertEqual(result["_meta"]["data_scope"], "PyTDX个股K线口径")
        self.assertEqual(result["_meta"]["confidence"], "normal")
        self.assertFalse(result["_meta"]["error"])

    def test_stock_kline_honors_count(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "code": "002475",
            "total_bars": 3,
            "last_close": 31.2,
            "mas": {},
            "bars": [
                {"time": "2026-06-02 15:00", "close": 30.8},
                {"time": "2026-06-03 15:00", "close": 31.0},
                {"time": "2026-06-04 15:00", "close": 31.2},
            ],
            "_meta": {
                "data_type": "kline",
                "source": "pytdx",
                "fetched_at": "2026-06-04T15:01:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.pytdx.fetch_kline", return_value=raw) as fetch_kline:
            result = resolve("stock_kline", code="002475", period="15m", count=2, _now=ts("2026-06-04T15:01:20+08:00"))

        fetch_kline.assert_called_once_with("002475", period="15m")
        self.assertEqual([bar["time"] for bar in result["data"]["bars"]], ["2026-06-03 15:00", "2026-06-04 15:00"])
        self.assertEqual(result["data"]["requested_count"], 2)
        self.assertEqual(result["data"]["returned_bars"], 2)

    def test_stock_kline_requires_code(self):
        from ym_stock_data.v2 import resolve

        with self.assertRaisesRegex(ValueError, "code"):
            resolve("stock_kline")

    def test_stock_kline_rejects_unknown_period(self):
        from ym_stock_data.v2 import resolve

        with self.assertRaisesRegex(ValueError, "period"):
            resolve("stock_kline", code="002475", period="1m")

    def test_stock_kline_rejects_invalid_count(self):
        from ym_stock_data.v2 import resolve

        with self.assertRaisesRegex(ValueError, "count"):
            resolve("stock_kline", code="002475", period="15m", count=0)

    def test_stock_kline_marks_stale_bars(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "code": "002475",
            "last_close": 31.2,
            "bars": [],
            "_meta": {
                "data_type": "kline",
                "source": "pytdx",
                "fetched_at": "2026-06-04T15:01:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.pytdx.fetch_kline", return_value=raw), \
             patch("ym_stock_data.v2.adapters.fetch_v1", side_effect=AssertionError("v2 must not call v1 fetch route")):
            result = resolve("stock_kline", code="002475", period="daily", _now=ts("2026-06-04T15:07:00+08:00"))

        self.assertEqual(result["_meta"]["confidence"], "stale")
        self.assertIn("超过阈值", result["_meta"]["warn"])

    def test_source_chain_captures_fallback_metadata(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "上证指数": {"最新价": 3020.1},
            "_source": "eastmoney_fallback",
            "_meta": {
                "data_type": "index",
                "source": "pytdx",
                "fallback_from": "pytdx",
                "fallback_to": "eastmoney",
                "fetched_at": "2026-06-03T09:30:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.pytdx.fetch_index", return_value=raw), \
             patch("ym_stock_data.v2.adapters.fetch_v1", side_effect=AssertionError("v2 must not call v1 fetch route")):
            result = resolve("realtime_market", _now=ts("2026-06-03T09:30:20+08:00"))

        self.assertEqual(result["_meta"]["source_chain"], ["pytdx", "eastmoney", "eastmoney_fallback"])

    def test_review_sentiment_adds_top_level_aggregates(self):
        from ym_stock_data.v2 import resolve

        def fake_fetch(query_str, limit=50):
            if query_str == "昨日涨停 今日涨跌幅 非st":
                datas = [{"今日涨跌幅": "3.0"}, {"今日涨跌幅": "-1.0"}, {"今日涨跌幅": "2.0"}]
            elif query_str == "昨日炸板 今日涨跌幅 炸板率 非st":
                datas = [{"炸板率": "25%"}]
            elif query_str == "今日连板 股票简称 连板数 非st":
                datas = [{"股票简称": "测试A", "连板数": 3}, {"股票简称": "测试B", "连续涨停天数[20260604]": "5"}]
            else:
                datas = [{"query": query_str}]
            return {
                "datas": datas,
                "row_count": len(datas),
                "_source": "openapi",
                "_meta": {
                    "data_type": "iwencai",
                    "source": "iwencai",
                    "fetched_at": "2026-06-03T15:10:00+08:00",
                },
            }

        with patch("ym_stock_data.sources.iwencai.query", side_effect=fake_fetch):
            result = resolve("review_sentiment", _now=ts("2026-06-03T15:15:00+08:00"))

        self.assertEqual(result["data"]["涨停收益均值"], 1.33)
        self.assertEqual(result["data"]["红盘率"], 66.67)
        self.assertEqual(result["data"]["炸板率"], 25.0)
        self.assertEqual(result["data"]["最高板"], 5)
        self.assertEqual(result["data"]["aggregates"]["limit_up_return_avg"], 1.33)

    def test_fields_policy_covers_critical_fields(self):
        fields_path = Path(__file__).resolve().parents[1] / "ym_stock_data/v2/policies/fields.json"
        fields = json.loads(fields_path.read_text(encoding="utf-8"))

        self.assertGreaterEqual(len(fields), 20)
        for item in fields:
            for key in ("field", "intent", "primary", "fallback", "data_scope", "trade_usage", "staleness_sec", "rate_class"):
                self.assertIn(key, item)

        realtime_fields = [item for item in fields if item["intent"] == "realtime_market"]
        forbidden = {"iwencai", "tdx_mcp", "web"}
        self.assertTrue(realtime_fields)
        for item in realtime_fields:
            self.assertNotIn(item["primary"]["source"], forbidden)

        stock_fields = [item for item in fields if item["intent"] == "stock_snapshot"]
        self.assertGreaterEqual(len(stock_fields), 8)
        for item in stock_fields:
            self.assertNotIn(item["primary"]["source"], forbidden)

        sector_fields = [item for item in fields if item["intent"] == "sector_index"]
        self.assertGreaterEqual(len(sector_fields), 2)
        for item in sector_fields:
            self.assertEqual(item["primary"]["source"], "ths_industry")
            self.assertEqual(item["primary"].get("code_prefix"), "881")

        kline_fields = [item for item in fields if item["intent"] == "stock_kline"]
        self.assertGreaterEqual(len(kline_fields), 6)
        for item in kline_fields:
            self.assertEqual(item["primary"]["source"], "pytdx")
            self.assertNotIn(item["primary"]["source"], forbidden)


if __name__ == "__main__":
    unittest.main()
