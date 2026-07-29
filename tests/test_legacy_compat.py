import tempfile
import unittest
import inspect
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock, patch

import ym_stock_data.api as api
from ym_stock_data import fetch, list_supported, query
from ym_stock_data.contracts import validate_result
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.local import LocalProvider
from ym_stock_data.v2.resolve import resolve


EXPECTED_SUPPORTED = {
    "quotes",
    "index",
    "breadth",
    "sector_index",
    "kline",
    "kline_15m",
    "iwencai",
    "ths_hot",
    "tencent",
    "northbound",
    "dragon_tiger",
    "sector_inflow",
    "research",
    "filings",
    "news",
    "limit_state",
    "market_limit_state",
    "stock_event",
    "iwencai_content",
    "industry_research",
}


class NamedProvider:
    def __init__(self, name, calls, data=None):
        self.name = name
        self.calls = calls
        self.data = data

    def probe(self):
        return {"provider": self.name, "status": "ready"}

    def call(self, intent, params):
        self.calls.append((intent, params))
        if self.name == "iwencai_openapi":
            return ProviderOutcome(
                provider=self.name,
                status="auth_error",
                error_code="HTTP_401",
                latency_ms=1,
            )
        return ProviderOutcome(
            provider=self.name,
            status="success",
            data=self.data or {
                "datas": [{"股票代码": "600519", "股票简称": "贵州茅台"}],
                "row_count": 1,
            },
            latency_ms=1,
            quality={"status": "normal", "returned_count": 1, "reason_codes": []},
            auth={"required": False, "status": "not_required"},
        )


def business_rows(result):
    if isinstance(result.get("datas"), list):
        return result["datas"]
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("datas"), list):
        return data["datas"]
    if isinstance(data, dict):
        queries = data.get("queries")
        if isinstance(queries, list) and queries:
            return queries[0].get("result", {}).get("datas", [])
    return []


class LegacyCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state = ProviderState(Path(self.temp_dir.name) / "providers.sqlite3")
        self.calls = []
        self.providers = {
            name: NamedProvider(name, self.calls)
            for name in ("iwencai_openapi", "pywencai")
        }
        self.state_patch = patch.object(api, "_STATE", self.state)
        self.provider_patch = patch.object(
            api,
            "_provider_for",
            side_effect=lambda name: self.providers.get(
                name, api.UnavailableProvider(name)
            ),
        )
        self.state_patch.start()
        self.provider_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.provider_patch.stop)

    def test_fetch_iwencai_query_signature_reaches_canonical_router(self):
        result = fetch("iwencai", query="今日涨停", limit=3)

        self.assertNotIn("error", result)
        self.assertEqual("pywencai", result["_meta"]["provider_used"])
        self.assertEqual(2, len(self.calls))
        self.assertEqual("今日涨停", self.calls[0][1]["query"])

    def test_fetch_resolve_query_preserve_provider_attempts_and_rows(self):
        direct = query("review_sentiment", query="今日涨停", limit=3)
        via_resolve = resolve("review_sentiment", query="今日涨停", limit=3)
        via_fetch = fetch("iwencai", query="今日涨停", limit=3)

        for result in (direct, via_resolve, via_fetch):
            self.assertEqual("pywencai", result["_meta"]["provider_used"])
            self.assertEqual(
                ["iwencai_openapi", "pywencai"],
                result["_meta"]["source_chain"],
            )
            self.assertEqual(
                ["auth_error", "success"],
                [attempt["status"] for attempt in result["_meta"]["attempts"]],
            )
            self.assertEqual(business_rows(direct), business_rows(result))
        self.assertEqual(6, len(self.calls))

    def test_fetch_does_not_overwrite_actual_provider_metadata(self):
        result = fetch("iwencai", query="今日涨停", limit=3)

        self.assertEqual("pywencai", result["_meta"]["source"])
        self.assertEqual("pywencai", result["_meta"]["provider_used"])
        self.assertNotEqual("iwencai", result["_meta"]["source"])

    def test_explicit_query_list_is_split_into_canonical_calls(self):
        result = resolve(
            "review_sentiment",
            query=["今日涨停", "今日连板"],
            limit=3,
        )

        self.assertEqual(2, result["data"]["query_count"])
        self.assertEqual(4, len(self.calls))
        self.assertEqual(
            ["今日涨停", "今日涨停", "今日连板", "今日连板"],
            [params["query"] for _, params in self.calls],
        )

    def test_supported_inventory_is_preserved_and_explicitly_classified(self):
        fetch_module = import_module("ym_stock_data.fetch")

        self.assertEqual(EXPECTED_SUPPORTED, set(list_supported()))
        self.assertEqual(
            EXPECTED_SUPPORTED,
            set(fetch_module.CANONICAL_ROUTES) | set(fetch_module.LEGACY_DIRECT_ROUTES),
        )
        self.assertFalse(
            set(fetch_module.CANONICAL_ROUTES) & set(fetch_module.LEGACY_DIRECT_ROUTES)
        )
        self.assertIn("sector_index", fetch_module.LEGACY_DIRECT_ROUTES)
        self.assertNotIn("sector_index", fetch_module.CANONICAL_ROUTES)

    def test_legacy_880_sector_route_keeps_shape_and_real_source(self):
        source = Mock()
        source.fetch_sector.return_value = {
            "880001": {"name": "行业板块", "close": 1000},
            "_meta": {"source": "pytdx", "fetched_at": "2026-07-29T10:00:00+08:00"},
        }
        with patch("ym_stock_data.fetch._load_source", return_value=source):
            result = fetch("sector_index", codes=["880001"])

        source.fetch_sector.assert_called_once_with(codes=["880001"])
        self.assertEqual(1000, result["880001"]["close"])
        self.assertEqual("legacy_direct", result["_meta"]["compatibility_route"])
        self.assertEqual("pytdx", result["_meta"]["source"])

    def test_legacy_breadth_projects_bins_instead_of_review_wrapper(self):
        fetch_module = import_module("ym_stock_data.fetch")
        canonical = {
            "data": {
                "query_summary": {"total_queries": 1},
                "aggregates": {
                    "breadth": {"涨停": 72, "跌停": 12, "_total": 5094}
                },
            },
            "_meta": {
                "status": "success",
                "provider_used": "pytdx_breadth",
                "source": "pytdx_breadth",
                "attempts": [],
            },
        }
        with patch.object(fetch_module, "canonical_query", return_value=canonical):
            result = fetch("breadth")

        self.assertEqual(5094, result["_total"])
        self.assertEqual(72, result["涨停"])
        self.assertNotIn("query_summary", result)
        self.assertNotIn("aggregates", result)
        self.assertEqual("canonical", result["_meta"]["compatibility_route"])

    def test_legacy_breadth_keeps_bins_under_eastmoney_breadth_fallback(self):
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
        with patch.object(
            api,
            "_provider_for",
            side_effect=lambda name: LocalProvider(name)
            if name in {"pytdx_breadth", "eastmoney_breadth", "eastmoney_limit_pool"}
            else api.UnavailableProvider(name),
        ), patch(
            "ym_stock_data.providers.local.pytdx.fetch_breadth",
            return_value=breadth,
        ):
            result = fetch("breadth")

        self.assertEqual(5094, result["_total"])
        self.assertEqual(72, result["涨停"])
        self.assertEqual(12, result["跌停"])
        self.assertNotIn("query_summary", result)
        self.assertEqual("eastmoney_breadth", result["_meta"]["provider_used"])

    def test_mixed_provider_review_batch_uses_explicit_compatibility_contract(self):
        class MixedProvider:
            def __init__(self, name):
                self.name = name

            def call(self, intent, params):
                if self.name == "iwencai_openapi" and params["query"] == "q2":
                    return ProviderOutcome(
                        provider=self.name,
                        status="auth_error",
                        error_code="HTTP_401",
                        latency_ms=1,
                    )
                return ProviderOutcome(
                    provider=self.name,
                    status="success",
                    data={
                        "datas": [{"股票代码": "600519", "查询": params["query"]}],
                        "row_count": 1,
                    },
                    latency_ms=1,
                    quality={"status": "normal", "returned_count": 1, "reason_codes": []},
                    auth={"required": False, "status": "not_required"},
                )

        with patch.object(
            api,
            "_provider_for",
            side_effect=lambda name: MixedProvider(name)
            if name in {"iwencai_openapi", "pywencai"}
            else api.UnavailableProvider(name),
        ):
            result = resolve("review_sentiment", query=["q1", "q2"], limit=1)

        meta = result["_meta"]
        self.assertNotIn("contract_version", meta)
        self.assertNotIn("provider_used", meta)
        self.assertNotIn("source", meta)
        self.assertEqual("v2-review-batch", meta["compatibility_contract"])
        self.assertEqual(["iwencai_openapi", "pywencai"], meta["providers_used"])
        for item in result["data"]["queries"]:
            canonical_meta = item["_meta"]["canonical_meta"]
            validate_result({"data": item["result"], "_meta": canonical_meta})

    def test_resolve_single_string_remains_valid_canonical_contract(self):
        result = resolve("review_sentiment", query="今日涨停", limit=3)

        self.assertEqual("1.0", result["_meta"]["contract_version"])
        validate_result(result)

    def test_representative_canonical_fetch_shapes_and_metadata(self):
        cases = {
            "index": ("realtime_market", {}, {"上证指数": {"最新价": 3200}}, "上证指数"),
            "quotes": ("stock_snapshot", {"codes": ["600519"]}, {"600519": {"price": 1400}}, "600519"),
            "kline": ("stock_kline", {"code": "600519"}, {"bars": [{"time": "2026-07-29"}]}, "bars"),
            "research": ("research", {"code": "600519"}, {"reports": [{"title": "研报"}]}, "reports"),
            "filings": ("filings", {"code": "600519"}, {"filings": [{"title": "公告"}]}, "filings"),
            "news": ("news", {}, {"items": [{"title": "新闻"}]}, "items"),
        }
        first_providers = {
            "realtime_market": "pytdx",
            "stock_snapshot": "pytdx",
            "stock_kline": "pytdx",
            "research": "eastmoney_research",
            "filings": "cninfo",
            "news": "cls",
        }
        for data_type, (intent, kwargs, data, business_key) in cases.items():
            with self.subTest(data_type=data_type):
                provider_name = first_providers[intent]
                provider = NamedProvider(provider_name, [], data=data)
                with patch.object(
                    api,
                    "_provider_for",
                    side_effect=lambda name, provider=provider: provider
                    if name == provider.name
                    else api.UnavailableProvider(name),
                ):
                    result = fetch(data_type, **kwargs)
                self.assertIn(business_key, result)
                self.assertEqual("canonical", result["_meta"]["compatibility_route"])
                self.assertEqual(intent, result["_meta"]["canonical_intent"])
                self.assertEqual(provider_name, result["_meta"]["provider_used"])

    def test_resolve_single_string_calls_canonical_exactly_once(self):
        resolve_module = import_module("ym_stock_data.v2.resolve")
        canonical = {
            "data": {"datas": [], "queries": [], "query_count": 1},
            "_meta": {
                "intent": "review_sentiment",
                "status": "empty",
                "provider_used": "iwencai_openapi",
                "source": "iwencai_openapi",
                "source_chain": ["iwencai_openapi"],
                "attempts": [],
                "fetched_at": "2026-07-29T10:00:00+08:00",
                "quality": {"status": "empty", "returned_count": 0, "reason_codes": []},
                "freshness": {"max_age_sec": 1800},
            },
        }
        with patch.object(resolve_module.public_api, "query", return_value=canonical) as call:
            resolve("review_sentiment", query="今日涨停", limit=3)

        call.assert_called_once_with(
            "review_sentiment", query="今日涨停", limit=3
        )

    def test_canonical_api_has_no_v2_import_boundary(self):
        source = inspect.getsource(api)

        self.assertNotIn(".v2", source)
        self.assertNotIn("ym_stock_data.v2", source)


if __name__ == "__main__":
    unittest.main()
