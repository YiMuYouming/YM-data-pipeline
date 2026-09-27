import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import ym_stock_data.api as api
from ym_stock_data import query
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.local import LocalProvider
from tests.fixed_clock import FIXED_NOW_ISO, freeze_trading_clock
from ym_stock_data.provider_policy import CompiledPolicy


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
        freeze_trading_clock(self)
        legacy_policy = CompiledPolicy(None, "inactive", None)
        policy_patch = patch.object(api, "load_compiled_policy", return_value=legacy_policy)
        policy_patch.start()
        self.addCleanup(policy_patch.stop)
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
            "indicators": {"indicator_version": "indicators.v1", "emotion": 57.85},
            "trade_date": "20260924",
            "data_as_of": "2026-09-24T15:00:00+08:00",
        }
        with patch.object(api, "_provider_for", return_value=StaticProvider("stocktoday", breadth)):
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
        self.assertEqual("indicators.v1", result["data"]["indicators"]["indicator_version"])
        self.assertEqual("2026-09-24T15:00:00+08:00", result["_meta"]["data_as_of"])

    def test_pytdx_breadth_diagnostic_reports_internal_eastmoney_provenance(self):
        breadth = {
            "涨停": 72,
            "0~3%": 2600,
            "-0~-3%": 1800,
            "跌停": 12,
            "_total": 4484,
            "_source": "eastmoney_fallback",
        }
        with patch(
            "ym_stock_data.providers.local.pytdx.fetch_breadth",
            return_value=breadth,
        ):
            outcome = LocalProvider("pytdx_breadth").call("review_sentiment", {})

        self.assertEqual("eastmoney_breadth", outcome.provider)
        self.assertEqual("pytdx_breadth", outcome.provenance["fallback_from"])
        self.assertEqual(4484, outcome.data["_total"])

    def test_snapshot_quality_rejects_partial_coverage_and_falls_through(self):
        now = FIXED_NOW_ISO
        provider = StaticProvider(
            "tencent",
            {
                "600519": {
                    "code": "600519",
                    "price": 1400.0,
                    "last_close": 1390.0,
                    "open": 1395.0,
                    "high": 1405.0,
                    "low": 1388.0,
                    "volume": 1000.0,
                    "amount": 1400000.0,
                    "quote_time": now,
                }
            },
        )

        def provider_for(name):
            return provider if name == "tencent" else api.UnavailableProvider(name)

        with patch.object(api, "_provider_for", side_effect=provider_for):
            result = query("stock_snapshot", codes=["600519", "000858"])

        self.assertEqual("error", result["_meta"]["status"])
        self.assertIsNone(result["_meta"]["provider_used"])
        rejected = next(
            attempt
            for attempt in result["_meta"]["attempts"]
            if attempt["provider"] == "tencent"
        )
        self.assertEqual("quality_failure", rejected["status"])
        self.assertEqual("QUALITY_SNAPSHOT_INCOMPLETE", rejected["error_code"])

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

    def test_kline_quality_rejects_incomplete_legacy_fallback_payload(self):
        raw = {
            "code": "600519",
            "bars": [{"time": "2026-07-29", "close": 1400, "amount": None}],
            "_source": "tencent_fallback",
            "_meta": {"fallback_from": "stocktoday", "fallback_to": "tencent"},
        }
        provider = StaticProvider("tencent", raw)

        def provider_for(name):
            return provider if name == "tencent" else api.UnavailableProvider(name)

        with patch.object(api, "_provider_for", side_effect=provider_for):
            result = query("stock_kline", code="600519", count=1)

        self.assertEqual("error", result["_meta"]["status"])
        self.assertIsNone(result["_meta"]["provider_used"])
        rejected = next(
            attempt
            for attempt in result["_meta"]["attempts"]
            if attempt["provider"] == "tencent"
        )
        self.assertEqual("quality_failure", rejected["status"])
        self.assertEqual("QUALITY_ADJUSTMENT_MISMATCH", rejected["error_code"])

    def test_stock_kline_quality_identifies_validated_bar_shape(self):
        provider = StaticProvider("stocktoday", {
            "code": "600737", "period": "daily", "adjustment": "none",
            "volume_unit": "share", "amount_unit": "CNY",
            "bars": [{"datetime": "2026-09-23", "open": 14.98,
                      "high": 15.26, "low": 14.57, "close": 15.03,
                      "volume": 71119216, "amount": 1065797796}],
        })
        with patch.object(api, "_provider_for", return_value=provider):
            result = query("stock_kline", code="600737", period="daily",
                           start_date="20260923", end_date="20260923")
        quality = result["_meta"]["quality"]
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("kline_bars", quality["row_shape"])
        self.assertEqual("kline_bars", quality["expected_row_shape"])
        self.assertEqual("exact", quality["semantic_equivalence"])

    def test_market_fact_report_shape_keeps_real_source_gaps(self):
        provider = StaticProvider("market_facts", {
            "trade_date": "20260923", "counts": {"up": 51, "down": 13},
            "source_gaps": ["emotion_all_listed_denominator_unverified"],
        })
        with patch.object(api, "_provider_for", return_value=provider):
            result = query("market_facts", trade_date="20260923")
        quality = result["_meta"]["quality"]
        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("market_fact_report", quality["row_shape"])
        self.assertEqual("exact", quality["semantic_equivalence"])
        self.assertEqual("partial", quality["status"])
        self.assertIn("emotion_all_listed_denominator_unverified",
                      quality["reason_codes"])

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
        provider = StaticProvider("stocktoday", empty_pool)
        with patch.object(api, "_provider_for", return_value=provider):
            result = query("market_limit_state")

        self.assertEqual("empty", result["_meta"]["status"])
        self.assertEqual("empty", result["_meta"]["quality"]["status"])
        self.assertEqual(0, result["_meta"]["quality"]["returned_count"])


if __name__ == "__main__":
    unittest.main()
