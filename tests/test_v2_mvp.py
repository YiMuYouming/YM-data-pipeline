"""v2.0 MVP tests for the sidecar data pipeline."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.provider_state import ProviderState
import ym_stock_data.api as api

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TZ_SH = timezone(timedelta(hours=8))


def iwencai_outcome(raw):
    rows = raw.get("datas", []) if isinstance(raw, dict) else []
    return ProviderOutcome(
        provider="iwencai_openapi",
        status="success" if rows else "empty",
        data=raw,
        fetched_at=(raw.get("_meta", {}) or {}).get("fetched_at"),
        latency_ms=1,
    )


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TZ_SH)


def fresh_timestamp() -> str:
    return datetime.now(TZ_SH).isoformat(timespec="seconds")


def canonical_result(
    intent,
    data,
    *,
    provider="stocktoday",
    source_chain=None,
    status="success",
    fetched_at=None,
    attempts=None,
    quality_status=None,
):
    chain = list(source_chain or [provider])
    if attempts is None:
        attempts = [
            {
                "provider": name,
                "status": "success" if name == provider else "provider_error",
                "error_code": None if name == provider else "UPSTREAM",
                "latency_ms": 1,
            }
            for name in chain
        ]
    meta = {
        "contract_version": "1.0",
        "intent": intent,
        "status": status,
        "provider_used": provider if status in {"success", "degraded"} else None,
        "source": provider if status in {"success", "degraded"} else None,
        "source_chain": chain,
        "attempts": attempts,
        "fetched_at": fetched_at or fresh_timestamp(),
        "quality": {
            "status": quality_status or ("error" if status == "error" else "normal"),
            "returned_count": 0 if data is None else 1,
            "reason_codes": [],
        },
    }
    return {"data": data, "_meta": meta}


def canonical_snapshot(*codes):
    return {
        code: {
            "code": code,
            "price": 31.2,
            "last_close": 30.2,
            "open": 30.5,
            "high": 31.5,
            "low": 30.0,
            "volume": 1200,
            "amount": 37200,
            "quote_time": fresh_timestamp(),
        }
        for code in codes
    }


def canonical_kline(*, datetime_value="2026-06-04 15:00:00", adjustment="none"):
    return {
        "code": "002475",
        "period": "daily",
        "adjustment": adjustment,
        "volume_unit": "share",
        "amount_unit": "CNY",
        "bars": [{
            "datetime": datetime_value,
            "open": 30.0,
            "high": 32.0,
            "low": 29.8,
            "close": 31.2,
            "volume": 1000,
            "amount": 31200,
        }],
    }


class V2MvpTests(unittest.TestCase):
    def setUp(self):
        self.provider_state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.provider_state_dir.cleanup)
        state = ProviderState(Path(self.provider_state_dir.name) / "providers.sqlite3")
        state_patch = patch.object(api, "_STATE", state)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def test_v2_adapter_delegates_to_canonical_router_exactly_once(self):
        from ym_stock_data.v2 import adapters

        canonical = {
            "data": {"上证指数": 3020.1, "深证指数": 10000.0, "创业指数": 2000.0},
            "_meta": {
                "contract_version": "1.0",
                "status": "success",
                "provider_used": "stocktoday",
                "source": "stocktoday",
                "source_chain": ["stocktoday"],
                "attempts": [{
                    "provider": "stocktoday",
                    "status": "success",
                    "error_code": None,
                    "latency_ms": 1,
                }],
            },
        }
        with patch.object(adapters.public_api, "query", return_value=canonical) as query_call:
            result = adapters.fetch_index()

        query_call.assert_called_once_with("realtime_market")
        self.assertEqual(3020.1, result["上证指数"])
        self.assertEqual("stocktoday", result["_meta"]["provider_used"])

    def test_v2_adapter_has_no_direct_source_or_retry_owner(self):
        from ym_stock_data.v2 import adapters

        self.assertFalse(hasattr(adapters, "_pytdx_call"))
        self.assertFalse(hasattr(adapters, "pytdx"))
        self.assertFalse(hasattr(adapters, "iwencai"))

    def test_realtime_market_projects_full_canonical_three_index_result(self):
        from ym_stock_data.v2 import resolve

        result_data = {
            "上证指数": 3020.1,
            "深证指数": 10020.5,
            "创业指数": 2012.7,
            "成交额": 8112.5,
        }
        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            return_value=canonical_result("realtime_market", result_data),
        ) as query_call:
            result = resolve("realtime_market", _now=ts("2026-06-03T09:30:20+08:00"))

        query_call.assert_called_once_with("realtime_market")
        self.assertEqual(result["data"]["上证指数"], 3020.1)
        self.assertEqual(result["data"]["深证指数"], 10020.5)
        self.assertEqual(result["data"]["创业指数"], 2012.7)
        self.assertEqual(result["_meta"]["intent"], "realtime_market")
        self.assertEqual(result["_meta"]["source"], "stocktoday")
        self.assertEqual(result["_meta"]["source_chain"], ["stocktoday"])
        self.assertEqual(result["_meta"]["confidence"], "normal")
        self.assertFalse(result["_meta"]["error"])

    def test_realtime_market_marks_stale_data(self):
        from ym_stock_data.v2 import resolve

        attempts = [
            {
                "provider": "stocktoday",
                "status": "quality_failure",
                "error_code": "QUALITY_STALE",
                "latency_ms": 1,
            }
        ]
        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            return_value=canonical_result(
                "realtime_market",
                None,
                status="error",
                fetched_at="2026-06-03T09:30:00+08:00",
                attempts=attempts,
            ),
        ) as query_call:
            result = resolve("realtime_market", _now=ts("2026-06-03T09:31:10+08:00"))

        query_call.assert_called_once_with("realtime_market")
        self.assertEqual("error", result["_meta"]["confidence"])
        self.assertTrue(result["_meta"]["error"])
        self.assertEqual("QUALITY_STALE", result["_meta"]["attempts"][0]["error_code"])

    def test_review_sentiment_default_delegates_to_canonical_breadth_query(self):
        from ym_stock_data.v2 import resolve

        data = {
            "上涨家数": 2947,
            "下跌家数": 2147,
            "涨停家数": 72,
            "跌停家数": 12,
            "红盘率": 57.85,
            "query_count": 1,
        }
        canonical = canonical_result(
            "review_sentiment", data,
            provider="pytdx_breadth", source_chain=["pytdx_breadth"],
            quality_status="partial",
        )
        canonical["_meta"]["data_scope"] = "A股市场宽度与涨跌停聚合口径"
        with patch(
            "ym_stock_data.v2.resolve.public_api.query", return_value=canonical
        ) as query_call:
            result = resolve("review_sentiment", _now=ts("2026-06-03T15:15:00+08:00"))

        query_call.assert_called_once_with("review_sentiment")
        self.assertEqual(result["data"]["query_count"], 1)
        self.assertEqual(result["data"]["涨停家数"], 72)
        self.assertEqual(result["data"]["跌停家数"], 12)
        self.assertEqual(result["data"]["上涨家数"], 2947)
        self.assertEqual(result["data"]["下跌家数"], 2147)
        self.assertAlmostEqual(result["data"]["红盘率"], 57.85)
        self.assertEqual(result["_meta"]["intent"], "review_sentiment")
        self.assertEqual(result["_meta"]["source"], "pytdx_breadth")
        self.assertEqual(result["_meta"]["source_chain"], ["pytdx_breadth"])
        self.assertEqual(result["_meta"]["quality"]["status"], "partial")

    def test_review_sentiment_allows_single_query_override(self):
        from ym_stock_data.v2 import resolve

        query_text = "昨日涨停 今日涨跌幅 非st"
        data = {"queries": [{"query": query_text, "result": {"datas": [{"股票简称": "测试股份", "涨跌幅": 3.2}]}}]}
        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            return_value=canonical_result("review_sentiment", data),
        ) as query_call:
            result = resolve("review_sentiment", query=query_text, _now=ts("2026-06-03T15:15:00+08:00"))

        query_call.assert_called_once_with("review_sentiment", query=query_text)
        self.assertEqual(result["data"], data)
        self.assertEqual(result["_meta"]["queries"], [query_text])

    def test_review_sentiment_batch_calls_canonical_query_once_per_query(self):
        from ym_stock_data.v2 import resolve

        queries = ["板块涨幅前2", "板块跌幅前2"]
        def canonical(query_text):
            return canonical_result(
                "review_sentiment",
                {"queries": [{"query": query_text, "result": {"datas": [{"板块名称": query_text}]}}]},
            )
        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            side_effect=lambda _intent, query, **_kwargs: canonical(query),
        ) as query_call:
            result = resolve("review_sentiment", query=queries, _now=ts("2026-06-03T15:15:00+08:00"))

        self.assertEqual([call.args for call in query_call.call_args_list], [
            ("review_sentiment",), ("review_sentiment",),
        ])
        self.assertEqual([call.kwargs["query"] for call in query_call.call_args_list], queries)
        self.assertEqual(queries, result["_meta"]["queries"])
        self.assertEqual(2, result["data"]["query_count"])

    def test_stock_snapshot_projects_canonical_complete_quote(self):
        from ym_stock_data.v2 import resolve

        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            return_value=canonical_result("stock_snapshot", canonical_snapshot("002475")),
        ) as query_call:
            result = resolve("stock_snapshot", codes=["002475"], _now=ts("2026-06-04T09:45:20+08:00"))

        query_call.assert_called_once_with("stock_snapshot", codes=["002475"])
        self.assertEqual(result["data"]["002475"]["price"], 31.2)
        self.assertEqual(result["_meta"]["intent"], "stock_snapshot")
        self.assertEqual(result["_meta"]["source"], "stocktoday")
        self.assertEqual(result["_meta"]["source_chain"], ["stocktoday"])
        self.assertEqual(result["_meta"]["confidence"], "normal")
        self.assertFalse(result["_meta"]["error"])

    def test_stock_snapshot_requires_codes(self):
        from ym_stock_data.v2 import resolve

        with self.assertRaisesRegex(ValueError, "codes"):
            resolve("stock_snapshot")

    def test_stock_snapshot_marks_stale_quotes(self):
        from ym_stock_data.v2 import resolve

        rejected = canonical_result(
            "stock_snapshot",
            None,
            status="error",
            fetched_at="2026-06-04T09:45:00+08:00",
            attempts=[{
                "provider": "stocktoday",
                "status": "quality_failure",
                "error_code": "QUALITY_SNAPSHOT_STALE",
                "latency_ms": 1,
            }],
        )
        with patch(
            "ym_stock_data.v2.resolve.public_api.query", return_value=rejected
        ) as query_call:
            result = resolve("stock_snapshot", codes=["002475"], _now=ts("2026-06-04T09:46:10+08:00"))

        query_call.assert_called_once_with("stock_snapshot", codes=["002475"])
        self.assertEqual(result["_meta"]["confidence"], "error")
        self.assertTrue(result["_meta"]["error"])
        self.assertEqual("QUALITY_SNAPSHOT_STALE", result["_meta"]["attempts"][0]["error_code"])

    def test_canonical_snapshot_rejection_is_not_flattened_by_v2(self):
        from ym_stock_data.v2 import resolve

        rejected = canonical_result(
            "stock_snapshot", None, status="error",
            attempts=[{
                "provider": "tencent",
                "status": "quality_failure",
                "error_code": "QUALITY_SNAPSHOT_FIELDS",
                "latency_ms": 1,
            }],
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=rejected):
            result = resolve("stock_snapshot", codes=["002475"], _now=ts("2026-06-04T10:00:20+08:00"))

        self.assertIsNone(result["data"])
        self.assertTrue(result["_meta"]["error"])
        self.assertEqual("QUALITY_SNAPSHOT_FIELDS", result["_meta"]["attempts"][0]["error_code"])

    def test_stock_snapshot_promotes_row_level_fallback_provenance(self):
        from ym_stock_data.v2 import resolve

        fallback = canonical_result(
            "stock_snapshot",
            canonical_snapshot("002475"),
            provider="tencent",
            source_chain=["stocktoday", "tencent"],
            status="degraded",
            quality_status="partial",
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=fallback):
            result = resolve(
                "stock_snapshot",
                codes=["002475"],
                _now=ts("2026-06-04T10:00:20+08:00"),
            )

        self.assertEqual(result["_meta"]["source"], "tencent")
        self.assertEqual(result["_meta"]["confidence"], "degraded")
        self.assertEqual(
            result["_meta"]["source_chain"],
            ["stocktoday", "tencent"],
        )

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

        canonical = canonical_result("sector_index", raw, provider="ths_industry")
        canonical["_meta"]["data_scope"] = "同花顺881行业板块指数"
        with patch(
            "ym_stock_data.v2.resolve.public_api.query", return_value=canonical
        ) as query_call:
            result = resolve("sector_index", codes=["881124"], _now=ts("2026-06-04T10:00:20+08:00"))

        query_call.assert_called_once_with("sector_index", codes=["881124"])
        self.assertEqual(result["data"]["items"][0]["code"], "881124")
        self.assertEqual(result["data"]["items"][0]["main_net_inflow_yi"], -14.12)
        self.assertEqual(result["_meta"]["intent"], "sector_index")
        self.assertEqual(result["_meta"]["source"], "ths_industry")
        self.assertEqual(result["_meta"]["data_scope"], "同花顺881行业板块指数")
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

        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            return_value=canonical_result("sector_index", raw, provider="ths_industry"),
        ) as query_call:
            result = resolve("sector_index", names=["消费电子", "通信设备"], _now=ts("2026-06-04T10:00:20+08:00"))

        query_call.assert_called_once_with("sector_index", names=["消费电子", "通信设备"])
        self.assertEqual([item["code"] for item in result["data"]["items"]], ["881124", "881129"])

    def test_sector_index_rejects_non_ths_codes(self):
        from ym_stock_data.v2 import resolve

        with self.assertRaisesRegex(ValueError, "881"):
            resolve("sector_index", codes=["931494"])

    def test_stock_kline_projects_complete_canonical_bars(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "code": "002475",
            "total_bars": 3,
            "last_close": 31.2,
            "mas": {"MA5": 30.4, "MA10": 29.9, "MA20": 28.7},
            "adjustment": "none",
            "volume_unit": "share",
            "amount_unit": "CNY",
            "bars": [
                {"datetime": "2026-06-02 15:00", "open": 30.1, "high": 31.0, "low": 29.8, "close": 30.8, "volume": 1000, "amount": 30800},
                {"datetime": "2026-06-03 15:00", "open": 30.8, "high": 31.5, "low": 30.5, "close": 31.0, "volume": 1200, "amount": 37200},
                {"datetime": "2026-06-04 15:00", "open": 31.0, "high": 31.8, "low": 30.9, "close": 31.2, "volume": 1300, "amount": 40560},
            ],
        }
        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            return_value=canonical_result("stock_kline", raw),
        ) as query_call:
            result = resolve("stock_kline", code="002475", period="daily", _now=ts("2026-06-04T15:01:20+08:00"))

        query_call.assert_called_once_with("stock_kline", code="002475", period="daily")
        self.assertEqual(result["data"]["code"], "002475")
        self.assertNotIn("period", result["data"])
        self.assertEqual(result["data"]["last_close"], 31.2)
        self.assertEqual(result["data"]["mas"]["MA10"], 29.9)
        self.assertEqual(result["_meta"]["intent"], "stock_kline")
        self.assertEqual(result["_meta"]["source"], "stocktoday")
        self.assertEqual(result["_meta"]["source_chain"], ["stocktoday"])
        self.assertEqual(result["_meta"]["confidence"], "normal")
        self.assertFalse(result["_meta"]["error"])

    def test_stock_kline_passes_count_to_canonical_query(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "code": "002475",
            "total_bars": 3,
            "last_close": 31.2,
            "mas": {},
            "adjustment": "none",
            "volume_unit": "share",
            "amount_unit": "CNY",
            "bars": [
                {"datetime": "2026-06-03 15:00", "open": 30.8, "high": 31.5, "low": 30.5, "close": 31.0, "volume": 1200, "amount": 37200},
                {"datetime": "2026-06-04 15:00", "open": 31.0, "high": 31.8, "low": 30.9, "close": 31.2, "volume": 1300, "amount": 40560},
            ],
        }

        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            return_value=canonical_result("stock_kline", raw),
        ) as query_call:
            result = resolve("stock_kline", code="002475", period="15m", count=2, _now=ts("2026-06-04T15:01:20+08:00"))

        query_call.assert_called_once_with("stock_kline", code="002475", period="15m", count=2)
        self.assertEqual([bar["datetime"] for bar in result["data"]["bars"]], ["2026-06-03 15:00", "2026-06-04 15:00"])

    def test_v2_preserves_canonical_kline_quality_rejection(self):
        from ym_stock_data.v2 import resolve

        rejected = canonical_result(
            "stock_kline", None, status="error",
            attempts=[{
                "provider": "tencent",
                "status": "quality_failure",
                "error_code": "QUALITY_KLINE_FIELDS",
                "latency_ms": 1,
            }],
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=rejected):
            result = resolve("stock_kline", code="002475", period="daily", count=2, _now=ts("2026-06-04T15:01:20+08:00"))

        self.assertIsNone(result["data"])
        self.assertEqual("error", result["_meta"]["confidence"])
        self.assertEqual("QUALITY_KLINE_FIELDS", result["_meta"]["attempts"][0]["error_code"])

    def test_stock_kline_requires_code(self):
        from ym_stock_data.v2 import resolve

        with self.assertRaisesRegex(ValueError, "code"):
            resolve("stock_kline")

    def test_stock_kline_rejects_unknown_period(self):
        from ym_stock_data.v2 import resolve

        with self.assertRaisesRegex(ValueError, "period"):
            resolve("stock_kline", code="002475", period="2m")

    def test_stock_kline_rejects_invalid_count(self):
        from ym_stock_data.v2 import resolve

        with self.assertRaisesRegex(ValueError, "count"):
            resolve("stock_kline", code="002475", period="15m", count=0)

    def test_stock_kline_marks_stale_bars(self):
        from ym_stock_data.v2 import resolve

        rejected = canonical_result(
            "stock_kline", None, status="error",
            fetched_at="2026-06-04T15:01:00+08:00",
            attempts=[{
                "provider": "stocktoday",
                "status": "quality_failure",
                "error_code": "QUALITY_STALE",
                "latency_ms": 1,
            }],
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=rejected):
            result = resolve("stock_kline", code="002475", period="daily", _now=ts("2026-06-04T15:07:00+08:00"))

        self.assertEqual(result["_meta"]["confidence"], "error")
        self.assertEqual("QUALITY_STALE", result["_meta"]["attempts"][0]["error_code"])

    def test_stock_kline_marks_http_fallback_as_degraded_scope(self):
        from ym_stock_data.v2 import resolve

        fallback = canonical_result(
            "stock_kline",
            canonical_kline(),
            provider="tencent",
            source_chain=["stocktoday", "eastmoney_stock", "tencent"],
            status="degraded",
            quality_status="partial",
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=fallback):
            result = resolve(
                "stock_kline",
                code="002475",
                period="daily",
                count=1,
                _now=ts("2026-06-04T15:01:20+08:00"),
            )

        self.assertEqual(result["_meta"]["source"], "tencent")
        self.assertEqual(result["_meta"]["confidence"], "degraded")
        self.assertEqual(result["_meta"]["quality"]["status"], "partial")

    def test_source_chain_captures_fallback_metadata(self):
        from ym_stock_data.v2 import resolve

        fallback = canonical_result(
            "realtime_market",
            {"上证指数": 3200.0, "深证指数": 10000.0, "创业指数": 2000.0},
            provider="eastmoney",
            source_chain=["stocktoday", "tencent", "pytdx", "eastmoney"],
            status="degraded",
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=fallback):
            result = resolve("realtime_market", _now=ts("2026-06-03T09:30:20+08:00"))

        self.assertEqual(result["_meta"]["source_chain"], ["stocktoday", "tencent", "pytdx", "eastmoney"])

    def test_review_sentiment_adds_top_level_aggregates(self):
        from ym_stock_data.v2 import resolve

        def fake_query(query_str):
            if query_str == "昨日涨停 今日涨跌幅 非st":
                datas = [{"今日涨跌幅": "3.0"}, {"今日涨跌幅": "-1.0"}, {"今日涨跌幅": "2.0"}]
            elif query_str == "昨日炸板 今日涨跌幅 炸板率 非st":
                datas = [{"炸板率": "25%"}]
            elif query_str == "今日连板 股票简称 连板数 非st":
                datas = [{"股票简称": "测试A", "连板数": 3}, {"股票简称": "测试B", "连续涨停天数[20260604]": "5"}]
            else:
                datas = [{"query": query_str}]
            return canonical_result(
                "review_sentiment",
                {"queries": [{"query": query_str, "result": {"datas": datas}}]},
            )

        queries = [
            "昨日涨停 今日涨跌幅 非st",
            "昨日炸板 今日涨跌幅 炸板率 非st",
            "今日连板 股票简称 连板数 非st",
        ]
        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            side_effect=lambda _intent, query, **_kwargs: fake_query(query),
        ) as query_call:
            result = resolve(
                "review_sentiment",
                query=queries,
                _now=ts("2026-06-03T15:15:00+08:00"),
            )

        self.assertEqual(3, query_call.call_count)
        self.assertEqual(result["data"]["涨停收益均值"], 1.33)
        self.assertEqual(result["data"]["红盘率"], 66.67)
        self.assertEqual(result["data"]["炸板率"], 25.0)
        self.assertEqual(result["data"]["最高板"], 5)
        self.assertEqual(result["data"]["aggregates"]["limit_up_return_avg"], 1.33)

    def test_market_limit_state_is_projected_from_canonical_query(self):
        from ym_stock_data.v2 import resolve

        data = {
            "zt_count": 30,
            "zb_count": 10,
            "dt_count": 5,
            "break_rate": 25.0,
            "max_board": 4,
            "pools": {"zt": [{}], "zb": [{}], "dt": [{}]},
        }
        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            return_value=canonical_result(
                "market_limit_state", data, provider="eastmoney_limit_pool"
            ),
        ) as query_call:
            result = resolve("market_limit_state", date="20260714")

        query_call.assert_called_once_with("market_limit_state", date="20260714")
        self.assertEqual(30, result["data"]["zt_count"])
        self.assertEqual("market_limit_state", result["_meta"]["intent"])
        self.assertEqual(
            ["eastmoney_limit_pool"], result["_meta"]["source_chain"]
        )
        self.assertEqual("normal", result["_meta"]["quality"]["status"])

    def test_market_limit_state_source_error_is_explicit(self):
        from ym_stock_data.v2 import resolve

        failed = canonical_result(
            "market_limit_state", None, provider="eastmoney_limit_pool", status="error"
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=failed):
            result = resolve("market_limit_state", date="20260714")

        self.assertEqual("error", result["_meta"]["quality"]["status"])
        self.assertEqual("error", result["_meta"]["confidence"])

    def test_stock_event_is_projected_from_canonical_query(self):
        from ym_stock_data.v2 import resolve

        data = {
            "event": "lockup",
            "code": "600519",
            "total": 1,
            "items": [{"date": "2026-08-01"}],
        }
        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            return_value=canonical_result("stock_event", data, provider="eastmoney_datacenter"),
        ) as query_call:
            result = resolve("stock_event", event="lockup", code="600519")

        query_call.assert_called_once_with("stock_event", event="lockup", code="600519")
        self.assertEqual(1, result["data"]["total"])
        self.assertEqual("stock_event", result["_meta"]["intent"])
        self.assertEqual("normal", result["_meta"]["quality"]["status"])

    def test_stock_event_empty_result_preserves_canonical_quality(self):
        from ym_stock_data.v2 import resolve

        empty = canonical_result(
            "stock_event", {"event": "lockup", "code": "600519", "total": 0, "items": []},
            provider="eastmoney_datacenter", status="empty", quality_status="empty",
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=empty):
            result = resolve("stock_event", event="lockup", code="600519")

        self.assertEqual("empty", result["_meta"]["quality"]["status"])

    def test_stock_event_requires_event_and_code(self):
        from ym_stock_data.v2 import resolve

        with self.assertRaisesRegex(ValueError, "event"):
            resolve("stock_event", code="600519")
        with self.assertRaisesRegex(ValueError, "code"):
            resolve("stock_event", event="lockup")

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

        sentiment_by_name = {
            item["field"]: item
            for item in fields
            if item["intent"] == "review_sentiment"
        }
        for field in ("涨停家数", "跌停家数"):
            self.assertEqual("pytdx", sentiment_by_name[field]["primary"]["source"])
            self.assertEqual("zero_auth", sentiment_by_name[field]["rate_class"])
            self.assertEqual(item["primary"].get("code_prefix"), "881")

        kline_fields = [item for item in fields if item["intent"] == "stock_kline"]
        self.assertGreaterEqual(len(kline_fields), 6)
        for item in kline_fields:
            self.assertEqual(item["primary"]["source"], "pytdx")
            self.assertNotIn(item["primary"]["source"], forbidden)


if __name__ == "__main__":
    unittest.main()
