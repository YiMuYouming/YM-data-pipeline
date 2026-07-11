"""Semantic quality metadata for the V2 resolver."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TZ_SH = timezone(timedelta(hours=8))


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TZ_SH)


class V2QualityTests(unittest.TestCase):
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

        with patch("ym_stock_data.sources.iwencai.query", return_value=raw):
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
        self.assertEqual(["iwencai", "openapi"], query_meta["source_chain"])

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

        with patch("ym_stock_data.sources.iwencai.query", return_value=raw):
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

        with patch("ym_stock_data.sources.iwencai.query", return_value=raw):
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

        with patch("ym_stock_data.sources.iwencai.query", return_value=raw):
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


if __name__ == "__main__":
    unittest.main()
