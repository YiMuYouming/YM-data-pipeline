"""Semantic quality metadata for the V2 resolver."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from ym_stock_data.providers.base import ProviderOutcome

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TZ_SH = timezone(timedelta(hours=8))


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TZ_SH)


class RawResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


IWENCAI_TEST_STATE = {
    "_API_KEY": "dummy",
    "_OPENAPI_DOWN_AT": 0,
    "_PYWENCAI_DOWN_AT": 0,
    "_OPENAPI_BREAKER_AT": 0,
    "_OPENAPI_BREAKER_SECONDS": 300,
    "_OPENAPI_FAILURE_TYPE": "rate_limit",
    "_OPENAPI_LAST_ERROR": None,
    "_PYWENCAI_LAST_ERROR": None,
}


def iwencai_outcome(raw, *, provider="iwencai_openapi"):
    rows = raw.get("datas", []) if isinstance(raw, dict) else []
    if raw.get("error"):
        return ProviderOutcome(
            provider=provider,
            status="provider_error",
            error_code=str(raw.get("error_type") or "PROVIDER_ERROR"),
            latency_ms=1,
        )
    return ProviderOutcome(
        provider=provider,
        status="success" if rows else "empty",
        data=raw,
        fetched_at=(raw.get("_meta", {}) or {}).get("fetched_at"),
        latency_ms=1,
    )


class V2QualityTests(unittest.TestCase):
    def setUp(self):
        from ym_stock_data.sources import iwencai

        self.iwencai_state = patch.multiple(iwencai, **IWENCAI_TEST_STATE)
        self.iwencai_state.start()
        self.addCleanup(self.iwencai_state.stop)

    def quality(self, result):
        self.assertIn("quality", result["_meta"])
        return result["_meta"]["quality"]

    def test_empty_review_query_keeps_fresh_confidence_and_reports_empty_quality(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "datas": [],
            "row_count": 0,
            "_source": "openapi",
            "_meta": {
                "data_type": "iwencai",
                "source": "iwencai",
                "fetched_at": "2026-07-11T15:00:00+08:00",
            },
        }

        with patch(
            "ym_stock_data.providers.iwencai.IWenCaiOpenAPIProvider.call",
            return_value=iwencai_outcome(raw),
        ):
            result = resolve(
                "review_sentiment",
                query="近3日板块涨幅前20",
                expected_row_shape="sector_rows",
                expected_count=20,
                _now=ts("2026-07-11T15:00:20+08:00"),
            )

        self.assertEqual("normal", result["_meta"]["confidence"])
        quality = self.quality(result)
        self.assertEqual("empty", quality["status"])
        self.assertEqual("unknown", quality["row_shape"])
        self.assertEqual("sector_rows", quality["expected_row_shape"])
        self.assertEqual(20, quality["requested_count"])
        self.assertEqual(0, quality["returned_count"])
        self.assertEqual(0.0, quality["coverage"])
        self.assertEqual([], quality["missing"])
        self.assertEqual(0, quality["missing_count"])
        self.assertEqual("unknown", quality["semantic_equivalence"])
        self.assertIn("empty_result", quality["reason_codes"])

        query_meta = result["data"]["queries"][0]["_meta"]
        self.assertEqual(quality, query_meta["quality"])
        self.assertEqual(["iwencai_openapi"], query_meta["source_chain"])

    def test_review_sentiment_breadth_failure_uses_compatible_limit_pool(self):
        from ym_stock_data.v2 import resolve

        limit_state = {
            "zt_count": 30,
            "zb_count": 10,
            "dt_count": 5,
            "break_rate": 25.0,
            "max_board": 4,
            "pools": {"zt": [{}], "zb": [{}], "dt": [{}]},
            "_meta": {"fetched_at": "2026-07-13T15:10:00+08:00"},
        }

        with patch("ym_stock_data.sources.pytdx.fetch_breadth", return_value={}) as fetch_breadth, \
             patch("ym_stock_data.providers.local.fetch_limit_state", return_value=limit_state), \
             patch("ym_stock_data.providers.iwencai.IWenCaiOpenAPIProvider.call") as query:
            result = resolve(
                "review_sentiment",
                _now=ts("2026-07-13T15:10:20+08:00"),
            )

        fetch_breadth.assert_called_once_with()
        query.assert_not_called()
        self.assertIn("query_summary", result["data"])
        summary = result["data"]["query_summary"]
        self.assertEqual(summary["total_queries"], 1)
        self.assertEqual(summary["empty_queries"], 0)
        self.assertEqual(summary["nonempty_queries"], 1)
        self.assertEqual(summary["batch_status"], "partial")
        self.assertEqual(
            result["_meta"]["source_chain"],
            ["pytdx_breadth", "eastmoney_limit_pool"],
        )

    def test_sector_expectation_rejects_stock_rows_even_with_industry_fields(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "datas": [{
                "股票代码": "600000",
                "股票简称": "浦发银行",
                "所属行业": "银行",
                "所属概念": "沪股通",
            }],
            "row_count": 1,
            "_source": "openapi",
            "_meta": {
                "data_type": "iwencai",
                "source": "iwencai",
                "fetched_at": "2026-07-11T15:00:00+08:00",
            },
        }

        with patch(
            "ym_stock_data.providers.iwencai.IWenCaiOpenAPIProvider.call",
            return_value=iwencai_outcome(raw),
        ):
            result = resolve(
                "review_sentiment",
                query="近3日板块涨幅前20",
                expected_row_shape="sector_rows",
                expected_count=20,
                _now=ts("2026-07-11T15:00:20+08:00"),
            )

        self.assertEqual("normal", result["_meta"]["confidence"])
        quality = self.quality(result)
        self.assertEqual("semantic_degraded", quality["status"])
        self.assertEqual("stock_rows", quality["row_shape"])
        self.assertEqual("sector_rows", quality["expected_row_shape"])
        self.assertEqual("non_equivalent", quality["semantic_equivalence"])
        self.assertIn("row_shape_mismatch", quality["reason_codes"])
        self.assertEqual(quality, result["data"]["queries"][0]["_meta"]["quality"])

    def test_sector_index_infers_partial_name_coverage(self):
        from ym_stock_data.v2 import resolve

        sector = {"code": "881160", "name": "国防军工", "change_pct": 1.2}
        raw = {
            "items": [sector],
            "by_code": {"881160": sector},
            "by_name": {"国防军工": sector},
            "missing": ["军工", "商业航天"],
            "_meta": {
                "data_type": "sector_index",
                "source": "ths_industry",
                "fetched_at": "2026-07-11T15:00:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.ths_industry.fetch_sector_index", return_value=raw):
            result = resolve(
                "sector_index",
                names=["国防军工", "军工", "商业航天"],
                _now=ts("2026-07-11T15:00:20+08:00"),
            )

        quality = self.quality(result)
        self.assertEqual("partial", quality["status"])
        self.assertEqual("sector_rows", quality["row_shape"])
        self.assertEqual("sector_rows", quality["expected_row_shape"])
        self.assertEqual(3, quality["requested_count"])
        self.assertEqual(1, quality["returned_count"])
        self.assertEqual(1 / 3, quality["coverage"])
        self.assertEqual(["军工", "商业航天"], quality["missing"])
        self.assertEqual(2, quality["missing_count"])
        self.assertEqual("exact", quality["semantic_equivalence"])

    def test_review_sector_rows_report_normal_quality(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "datas": [
                {"板块代码": "881160", "板块名称": "国防军工", "涨跌幅": 1.2},
                {"板块代码": "881164", "板块名称": "航天装备", "涨跌幅": 0.8},
            ],
            "row_count": 2,
            "_source": "openapi",
            "_meta": {
                "data_type": "iwencai",
                "source": "iwencai",
                "fetched_at": "2026-07-11T15:00:00+08:00",
            },
        }

        with patch(
            "ym_stock_data.providers.iwencai.IWenCaiOpenAPIProvider.call",
            return_value=iwencai_outcome(raw),
        ):
            result = resolve(
                "review_sentiment",
                query="板块涨幅前2",
                expected_row_shape="sector_rows",
                expected_count=2,
                _now=ts("2026-07-11T15:00:20+08:00"),
            )

        quality = self.quality(result)
        self.assertEqual("normal", quality["status"])
        self.assertEqual("sector_rows", quality["row_shape"])
        self.assertEqual(1.0, quality["coverage"])
        self.assertEqual("exact", quality["semantic_equivalence"])
        self.assertEqual([], quality["reason_codes"])

    def test_source_error_takes_precedence_over_empty_rows(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "error": "all paths dead",
            "error_type": "all_paths_dead",
            "datas": [],
            "row_count": 0,
            "_source": "none",
            "_meta": {
                "data_type": "iwencai",
                "source": "iwencai",
                "fetched_at": "2026-07-11T15:00:00+08:00",
                "error": True,
            },
        }

        with patch(
            "ym_stock_data.providers.iwencai.IWenCaiOpenAPIProvider.call",
            return_value=iwencai_outcome(raw),
        ):
            result = resolve(
                "review_sentiment",
                query="近3日板块涨幅前20",
                expected_row_shape="sector_rows",
                expected_count=20,
                _now=ts("2026-07-11T15:00:20+08:00"),
            )

        self.assertEqual("error", result["_meta"]["confidence"])
        quality = self.quality(result)
        self.assertEqual("error", quality["status"])
        self.assertEqual("unknown", quality["row_shape"])
        self.assertEqual("unknown", quality["semantic_equivalence"])
        self.assertIn("source_error", quality["reason_codes"])

    def test_stock_snapshot_infers_missing_code_coverage_without_moving_data(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "600000": {"最新价": 10.1},
            "_meta": {
                "data_type": "quotes",
                "source": "pytdx",
                "fetched_at": "2026-07-11T15:00:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.pytdx.fetch_quotes", return_value=raw):
            result = resolve(
                "stock_snapshot",
                codes=["600000", "600001"],
                _now=ts("2026-07-11T15:00:20+08:00"),
            )

        self.assertEqual(10.1, result["data"]["600000"]["最新价"])
        quality = self.quality(result)
        self.assertEqual("partial", quality["status"])
        self.assertEqual("stock_rows", quality["row_shape"])
        self.assertEqual(0.5, quality["coverage"])
        self.assertEqual(["600001"], quality["missing"])

    def test_counted_kline_infers_short_coverage(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "code": "600000",
            "bars": [{"time": "2026-07-11 15:00", "close": 10.1}],
            "_meta": {
                "data_type": "kline",
                "source": "pytdx",
                "fetched_at": "2026-07-11T15:00:00+08:00",
            },
        }

        with patch("ym_stock_data.sources.pytdx.fetch_kline", return_value=raw):
            result = resolve(
                "stock_kline",
                code="600000",
                count=3,
                _now=ts("2026-07-11T15:00:20+08:00"),
            )

        quality = self.quality(result)
        self.assertEqual("partial", quality["status"])
        self.assertEqual("unknown", quality["row_shape"])
        self.assertEqual(3, quality["requested_count"])
        self.assertEqual(1, quality["returned_count"])
        self.assertEqual(1 / 3, quality["coverage"])

    def test_unknown_rows_with_shape_expectation_are_semantically_degraded(self):
        from ym_stock_data.v2.quality import assess_quality

        quality = assess_quality(
            [{"排名": 1, "涨跌幅": 2.5}],
            expected_row_shape="sector_rows",
        )

        self.assertEqual("semantic_degraded", quality["status"])
        self.assertEqual("unknown", quality["row_shape"])
        self.assertEqual("unknown", quality["semantic_equivalence"])
        self.assertIn("row_shape_unknown", quality["reason_codes"])

    def test_mixed_stock_and_sector_rows_are_semantically_degraded(self):
        from ym_stock_data.v2.quality import assess_quality

        quality = assess_quality(
            [
                {"股票代码": "600000", "股票简称": "浦发银行"},
                {"板块代码": "881160", "板块名称": "国防军工"},
            ],
            expected_row_shape="sector_rows",
        )

        self.assertEqual("semantic_degraded", quality["status"])
        self.assertEqual("unknown", quality["row_shape"])
        self.assertEqual("non_equivalent", quality["semantic_equivalence"])
        self.assertIn("mixed_row_shapes", quality["reason_codes"])

    def test_non_mapping_row_degrades_shape_and_does_not_count_toward_coverage(self):
        from ym_stock_data.v2.quality import assess_quality

        quality = assess_quality(
            [
                {"板块代码": "881160", "板块名称": "国防军工"},
                "garbage",
            ],
            expected_row_shape="sector_rows",
            expected_count=2,
        )

        self.assertEqual("semantic_degraded", quality["status"])
        self.assertEqual("sector_rows", quality["row_shape"])
        self.assertEqual(1, quality["returned_count"])
        self.assertEqual(0.5, quality["coverage"])
        self.assertIn("invalid_row", quality["reason_codes"])
        self.assertIn("mixed_row_types", quality["reason_codes"])

    def test_review_quality_rollup_uses_worst_status_and_merges_counts(self):
        from ym_stock_data.v2 import resolve

        raw_results = {
            "normal": {
                "datas": [
                    {"板块代码": "881160", "板块名称": "国防军工"},
                    {"板块代码": "881164", "板块名称": "航天装备"},
                ],
                "row_count": 2,
                "_source": "openapi",
                "_meta": {"fetched_at": "2026-07-11T15:00:00+08:00"},
            },
            "partial": {
                "datas": [{"板块代码": "881160", "板块名称": "国防军工"}],
                "row_count": 1,
                "missing": ["商业航天"],
                "_source": "pywencai",
                "_meta": {
                    "fetched_at": "2026-07-11T15:00:00+08:00",
                    "fallback_from": "openapi",
                    "fallback_to": "pywencai",
                },
            },
            "error": {
                "error": "all paths dead",
                "datas": [],
                "row_count": 0,
                "_source": "none",
                "_meta": {
                    "fetched_at": "2026-07-11T15:00:00+08:00",
                    "error": True,
                },
            },
        }

        with patch(
            "ym_stock_data.providers.iwencai.IWenCaiOpenAPIProvider.call",
            side_effect=lambda _intent, params: iwencai_outcome(
                raw_results[params["query"]]
            ),
        ):
            result = resolve(
                "review_sentiment",
                query=list(raw_results),
                expected_row_shape="sector_rows",
                expected_count=2,
                _now=ts("2026-07-11T15:00:20+08:00"),
            )

        qualities = [item["_meta"]["quality"] for item in result["data"]["queries"]]
        self.assertEqual(["normal", "partial", "error"], [item["status"] for item in qualities])
        rollup = self.quality(result)
        self.assertEqual("error", rollup["status"])
        self.assertEqual(6, rollup["requested_count"])
        self.assertEqual(3, rollup["returned_count"])
        self.assertEqual(0.5, rollup["coverage"])
        self.assertEqual(["商业航天"], rollup["missing"])
        for reason in ("missing_items", "coverage_shortfall", "source_error", "empty_result"):
            self.assertIn(reason, rollup["reason_codes"])

    def test_review_query_retains_fallback_provenance_and_consistent_coverage(self):
        from ym_stock_data.v2 import resolve
        fallback = {
            "datas": [
                {"股票代码": f"600{index:03d}", "股票简称": f"测试{index}"}
                for index in range(20)
            ],
            "row_count": 20,
            "_source": "pywencai",
        }

        with patch(
            "ym_stock_data.providers.iwencai.IWenCaiOpenAPIProvider.call",
            return_value=ProviderOutcome(
                provider="iwencai_openapi",
                status="provider_error",
                error_code="HTTP_503",
                latency_ms=1,
            ),
        ), patch(
            "ym_stock_data.providers.iwencai.PyWenCaiProvider.call",
            return_value=iwencai_outcome(fallback, provider="pywencai"),
        ):
            result = resolve(
                "review_sentiment",
                query="银行股",
                limit=50,
                expected_row_shape="stock_rows",
                expected_count=20,
            )

        query_meta = result["data"]["queries"][0]["_meta"]
        self.assertIn("provider", query_meta)
        self.assertEqual("pywencai", query_meta["provider"])
        datetime.fromisoformat(query_meta["query_time"])
        self.assertEqual("http_5xx", query_meta["fallback_reason"])
        self.assertEqual(["iwencai_openapi", "pywencai"], query_meta["source_chain"])
        self.assertEqual({
            "requested_count": 20,
            "returned_count": 20,
            "ratio": 1.0,
        }, query_meta["coverage"])
        self.assertEqual(20, query_meta["quality"]["requested_count"])
        self.assertEqual(20, query_meta["quality"]["returned_count"])
        self.assertEqual(1.0, query_meta["quality"]["coverage"])


if __name__ == "__main__":
    unittest.main()
