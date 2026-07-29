import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ym_stock_data.api as api
from ym_stock_data import query
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome


class StaticProvider:
    def __init__(self, name, data):
        self.name = name
        self.data = data

    def call(self, intent, params):
        return ProviderOutcome(
            provider=self.name,
            status="success" if self.data else "empty",
            data=self.data,
            latency_ms=1,
            quality={"status": "normal", "returned_count": 999, "reason_codes": []},
        )


class CanonicalQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        state = ProviderState(Path(self.temp_dir.name) / "providers.sqlite3")
        state_patch = patch.object(api, "_STATE", state)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def test_default_breadth_keeps_sentiment_aggregates_and_partial_quality(self):
        breadth = {
            "涨停": 72,
            ">7%": 31,
            "5~7%": 64,
            "3~5%": 180,
            "0~3%": 2600,
            "-0~-3%": 1800,
            "-3~-5%": 210,
            "-5~-7%": 80,
            "<-7%": 45,
            "跌停": 12,
            "_total": 5094,
        }
        with patch("ym_stock_data.providers.local.pytdx.fetch_breadth", return_value=breadth):
            result = query("review_sentiment")

        self.assertEqual(2947, result["data"]["上涨家数"])
        self.assertEqual(2147, result["data"]["下跌家数"])
        self.assertEqual(72, result["data"]["涨停家数"])
        self.assertEqual(12, result["data"]["跌停家数"])
        self.assertEqual(57.85, result["data"]["红盘率"])
        self.assertIn("query_summary", result["data"])
        self.assertIn("aggregates", result["data"])
        self.assertEqual("partial", result["_meta"]["quality"]["status"])
        self.assertIn("炸板率", result["_meta"]["quality"]["missing"])

    def test_pytdx_internal_eastmoney_breadth_fallback_keeps_bins_and_provenance(self):
        breadth = {
            "涨停": 72,
            ">7%": 31,
            "5~7%": 64,
            "3~5%": 180,
            "0~3%": 2600,
            "-0~-3%": 1800,
            "-3~-5%": 210,
            "-5~-7%": 80,
            "<-7%": 45,
            "跌停": 12,
            "_total": 5094,
            "_source": "eastmoney_fallback",
        }
        with patch(
            "ym_stock_data.providers.local.pytdx.fetch_breadth",
            return_value=breadth,
        ):
            result = query("review_sentiment")

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("eastmoney_breadth", result["_meta"]["provider_used"])
        self.assertEqual(
            ["pytdx_breadth", "eastmoney_breadth"],
            result["_meta"]["source_chain"],
        )
        self.assertEqual(
            ["provider_error", "success"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )
        self.assertEqual(5094, result["data"]["aggregates"]["breadth"]["_total"])
        self.assertEqual(2947, result["data"]["上涨家数"])
        self.assertEqual(2147, result["data"]["下跌家数"])

    def test_snapshot_quality_reports_partial_coverage_and_missing_codes(self):
        provider = StaticProvider("pytdx", {"600519": {"price": 1400}})
        with patch.object(api, "_provider_for", return_value=provider):
            result = query("stock_snapshot", codes=["600519", "000858"])

        quality = result["_meta"]["quality"]
        self.assertEqual("partial", quality["status"])
        self.assertEqual(0.5, quality["coverage"])
        self.assertEqual(["000858"], quality["missing"])
        self.assertIn("coverage_shortfall", quality["reason_codes"])

    def test_sector_quality_reports_partial_name_coverage(self):
        sector = {"code": "881160", "name": "国防军工", "change_pct": 1.2}
        provider = StaticProvider(
            "ths_industry",
            {"items": [sector], "missing": ["商业航天"]},
        )
        with patch.object(api, "_provider_for", return_value=provider):
            result = query(
                "sector_index",
                names=["国防军工", "商业航天"],
            )

        quality = result["_meta"]["quality"]
        self.assertEqual("partial", quality["status"])
        self.assertEqual(0.5, quality["coverage"])
        self.assertEqual(["商业航天"], quality["missing"])
        self.assertEqual("exact", quality["semantic_equivalence"])

    def test_kline_internal_fallback_keeps_semantic_degradation(self):
        raw = {
            "code": "600519",
            "bars": [{"time": "2026-07-29", "close": 1400, "amount": None}],
            "_source": "tencent_fallback",
            "_meta": {"fallback_from": "pytdx", "fallback_to": "tencent"},
        }
        with patch("ym_stock_data.providers.local.pytdx.fetch_kline", return_value=raw):
            result = query("stock_kline", code="600519", count=1)

        quality = result["_meta"]["quality"]
        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("partial", quality["status"])
        self.assertEqual("unknown", quality["semantic_equivalence"])
        self.assertIn("fallback_source", quality["reason_codes"])
        self.assertIn("amount", quality["missing"])

    def test_explicit_review_keeps_shape_quality_summary_and_aggregates(self):
        provider = StaticProvider(
            "iwencai_openapi",
            {
                "datas": [
                    {"股票代码": "600001", "今日涨跌幅": "3.0"},
                    {"股票代码": "600002", "今日涨跌幅": "-1.0"},
                ],
                "row_count": 2,
            },
        )
        with patch.object(api, "_provider_for", return_value=provider):
            result = query(
                "review_sentiment",
                query="昨日涨停 今日涨跌幅 非st",
                expected_row_shape="stock_rows",
                expected_count=2,
            )

        self.assertEqual(1.0, result["data"]["涨停收益均值"])
        self.assertEqual(50.0, result["data"]["红盘率"])
        self.assertEqual("normal", result["_meta"]["quality"]["status"])
        self.assertEqual(1.0, result["_meta"]["quality"]["coverage"])
        self.assertEqual("normal", result["data"]["query_summary"]["batch_status"])

    def test_empty_limit_pool_has_consistent_empty_result_and_quality(self):
        empty_pool = {
            "zt_count": 0,
            "zb_count": 0,
            "dt_count": 0,
            "break_rate": 0.0,
            "max_board": 0,
            "pools": {"zt": [], "zb": [], "dt": []},
        }
        provider = StaticProvider("eastmoney_limit_pool", empty_pool)
        with patch.object(api, "_provider_for", return_value=provider):
            result = query("market_limit_state")

        self.assertEqual("empty", result["_meta"]["status"])
        self.assertEqual("empty", result["_meta"]["quality"]["status"])
        self.assertEqual(0, result["_meta"]["quality"]["returned_count"])


if __name__ == "__main__":
    unittest.main()
