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


if __name__ == "__main__":
    unittest.main()
