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
    def test_realtime_market_wraps_v1_fetch_and_adds_meta(self):
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

        with patch("ym_stock_data.v2.adapters.fetch", return_value=raw) as fetch:
            result = resolve("realtime_market", _now=ts("2026-06-03T09:30:20+08:00"))

        fetch.assert_called_once_with("index")
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

        with patch("ym_stock_data.v2.adapters.fetch", return_value=raw):
            result = resolve("realtime_market", _now=ts("2026-06-03T09:31:10+08:00"))

        self.assertEqual(result["_meta"]["confidence"], "stale")
        self.assertIn("超过阈值", result["_meta"]["warn"])
        self.assertGreater(result["_meta"]["age_sec"], result["_meta"]["staleness_sec"])

    def test_review_sentiment_runs_unique_policy_queries(self):
        from ym_stock_data.v2 import resolve

        def fake_fetch(data_type, **kwargs):
            return {
                "datas": [{"query": kwargs["query"], "value": 1}],
                "row_count": 1,
                "_source": "openapi",
                "_meta": {
                    "data_type": data_type,
                    "source": "iwencai",
                    "fetched_at": "2026-06-03T15:10:00+08:00",
                },
            }

        with patch("ym_stock_data.v2.adapters.fetch", side_effect=fake_fetch) as fetch:
            result = resolve("review_sentiment", _now=ts("2026-06-03T15:15:00+08:00"))

        queries = [call.kwargs["query"] for call in fetch.call_args_list]
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

        with patch("ym_stock_data.v2.adapters.fetch", return_value=raw) as fetch:
            result = resolve("review_sentiment", query="昨日涨停 今日涨跌幅 非st", _now=ts("2026-06-03T15:15:00+08:00"))

        fetch.assert_called_once_with("iwencai", query="昨日涨停 今日涨跌幅 非st", limit=50)
        self.assertEqual(result["data"]["query_count"], 1)
        self.assertEqual(result["_meta"]["queries"], ["昨日涨停 今日涨跌幅 非st"])

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

        with patch("ym_stock_data.v2.adapters.fetch", return_value=raw):
            result = resolve("realtime_market", _now=ts("2026-06-03T09:30:20+08:00"))

        self.assertEqual(result["_meta"]["source_chain"], ["pytdx", "eastmoney", "eastmoney_fallback"])

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


if __name__ == "__main__":
    unittest.main()
