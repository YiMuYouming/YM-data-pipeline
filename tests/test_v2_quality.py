"""Semantic quality metadata for the V2 resolver."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import ym_stock_data.api as api
from ym_stock_data.provider_state import ProviderState

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TZ_SH = timezone(timedelta(hours=8))


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TZ_SH)


def fresh_timestamp() -> str:
    return datetime.now(TZ_SH).isoformat(timespec="seconds")


def canonical_result(
    intent,
    data,
    *,
    status="success",
    provider="stocktoday",
    source_chain=None,
    quality=None,
    attempts=None,
    fetched_at=None,
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
    if quality is None:
        count = len(data.get("queries", [])) if isinstance(data, dict) and isinstance(data.get("queries"), list) else int(data is not None)
        quality = {
            "status": "error" if status == "error" else "normal",
            "row_shape": "unknown",
            "expected_row_shape": None,
            "requested_count": None,
            "returned_count": count,
            "coverage": None,
            "missing": [],
            "missing_count": 0,
            "semantic_equivalence": "unknown",
            "reason_codes": [],
        }
    return {
        "data": data,
        "_meta": {
            "contract_version": "1.0",
            "intent": intent,
            "status": status,
            "provider_used": provider if status in {"success", "degraded", "empty"} else None,
            "source": provider if status in {"success", "degraded", "empty"} else None,
            "source_chain": chain,
            "attempts": attempts,
            "fetched_at": fetched_at or fresh_timestamp(),
            "quality": quality,
        },
    }


def quality_record(
    status,
    *,
    row_shape="unknown",
    expected_row_shape=None,
    requested_count=None,
    returned_count=0,
    coverage=None,
    missing=None,
    semantic_equivalence="unknown",
    reason_codes=None,
):
    missing = list(missing or [])
    return {
        "status": status,
        "row_shape": row_shape,
        "expected_row_shape": expected_row_shape,
        "requested_count": requested_count,
        "returned_count": returned_count,
        "coverage": coverage,
        "missing": missing,
        "missing_count": len(missing),
        "semantic_equivalence": semantic_equivalence,
        "reason_codes": list(reason_codes or []),
    }


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
        self.provider_state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.provider_state_dir.cleanup)
        state = ProviderState(Path(self.provider_state_dir.name) / "providers.sqlite3")
        state_patch = patch.object(api, "_STATE", state)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def quality(self, result):
        self.assertIn("quality", result["_meta"])
        return result["_meta"]["quality"]

    def test_empty_review_query_preserves_canonical_empty_quality(self):
        from ym_stock_data.v2 import resolve

        query_text = "近3日板块涨幅前20"
        source_chain = ["iwencai_openapi", "pywencai", "tdx_screener", "wind_screener"]
        quality = quality_record(
            "empty",
            expected_row_shape="sector_rows",
            requested_count=20,
            reason_codes=["empty_result"],
        )
        attempts = [
            {"provider": name, "status": "empty", "error_code": None, "latency_ms": 1}
            for name in source_chain
        ]
        canonical = canonical_result(
            "review_sentiment",
            {"queries": [{"query": query_text, "result": {"datas": []}, "_meta": {
                "quality": quality, "source_chain": source_chain,
            }}]},
            status="empty",
            provider="wind_screener",
            source_chain=source_chain,
            attempts=attempts,
            quality=quality,
            fetched_at="2026-07-11T15:00:00+08:00",
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=canonical) as query_call:
            result = resolve(
                "review_sentiment",
                query=query_text,
                expected_row_shape="sector_rows",
                expected_count=20,
                _now=ts("2026-07-11T15:00:20+08:00"),
            )

        query_call.assert_called_once_with(
            "review_sentiment", query=query_text,
            expected_row_shape="sector_rows", expected_count=20,
        )
        self.assertEqual("normal", result["_meta"]["confidence"])
        quality = self.quality(result)
        self.assertEqual("empty", quality["status"])
        self.assertEqual("unknown", quality["row_shape"])
        self.assertEqual("sector_rows", quality["expected_row_shape"])
        self.assertEqual(20, quality["requested_count"])
        self.assertEqual(0, quality["returned_count"])
        self.assertIsNone(quality["coverage"])
        self.assertEqual([], quality["missing"])
        self.assertEqual(0, quality["missing_count"])
        self.assertEqual("unknown", quality["semantic_equivalence"])
        self.assertIn("empty_result", quality["reason_codes"])

        query_meta = result["data"]["queries"][0]["_meta"]
        self.assertEqual(quality, query_meta["quality"])
        self.assertEqual(source_chain, query_meta["source_chain"])
        self.assertEqual(["empty"] * 4, [attempt["status"] for attempt in result["_meta"]["attempts"]])

    def test_review_sentiment_preserves_canonical_fallback_provenance(self):
        from ym_stock_data.v2 import resolve

        data = {"zt_count": 30, "zb_count": 10, "dt_count": 5, "break_rate": 25.0, "max_board": 4}
        canonical = canonical_result(
            "review_sentiment", data, status="degraded", provider="eastmoney_breadth",
            source_chain=["pytdx_breadth", "eastmoney_breadth"],
            quality=quality_record("partial", returned_count=1, reason_codes=["fallback_source"]),
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=canonical) as query_call:
            result = resolve(
                "review_sentiment",
            )

        query_call.assert_called_once_with("review_sentiment")
        self.assertEqual(data, result["data"])
        self.assertEqual("degraded", result["_meta"]["confidence"])
        self.assertEqual(["pytdx_breadth", "eastmoney_breadth"], result["_meta"]["source_chain"])

    def test_sector_semantic_degradation_is_preserved_from_canonical_result(self):
        from ym_stock_data.v2 import resolve

        query_text = "近3日板块涨幅前20"
        item = {
            "query": query_text,
            "result": {"datas": [{
                "股票代码": "600000",
                "股票简称": "浦发银行",
                "所属行业": "银行",
                "所属概念": "沪股通",
            }]},
            "_meta": {"quality": quality_record(
                "semantic_degraded", row_shape="stock_rows", expected_row_shape="sector_rows",
                requested_count=20, returned_count=1, coverage=0.05,
                semantic_equivalence="non_equivalent", reason_codes=["row_shape_mismatch"],
            )},
        }
        canonical = canonical_result(
            "review_sentiment", {"queries": [item]}, quality=item["_meta"]["quality"],
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=canonical):
            result = resolve(
                "review_sentiment",
                query=query_text,
                expected_row_shape="sector_rows",
                expected_count=20,
            )

        self.assertEqual("normal", result["_meta"]["confidence"])
        quality = self.quality(result)
        self.assertEqual("semantic_degraded", quality["status"])
        self.assertEqual("stock_rows", quality["row_shape"])
        self.assertEqual("sector_rows", quality["expected_row_shape"])
        self.assertEqual("non_equivalent", quality["semantic_equivalence"])
        self.assertIn("row_shape_mismatch", quality["reason_codes"])
        self.assertEqual(quality, result["data"]["queries"][0]["_meta"]["quality"])

    def test_sector_index_preserves_canonical_partial_name_coverage(self):
        from ym_stock_data.v2 import resolve

        sector = {"code": "881160", "name": "国防军工", "change_pct": 1.2}
        raw = {
            "items": [sector],
            "by_code": {"881160": sector},
            "by_name": {"国防军工": sector},
            "missing": ["军工", "商业航天"],
        }

        quality = quality_record(
            "partial", row_shape="sector_rows", expected_row_shape="sector_rows",
            requested_count=3, returned_count=1, coverage=1 / 3,
            missing=["军工", "商业航天"], semantic_equivalence="exact",
            reason_codes=["missing_items", "coverage_shortfall"],
        )
        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            return_value=canonical_result("sector_index", raw, provider="ths_industry", quality=quality),
        ) as query_call:
            result = resolve(
                "sector_index",
                names=["国防军工", "军工", "商业航天"],
            )

        query_call.assert_called_once_with("sector_index", names=["国防军工", "军工", "商业航天"])
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

    def test_review_sector_rows_preserve_canonical_normal_quality(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "datas": [
                {"板块代码": "881160", "板块名称": "国防军工", "涨跌幅": 1.2},
                {"板块代码": "881164", "板块名称": "航天装备", "涨跌幅": 0.8},
            ],
            "row_count": 2,
            "_source": "openapi",
        }

        query_text = "板块涨幅前2"
        quality = quality_record(
            "normal", row_shape="sector_rows", expected_row_shape="sector_rows",
            requested_count=2, returned_count=2, coverage=1.0,
            semantic_equivalence="exact",
        )
        canonical = canonical_result(
            "review_sentiment",
            {"queries": [{"query": query_text, "result": raw, "_meta": {"quality": quality}}]},
            quality=quality,
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=canonical) as query_call:
            result = resolve(
                "review_sentiment",
                query=query_text,
                expected_row_shape="sector_rows",
                expected_count=2,
            )

        query_call.assert_called_once_with(
            "review_sentiment", query=query_text,
            expected_row_shape="sector_rows", expected_count=2,
        )
        quality = self.quality(result)
        self.assertEqual("normal", quality["status"])
        self.assertEqual("sector_rows", quality["row_shape"])
        self.assertEqual(1.0, quality["coverage"])
        self.assertEqual("exact", quality["semantic_equivalence"])
        self.assertEqual([], quality["reason_codes"])

    def test_source_error_takes_precedence_over_empty_rows(self):
        from ym_stock_data.v2 import resolve

        query_text = "近3日板块涨幅前20"
        quality = quality_record(
            "error", expected_row_shape="sector_rows", requested_count=20,
            reason_codes=["source_error", "empty_result"],
        )
        attempts = [
            {"provider": "iwencai_openapi", "status": "provider_error", "error_code": "HTTP_503", "latency_ms": 1},
            {"provider": "pywencai", "status": "provider_error", "error_code": "UNAVAILABLE", "latency_ms": 1},
        ]
        failed = canonical_result(
            "review_sentiment", None, status="error", quality=quality,
            source_chain=["iwencai_openapi", "pywencai"], attempts=attempts,
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=failed) as query_call:
            result = resolve(
                "review_sentiment",
                query=query_text,
                expected_row_shape="sector_rows",
                expected_count=20,
            )

        query_call.assert_called_once_with(
            "review_sentiment", query=query_text,
            expected_row_shape="sector_rows", expected_count=20,
        )
        self.assertEqual("error", result["_meta"]["confidence"])
        self.assertTrue(result["_meta"]["error"])
        self.assertEqual(attempts, result["_meta"]["attempts"])
        quality = self.quality(result)
        self.assertEqual("error", quality["status"])
        self.assertEqual("unknown", quality["row_shape"])
        self.assertEqual("unknown", quality["semantic_equivalence"])
        self.assertIn("source_error", quality["reason_codes"])

    def test_stock_snapshot_missing_requested_code_is_canonical_error(self):
        from ym_stock_data.v2 import resolve

        quality = quality_record(
            "error", expected_row_shape="stock_rows", requested_count=2,
            reason_codes=["source_error", "empty_result"],
        )
        failed = canonical_result(
            "stock_snapshot", None, status="error", quality=quality,
            attempts=[{
                "provider": "tencent", "status": "quality_failure",
                "error_code": "QUALITY_SNAPSHOT_INCOMPLETE", "latency_ms": 1,
            }],
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=failed) as query_call:
            result = resolve(
                "stock_snapshot",
                codes=["600000", "600001"],
            )

        query_call.assert_called_once_with("stock_snapshot", codes=["600000", "600001"])
        self.assertIsNone(result["data"])
        self.assertEqual("error", result["_meta"]["confidence"])
        self.assertEqual("QUALITY_SNAPSHOT_INCOMPLETE", result["_meta"]["attempts"][0]["error_code"])
        quality = self.quality(result)
        self.assertEqual("error", quality["status"])

    def test_counted_kline_preserves_canonical_short_coverage(self):
        from ym_stock_data.v2 import resolve

        raw = {
            "code": "600000",
            "adjustment": "none",
            "volume_unit": "share",
            "amount_unit": "CNY",
            "bars": [{
                "datetime": datetime.now(TZ_SH).strftime("%Y-%m-%d %H:%M:%S"),
                "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1,
                "volume": 1000, "amount": 10100,
            }],
        }
        quality = quality_record(
            "partial", requested_count=3, returned_count=1, coverage=1 / 3,
            reason_codes=["coverage_shortfall"],
        )
        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            return_value=canonical_result("stock_kline", raw, quality=quality),
        ) as query_call:
            result = resolve(
                "stock_kline",
                code="600000",
                count=3,
            )

        query_call.assert_called_once_with("stock_kline", code="600000", count=3)
        self.assertEqual(raw, result["data"])
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

        normal_quality = quality_record(
            "normal", row_shape="sector_rows", expected_row_shape="sector_rows",
            requested_count=2, returned_count=2, coverage=1.0,
            semantic_equivalence="exact",
        )
        partial_quality = quality_record(
            "partial", row_shape="sector_rows", expected_row_shape="sector_rows",
            requested_count=2, returned_count=1, coverage=0.5,
            missing=["商业航天"], semantic_equivalence="exact",
            reason_codes=["missing_items", "coverage_shortfall"],
        )
        error_quality = quality_record(
            "error", expected_row_shape="sector_rows", requested_count=2,
            reason_codes=["source_error", "empty_result"],
        )
        normal_rows = [
            {"板块代码": "881160", "板块名称": "国防军工"},
            {"板块代码": "881164", "板块名称": "航天装备"},
        ]
        partial_rows = [{"板块代码": "881160", "板块名称": "国防军工"}]
        error_attempts = [
            {"provider": "iwencai_openapi", "status": "provider_error", "error_code": "PROVIDER_ERROR", "latency_ms": 1},
            {"provider": "pywencai", "status": "provider_error", "error_code": "CONTROLLED_PROVIDER_ERROR", "latency_ms": 1},
            {"provider": "tdx_screener", "status": "auth_error", "error_code": "AUTH_MISSING", "latency_ms": 1},
            {"provider": "wind_screener", "status": "provider_error", "error_code": "CONTROLLED_WIND_ERROR", "latency_ms": 1},
        ]
        responses = {
            "normal": canonical_result(
                "review_sentiment",
                {"queries": [{"query": "normal", "result": {"datas": normal_rows}, "_meta": {"quality": normal_quality}}]},
                quality=normal_quality,
            ),
            "partial": canonical_result(
                "review_sentiment",
                {"queries": [{"query": "partial", "result": {"datas": partial_rows}, "_meta": {"quality": partial_quality}}]},
                status="degraded", provider="pywencai",
                source_chain=["iwencai_openapi", "pywencai"], quality=partial_quality,
                attempts=[
                    {"provider": "iwencai_openapi", "status": "provider_error", "error_code": "HTTP_503", "latency_ms": 1},
                    {"provider": "pywencai", "status": "success", "error_code": None, "latency_ms": 1},
                ],
            ),
            "error": canonical_result(
                "review_sentiment", None, status="error", quality=error_quality,
                source_chain=[item["provider"] for item in error_attempts], attempts=error_attempts,
            ),
        }
        with patch(
            "ym_stock_data.v2.resolve.public_api.query",
            side_effect=lambda _intent, query, **_kwargs: responses[query],
        ) as query_call:
            result = resolve(
                "review_sentiment",
                query=["normal", "partial", "error"],
                expected_row_shape="sector_rows",
                expected_count=2,
            )

        self.assertEqual(3, query_call.call_count)
        qualities = [item["_meta"]["quality"] for item in result["data"]["queries"]]
        self.assertEqual(["normal", "partial", "error"], [item["status"] for item in qualities])
        canonical_attempts = result["data"]["queries"][2]["_meta"]["canonical_meta"]["attempts"]
        self.assertEqual(error_attempts, canonical_attempts)
        rollup = self.quality(result)
        self.assertEqual("error", rollup["status"])
        self.assertEqual(6, rollup["requested_count"])
        self.assertEqual(3, rollup["returned_count"])
        self.assertEqual(0.5, rollup["coverage"])
        self.assertEqual(["商业航天"], rollup["missing"])
        for reason in ("missing_items", "coverage_shortfall", "source_error", "empty_result"):
            self.assertIn(reason, rollup["reason_codes"])

    def test_review_query_projects_canonical_fallback_provenance_and_coverage(self):
        from ym_stock_data.v2 import resolve
        fallback = {
            "datas": [
                {"股票代码": f"600{index:03d}", "股票简称": f"测试{index}"}
                for index in range(20)
            ],
            "row_count": 20,
            "_source": "pywencai",
        }
        quality = quality_record(
            "normal", row_shape="stock_rows", expected_row_shape="stock_rows",
            requested_count=20, returned_count=20, coverage=1.0,
            semantic_equivalence="exact",
        )
        query_meta = {
            "provider": "pywencai",
            "query_time": fresh_timestamp(),
            "fallback_reason": "http_5xx",
            "source_chain": ["iwencai_openapi", "pywencai"],
            "coverage": {"requested_count": 20, "returned_count": 20, "ratio": 1.0},
            "quality": quality,
        }
        canonical = canonical_result(
            "review_sentiment",
            {"queries": [{"query": "银行股", "result": fallback, "_meta": query_meta}]},
            status="degraded", provider="pywencai",
            source_chain=["iwencai_openapi", "pywencai"], quality=quality,
        )
        with patch("ym_stock_data.v2.resolve.public_api.query", return_value=canonical) as query_call:
            result = resolve(
                "review_sentiment",
                query="银行股",
                limit=50,
                expected_row_shape="stock_rows",
                expected_count=20,
            )

        query_call.assert_called_once_with(
            "review_sentiment", query="银行股", limit=50,
            expected_row_shape="stock_rows", expected_count=20,
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
