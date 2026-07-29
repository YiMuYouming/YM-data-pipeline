import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ym_stock_data.api as api
from ym_stock_data import query
from ym_stock_data.contracts import build_result as real_build_result
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome


class FakeProvider:
    def __init__(self, name, outcomes):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = []

    def probe(self):
        return {"provider": self.name, "status": "ready"}

    def call(self, intent, params):
        self.calls.append((intent, params))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def outcome(provider, status, *, data=None, error_code=None, quality=None):
    return ProviderOutcome(
        provider=provider,
        status=status,
        data=data,
        error_code=error_code,
        latency_ms=1,
        quality=quality,
    )


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state = ProviderState(Path(self.temp_dir.name) / "providers.sqlite3")
        self.state_patch = patch.object(api, "_STATE", self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def provider_patch(self, providers):
        return patch.object(
            api,
            "_provider_for",
            side_effect=lambda name: providers.get(name, api.UnavailableProvider(name)),
        )

    def test_public_import_is_query(self):
        self.assertIs(query, api.query)

    def test_registry_provider_classes_are_instantiated_before_use(self):
        class RegisteredProvider:
            def call(self, intent, params):
                return outcome("registered", "dependency_missing")

        with patch.dict(
            api.PROVIDER_REGISTRY,
            {"registered": RegisteredProvider},
        ):
            provider = api._provider_for("registered")

        self.assertIsInstance(provider, RegisteredProvider)

    def test_success_stops_the_chain(self):
        first = FakeProvider(
            "pytdx",
            [outcome("pytdx", "success", data={"上证指数": 3200})],
        )
        second = FakeProvider(
            "eastmoney",
            [AssertionError("second provider must not run")],
        )
        with self.provider_patch({"pytdx": first, "eastmoney": second}):
            result = query("realtime_market")

        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("pytdx", result["_meta"]["provider_used"])
        self.assertEqual(1, len(first.calls))
        self.assertEqual([], second.calls)

    def test_semantically_valid_empty_stops_the_chain(self):
        first = FakeProvider(
            "iwencai_openapi",
            [outcome("iwencai_openapi", "empty", data={"datas": [], "row_count": 0})],
        )
        second = FakeProvider(
            "pywencai",
            [AssertionError("valid empty must stop")],
        )
        with self.provider_patch({"iwencai_openapi": first, "pywencai": second}):
            result = query("review_sentiment", query="没有匹配股票", limit=20)

        self.assertEqual("empty", result["_meta"]["status"])
        self.assertEqual("iwencai_openapi", result["_meta"]["provider_used"])
        self.assertEqual([], second.calls)

    def test_invalid_empty_continues_to_compatible_provider(self):
        first = FakeProvider(
            "pytdx",
            [outcome("pytdx", "empty", data={})],
        )
        second = FakeProvider(
            "tencent",
            [outcome("tencent", "success", data={"600519": {"price": 1400}})],
        )
        with self.provider_patch({"pytdx": first, "tencent": second}):
            result = query("stock_snapshot", codes=["600519"])

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertEqual(
            ["provider_error", "success"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )
        self.assertEqual("INVALID_EMPTY", result["_meta"]["attempts"][0]["error_code"])

    def test_sector_empty_is_a_semantically_valid_empty_set(self):
        first = FakeProvider(
            "ths_industry",
            [outcome("ths_industry", "empty", data={"items": [], "missing": ["不存在板块"]})],
        )
        with self.provider_patch({"ths_industry": first}):
            result = query("sector_index", names=["不存在板块"])

        self.assertEqual("empty", result["_meta"]["status"])
        self.assertEqual("ths_industry", result["_meta"]["provider_used"])

    def test_uniform_row_level_fallback_promotes_actual_provider(self):
        raw = {
            "600519": {"price": 1400, "_source": "tencent_fallback"},
            "000858": {"price": 120, "_source": "tencent_fallback"},
        }
        with patch(
            "ym_stock_data.providers.local.pytdx.fetch_quotes",
            return_value=raw,
        ):
            result = query("stock_snapshot", codes=["600519", "000858"])

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertEqual(
            ["pytdx", "tencent"],
            result["_meta"]["source_chain"],
        )

    def test_mixed_row_provenance_is_not_reported_as_pytdx(self):
        mixed = {
            "600519": {"price": 1400},
            "000858": {"price": 120, "_source": "tencent_fallback"},
        }
        tencent_rows = {
            "600519": {"price": 1400},
            "000858": {"price": 120},
        }
        with patch(
            "ym_stock_data.providers.local.pytdx.fetch_quotes",
            return_value=mixed,
        ), patch(
            "ym_stock_data.providers.local.tencent.fetch_quotes",
            return_value=tencent_rows,
        ):
            result = query("stock_snapshot", codes=["600519", "000858"])

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertEqual("MIXED_PROVENANCE", result["_meta"]["attempts"][0]["error_code"])

    def test_stock_kline_count_is_applied_on_primary_path(self):
        raw = {"code": "600519", "bars": [{"time": str(index)} for index in range(5)]}
        with patch(
            "ym_stock_data.providers.local.pytdx.fetch_kline",
            return_value=raw,
        ):
            result = query("stock_kline", code="600519", period="daily", count=2)

        self.assertEqual([{"time": "3"}, {"time": "4"}], result["data"]["bars"])
        self.assertEqual(2, result["data"]["requested_count"])

    def test_compatible_failures_continue_and_degrade_success(self):
        for failed_status in (
            "auth_error",
            "dependency_missing",
            "timeout",
            "incompatible",
        ):
            with self.subTest(status=failed_status):
                first = FakeProvider(
                    "iwencai_openapi",
                    [outcome("iwencai_openapi", failed_status, error_code="EXPECTED")],
                )
                second = FakeProvider(
                    "pywencai",
                    [
                        outcome(
                            "pywencai",
                            "success",
                            data={"datas": [{"股票代码": "600519"}], "row_count": 1},
                        )
                    ],
                )
                with self.provider_patch(
                    {"iwencai_openapi": first, "pywencai": second}
                ):
                    result = query("review_sentiment", query="白酒股", limit=1)
                self.assertEqual("degraded", result["_meta"]["status"])
                self.assertEqual("pywencai", result["_meta"]["provider_used"])

    def test_total_failure_records_missing_registry_provider(self):
        first = FakeProvider(
            "eastmoney_research",
            [outcome("eastmoney_research", "provider_error", error_code="HTTP_500")],
        )
        with self.provider_patch({"eastmoney_research": first}):
            result = query("research", code="600519")

        self.assertEqual("error", result["_meta"]["status"])
        self.assertIsNone(result["_meta"]["provider_used"])
        self.assertEqual(
            ["eastmoney_research", "tdx_report"],
            result["_meta"]["source_chain"],
        )
        self.assertEqual("dependency_missing", result["_meta"]["attempts"][1]["status"])
        self.assertEqual(
            "PROVIDER_NOT_IMPLEMENTED",
            result["_meta"]["attempts"][1]["error_code"],
        )

    def test_route_external_provider_claim_is_rejected(self):
        spoof = FakeProvider(
            "pytdx",
            [outcome("wind_mcp", "success", data={"上证指数": 3200})],
        )
        fallback = FakeProvider(
            "eastmoney",
            [outcome("eastmoney", "success", data={"上证指数": 3200})],
        )
        with self.provider_patch({"pytdx": spoof, "eastmoney": fallback}):
            result = query("realtime_market")

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("eastmoney", result["_meta"]["provider_used"])
        self.assertEqual(
            "INCOMPATIBLE_PROVIDER",
            result["_meta"]["attempts"][0]["error_code"],
        )

    def test_unverified_route_internal_provider_claim_is_rejected(self):
        spoof = FakeProvider(
            "pytdx",
            [outcome("tencent", "success", data={"600519": {"price": 1400}})],
        )
        fallback = FakeProvider(
            "tencent",
            [outcome("tencent", "success", data={"600519": {"price": 1400}})],
        )
        with self.provider_patch({"pytdx": spoof, "tencent": fallback}):
            result = query("stock_snapshot", codes=["600519"])

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertEqual(
            "INCOMPATIBLE_PROVIDER",
            result["_meta"]["attempts"][0]["error_code"],
        )

    def test_parameter_validation_happens_before_provider_call(self):
        provider = FakeProvider("pytdx", [AssertionError("must not run")])
        with self.provider_patch({"pytdx": provider}):
            with self.assertRaises(ValueError):
                query("stock_snapshot")
        self.assertEqual([], provider.calls)

    def test_invalid_numeric_and_unknown_params_fail_before_provider_call(self):
        provider = FakeProvider("pytdx", [AssertionError("must not run")])
        with self.provider_patch({"pytdx": provider}):
            with self.assertRaises(ValueError):
                query("stock_kline", code="600519", count="not-a-number")
            with self.assertRaises(ValueError):
                query("stock_snapshot", codes=["600519"], mystery=True)
            with self.assertRaises(ValueError):
                query("review_sentiment", query=["涨停", "连板"])
        self.assertEqual([], provider.calls)

    def test_breaker_is_an_auditable_attempt_and_provider_is_skipped(self):
        self.state.record_failure(
            provider="iwencai_openapi",
            failure_type="auth_error",
            error_code="HTTP_401",
            breaker_seconds=300,
        )
        first = FakeProvider("iwencai_openapi", [AssertionError("must not run")])
        second = FakeProvider(
            "pywencai",
            [outcome("pywencai", "empty", data={"datas": [], "row_count": 0})],
        )
        with self.provider_patch({"iwencai_openapi": first, "pywencai": second}):
            result = query("review_sentiment", query="没有匹配股票")

        self.assertEqual([], first.calls)
        self.assertEqual("breaker_open", result["_meta"]["attempts"][0]["status"])
        self.assertEqual("HTTP_401", result["_meta"]["attempts"][0]["error_code"])

    def test_each_terminal_path_calls_build_result_exactly_once(self):
        cases = [
            (
                "success",
                {"pytdx": FakeProvider("pytdx", [outcome("pytdx", "success", data={"上证指数": 1})])},
                ("realtime_market", {}),
            ),
            (
                "empty",
                {
                    "iwencai_openapi": FakeProvider(
                        "iwencai_openapi",
                        [outcome("iwencai_openapi", "empty", data={"datas": [], "row_count": 0})],
                    )
                },
                ("review_sentiment", {"query": "没有匹配股票"}),
            ),
            (
                "error",
                {},
                ("wind_enrichment", {}),
            ),
        ]
        for expected_status, providers, (intent, params) in cases:
            with self.subTest(status=expected_status), self.provider_patch(providers), patch.object(
                api,
                "build_result",
                wraps=real_build_result,
            ) as build:
                result = query(intent, **params)
            self.assertEqual(expected_status, result["_meta"]["status"])
            build.assert_called_once()

    def test_keyboard_interrupt_is_not_caught(self):
        provider = FakeProvider("pytdx", [KeyboardInterrupt()])
        with self.provider_patch({"pytdx": provider}):
            with self.assertRaises(KeyboardInterrupt):
                query("realtime_market")


if __name__ == "__main__":
    unittest.main()
