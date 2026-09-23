import tempfile
import unittest
from datetime import datetime
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


def outcome(
    provider, status, *, data=None, error_code=None, quality=None, auth=None
):
    return ProviderOutcome(
        provider=provider,
        status=status,
        data=data,
        error_code=error_code,
        latency_ms=1,
        quality=quality,
        auth=auth,
    )


def full_index_data():
    return {
        "上证指数": 3200.0,
        "深证指数": 10000.0,
        "创业指数": 2000.0,
    }


def full_snapshot_data(*codes):
    return {
        code: {
            "code": code,
            "price": 1400.0,
            "last_close": 1390.0,
            "open": 1395.0,
            "high": 1410.0,
            "low": 1385.0,
            "volume": 1000.0,
            "amount": 1000000.0,
            "quote_time": datetime.now().isoformat(timespec="seconds"),
        }
        for code in codes
    }


def full_kline_data(adjustment="none"):
    return {
        "bars": [
            {
                "datetime": "2026-09-22 15:00:00",
                "open": 1395.0,
                "high": 1410.0,
                "low": 1385.0,
                "close": 1400.0,
                "volume": 1000.0,
                "amount": 1000000.0,
            }
        ],
        "adjustment": adjustment,
        "volume_unit": "share",
        "amount_unit": "CNY",
    }


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
            "stocktoday",
            [outcome("stocktoday", "success", data=full_index_data())],
        )
        second = FakeProvider(
            "tencent",
            [AssertionError("second provider must not run")],
        )
        with self.provider_patch({"stocktoday": first, "tencent": second}):
            result = query("realtime_market")

        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("stocktoday", result["_meta"]["provider_used"])
        self.assertEqual(1, len(first.calls))
        self.assertEqual([], second.calls)

    def test_stocktoday_primary_success_is_reported_as_primary(self):
        cases = [
            (
                "realtime_market",
                {},
                {"上证指数": 3200, "深证指数": 10000, "创业指数": 2000},
            ),
            (
                "stock_snapshot",
                {"codes": ["600519"]},
                full_snapshot_data("600519"),
            ),
            (
                "stock_kline",
                {"code": "600519", "period": "daily", "count": 1},
                full_kline_data(),
            ),
            (
                "stock_kline",
                {"code": "600519", "period": "15m", "count": 1},
                full_kline_data(),
            ),
        ]
        for intent, params, data in cases:
            with self.subTest(intent=intent):
                stocktoday = FakeProvider(
                    "stocktoday",
                    [outcome("stocktoday", "success", data=data)],
                )
                fallback = FakeProvider(
                    "tencent", [AssertionError("fallback must not run")]
                )
                with self.provider_patch(
                    {"stocktoday": stocktoday, "tencent": fallback}
                ):
                    result = query(intent, **params)
                self.assertEqual("success", result["_meta"]["status"])
                self.assertEqual("stocktoday", result["_meta"]["provider_used"])
                self.assertEqual("primary", result["_meta"]["source_tier"])
                self.assertEqual([], fallback.calls)

    def test_stocktoday_error_or_empty_reaches_existing_source_as_degraded(self):
        cases = [
            (
                "realtime_market",
                {},
                full_index_data(),
            ),
            (
                "stock_snapshot",
                {"codes": ["600519"]},
                full_snapshot_data("600519"),
            ),
            (
                "stock_kline",
                {"code": "600519", "period": "daily", "count": 1},
                full_kline_data(),
            ),
        ]
        for failed_status in ("provider_error", "empty"):
            for intent, params, fallback_data in cases:
                with self.subTest(status=failed_status, intent=intent):
                    stocktoday = FakeProvider(
                        "stocktoday",
                        [
                            outcome(
                                "stocktoday",
                                failed_status,
                                data=(
                                    (
                                        {"bars": [], "_stocktoday": {}}
                                        if intent == "stock_kline"
                                        else {"_stocktoday": {}}
                                    )
                                    if failed_status == "empty"
                                    else None
                                ),
                                error_code=(
                                    "UPSTREAM_ERROR"
                                    if failed_status == "provider_error"
                                    else None
                                ),
                            )
                        ],
                    )
                    fallback = FakeProvider(
                        "tencent", [outcome("tencent", "success", data=fallback_data)]
                    )
                    providers = {"stocktoday": stocktoday, "tencent": fallback}
                    if intent == "stock_kline":
                        providers["eastmoney_stock"] = FakeProvider(
                            "eastmoney_stock",
                            [outcome("eastmoney_stock", "provider_error", error_code="UPSTREAM")],
                        )
                    with self.provider_patch(providers):
                        result = query(intent, **params)
                    self.assertEqual("degraded", result["_meta"]["status"])
                    self.assertEqual("tencent", result["_meta"]["provider_used"])
                    self.assertEqual("fallback", result["_meta"]["source_tier"])
                    expected_attempts = [failed_status, "success"]
                    if intent == "stock_kline":
                        expected_attempts.insert(1, "provider_error")
                    self.assertEqual(
                        expected_attempts,
                        [item["status"] for item in result["_meta"]["attempts"]],
                    )

    def test_explicit_stocktoday_source_remains_single_source(self):
        stocktoday = FakeProvider(
            "stocktoday",
            [outcome("stocktoday", "provider_error", error_code="UPSTREAM_ERROR")],
        )
        fallback = FakeProvider("tencent", [AssertionError("must not run")])
        with self.provider_patch({"stocktoday": stocktoday, "tencent": fallback}):
            result = query("stock_snapshot", codes=["600519"], source="stocktoday")

        self.assertEqual("error", result["_meta"]["status"])
        self.assertEqual(["stocktoday"], result["_meta"]["source_chain"])
        self.assertEqual([], fallback.calls)

    def test_new_limit_board_uses_stocktoday_but_existing_semantics_do_not(self):
        primary = FakeProvider(
            "stocktoday",
            [
                outcome(
                    "stocktoday",
                    "success",
                    data={"kind": "up", "items": [{"code": "600519"}]},
                )
            ],
        )
        fallback = FakeProvider(
            "eastmoney_limit_pool", [AssertionError("fallback must not run")]
        )
        with self.provider_patch(
            {"stocktoday": primary, "eastmoney_limit_pool": fallback}
        ):
            result = query("market_limit_board", kind="up")
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("stocktoday", result["_meta"]["provider_used"])
        self.assertEqual("primary", result["_meta"]["source_tier"])

        sector = FakeProvider(
            "ths_industry",
            [
                outcome(
                    "ths_industry",
                    "success",
                    data={"items": [{"name": "半导体", "change_pct": 1.2}], "missing": []},
                )
            ],
        )
        with self.provider_patch({"ths_industry": sector}):
            result = query("sector_index", names=["半导体"])
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("ths_industry", result["_meta"]["provider_used"])
        self.assertEqual("primary", result["_meta"]["source_tier"])

        aggregate = FakeProvider(
            "eastmoney_limit_pool",
            [
                outcome(
                    "eastmoney_limit_pool",
                    "success",
                    data={
                        "date": "20260923",
                        "zt_count": 1,
                        "zb_count": 0,
                        "dt_count": 0,
                        "break_rate": 0.0,
                        "max_board": 1,
                        "pools": {"zt": [{"code": "600519"}], "zb": [], "dt": [], "yzt": []},
                    },
                )
            ],
        )
        with self.provider_patch({"eastmoney_limit_pool": aggregate}):
            result = query("market_limit_state")
        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual("eastmoney_limit_pool", result["_meta"]["provider_used"])

    def test_limit_board_fallback_is_explicit_and_hot_rank_has_no_silent_substitute(self):
        stocktoday = FakeProvider(
            "stocktoday",
            [outcome("stocktoday", "provider_error", error_code="UPSTREAM_ERROR")],
        )
        eastmoney = FakeProvider(
            "eastmoney_limit_pool",
            [
                outcome(
                    "eastmoney_limit_pool",
                    "success",
                    data={
                        "kind": "up",
                        "date": "20260923",
                        "items": [{"code": "600519"}],
                    },
                )
            ],
        )
        with self.provider_patch(
            {"stocktoday": stocktoday, "eastmoney_limit_pool": eastmoney}
        ):
            result = query("market_limit_board", kind="up")
        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("eastmoney_limit_pool", result["_meta"]["provider_used"])
        self.assertEqual("fallback", result["_meta"]["source_tier"])
        self.assertEqual(
            ["provider_error", "success"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )

        hot = FakeProvider(
            "stocktoday",
            [outcome("stocktoday", "provider_error", error_code="UPSTREAM_ERROR")],
        )
        with self.provider_patch({"stocktoday": hot}):
            result = query("market_hot_rank", source="ths")
        self.assertEqual("error", result["_meta"]["status"])
        self.assertIsNone(result["_meta"]["provider_used"])
        self.assertEqual(
            "no_semantically_equivalent_hot_rank_fallback",
            result["_meta"]["source_gap"],
        )

    def test_explicit_screen_openapi_empty_continues_to_pywencai_success(self):
        first = FakeProvider(
            "iwencai_openapi",
            [outcome("iwencai_openapi", "empty", data={"datas": [], "row_count": 0})],
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
        with self.provider_patch({"iwencai_openapi": first, "pywencai": second}):
            result = query("review_sentiment", query="白酒股", limit=20)

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("pywencai", result["_meta"]["provider_used"])
        self.assertEqual(
            ["empty", "success"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )

    def test_explicit_screen_two_empties_continue_to_tdx_success(self):
        providers = {
            "iwencai_openapi": FakeProvider(
                "iwencai_openapi",
                [
                    outcome(
                        "iwencai_openapi",
                        "empty",
                        data={"datas": [], "row_count": 0},
                    )
                ],
            ),
            "pywencai": FakeProvider(
                "pywencai",
                [outcome("pywencai", "empty", data={"datas": [], "row_count": 0})],
            ),
            "tdx_screener": FakeProvider(
                "tdx_screener",
                [
                    outcome(
                        "tdx_screener",
                        "success",
                        data={"datas": [{"股票代码": "600519"}], "row_count": 1},
                    )
                ],
            ),
        }
        with self.provider_patch(providers):
            result = query("review_sentiment", query="白酒股", limit=20)

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("tdx_screener", result["_meta"]["provider_used"])
        self.assertEqual(
            ["empty", "empty", "success"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )

    def test_explicit_screen_reaches_wind_after_three_compatible_attempts(self):
        providers = {
            "iwencai_openapi": FakeProvider(
                "iwencai_openapi",
                [outcome("iwencai_openapi", "empty", data={"datas": [], "row_count": 0})],
            ),
            "pywencai": FakeProvider(
                "pywencai",
                [outcome("pywencai", "provider_error", error_code="UPSTREAM_ERROR")],
            ),
            "tdx_screener": FakeProvider(
                "tdx_screener",
                [outcome("tdx_screener", "empty", data={"datas": [], "row_count": 0})],
            ),
            "wind_screener": FakeProvider(
                "wind_screener",
                [
                    outcome(
                        "wind_screener",
                        "success",
                        data={"datas": [{"股票代码": "600519"}], "row_count": 1},
                        auth={"required": True, "status": "ok"},
                    )
                ],
            ),
        }
        with self.provider_patch(providers):
            result = query(
                "review_sentiment",
                query="白酒股",
                limit=20,
                lang="English",
                version="v2",
            )

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("wind_screener", result["_meta"]["provider_used"])
        self.assertEqual(
            ["empty", "provider_error", "empty", "success"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )
        self.assertEqual("English", providers["wind_screener"].calls[0][1]["lang"])
        self.assertEqual("v2", providers["wind_screener"].calls[0][1]["version"])
        self.assertEqual(
            {"required": True, "status": "ok"},
            result["_meta"]["auth"],
        )

    def test_explicit_screen_all_compatible_providers_empty_is_auditable(self):
        providers = {
            name: FakeProvider(
                name,
                [
                    outcome(
                        name,
                        "empty",
                        data={"datas": [], "row_count": 0},
                        auth={"required": True, "status": f"ok-{name}"},
                    )
                ],
            )
            for name in (
                "iwencai_openapi",
                "pywencai",
                "tdx_screener",
                "wind_screener",
            )
        }
        with self.provider_patch(providers):
            result = query("review_sentiment", query="没有匹配股票", limit=20)

        self.assertEqual("empty", result["_meta"]["status"])
        self.assertEqual("wind_screener", result["_meta"]["provider_used"])
        self.assertEqual(
            ["iwencai_openapi", "pywencai", "tdx_screener", "wind_screener"],
            result["_meta"]["source_chain"],
        )
        self.assertEqual(
            ["empty", "empty", "empty", "empty"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )
        self.assertEqual("empty", result["_meta"]["quality"]["status"])
        self.assertEqual(0, result["_meta"]["quality"]["returned_count"])
        self.assertEqual(
            {"required": True, "status": "ok-wind_screener"},
            result["_meta"]["auth"],
        )

    def test_explicit_screen_last_empty_does_not_overwrite_prior_failures(self):
        statuses = (
            ("iwencai_openapi", "auth_error", "HTTP_401"),
            ("pywencai", "provider_error", "UPSTREAM_ERROR"),
            ("tdx_screener", "empty", None),
            ("wind_screener", "empty", None),
        )
        providers = {
            name: FakeProvider(
                name,
                [
                    outcome(
                        name,
                        status,
                        data={"datas": [], "row_count": 0}
                        if status == "empty"
                        else None,
                        error_code=error_code,
                        auth={"required": True, "status": "error"}
                        if status == "auth_error"
                        else {"required": True, "status": "ok"}
                        if status == "empty"
                        else {"required": True, "status": "present"},
                    )
                ],
            )
            for name, status, error_code in statuses
        }

        with self.provider_patch(providers):
            result = query("review_sentiment", query="没有匹配股票", limit=20)

        self.assertEqual("error", result["_meta"]["status"])
        self.assertIsNone(result["_meta"]["provider_used"])
        self.assertIsNone(result["data"])
        self.assertEqual(
            ["auth_error", "provider_error", "empty", "empty"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )
        self.assertEqual(
            {"required": True, "status": "error"},
            result["_meta"]["auth"],
        )

    def test_explicit_screen_initial_empty_then_remaining_failures_is_error(self):
        statuses = (
            ("iwencai_openapi", "empty", None),
            ("pywencai", "auth_error", "HTTP_401"),
            ("tdx_screener", "provider_error", "UPSTREAM_ERROR"),
            ("wind_screener", "dependency_missing", "CLI_NOT_FOUND"),
        )
        providers = {
            name: FakeProvider(
                name,
                [
                    outcome(
                        name,
                        status,
                        data={"datas": [], "row_count": 0}
                        if status == "empty"
                        else None,
                        error_code=error_code,
                    )
                ],
            )
            for name, status, error_code in statuses
        }

        with self.provider_patch(providers):
            result = query("review_sentiment", query="没有匹配股票", limit=20)

        self.assertEqual("error", result["_meta"]["status"])
        self.assertIsNone(result["_meta"]["provider_used"])
        self.assertEqual(
            ["empty", "auth_error", "provider_error", "dependency_missing"],
            [attempt["status"] for attempt in result["_meta"]["attempts"]],
        )

    def test_invalid_empty_continues_to_compatible_provider(self):
        first = FakeProvider(
            "stocktoday",
            [outcome("stocktoday", "empty", data={})],
        )
        second = FakeProvider(
            "tencent",
            [outcome("tencent", "success", data=full_snapshot_data("600519"))],
        )
        with self.provider_patch({"stocktoday": first, "tencent": second}):
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
        stocktoday = FakeProvider(
            "stocktoday", [outcome("stocktoday", "empty", data={"items": []})]
        )
        with self.provider_patch({"stocktoday": stocktoday, "ths_industry": first}):
            result = query("sector_index", names=["不存在板块"])

        self.assertEqual("empty", result["_meta"]["status"])
        self.assertEqual("ths_industry", result["_meta"]["provider_used"])

    def test_uniform_row_level_fallback_promotes_actual_provider(self):
        raw = full_snapshot_data("600519", "000858")
        raw["600519"]["_source"] = "tencent_fallback"
        raw["000858"]["_source"] = "tencent_fallback"
        stocktoday = FakeProvider("stocktoday", [outcome("stocktoday", "provider_error", error_code="UPSTREAM")])
        tencent = FakeProvider("tencent", [outcome("tencent", "success", data=raw)])
        with self.provider_patch({"stocktoday": stocktoday, "tencent": tencent}):
            result = query("stock_snapshot", codes=["600519", "000858"])

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertEqual(
            ["stocktoday", "tencent"],
            result["_meta"]["source_chain"],
        )

    def test_mixed_row_provenance_is_not_reported_as_pytdx(self):
        tencent_rows = full_snapshot_data("600519", "000858")
        stocktoday = FakeProvider("stocktoday", [outcome("stocktoday", "provider_error", error_code="UPSTREAM")])
        tencent = FakeProvider("tencent", [outcome("tencent", "success", data=tencent_rows)])
        with self.provider_patch({"stocktoday": stocktoday, "tencent": tencent}):
            result = query("stock_snapshot", codes=["600519", "000858"])

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("tencent", result["_meta"]["provider_used"])
        self.assertEqual(["stocktoday", "tencent"], result["_meta"]["source_chain"])

    def test_stock_kline_count_is_applied_on_primary_path(self):
        raw = {
            "code": "600519",
            "bars": [
                {
                    "time": f"2026-09-{18 + index}",
                    "open": 1395.0,
                    "high": 1410.0,
                    "low": 1385.0,
                    "close": 1400.0 + index,
                    "vol": 1000.0,
                    "amount": 1000000.0,
                }
                for index in range(5)
            ],
        }
        stocktoday = FakeProvider(
            "stocktoday", [outcome("stocktoday", "provider_error", error_code="UPSTREAM")]
        )
        tencent = FakeProvider(
            "tencent", [outcome("tencent", "provider_error", error_code="UPSTREAM")]
        )
        with patch.dict(
            api.PROVIDER_REGISTRY,
            {"stocktoday": stocktoday, "tencent": tencent},
        ), patch(
            "ym_stock_data.providers.local.pytdx.fetch_kline",
            return_value=raw,
        ):
            result = query("stock_kline", code="600519", period="daily", count=2)

        self.assertEqual(
            ["2026-09-21", "2026-09-22"],
            [bar["datetime"] for bar in result["data"]["bars"]],
        )
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
        stocktoday = FakeProvider(
            "stocktoday",
            [outcome("wind_mcp", "success", data=full_index_data())],
        )
        spoof = FakeProvider(
            "pytdx",
            [outcome("wind_mcp", "success", data=full_index_data())],
        )
        fallback = FakeProvider(
            "eastmoney",
            [outcome("eastmoney", "success", data=full_index_data())],
        )
        with self.provider_patch(
            {"stocktoday": stocktoday, "pytdx": spoof, "eastmoney": fallback}
        ):
            result = query("realtime_market")

        self.assertEqual("degraded", result["_meta"]["status"])
        self.assertEqual("eastmoney", result["_meta"]["provider_used"])
        self.assertEqual(
            "INCOMPATIBLE_PROVIDER",
            result["_meta"]["attempts"][0]["error_code"],
        )

    def test_unverified_route_internal_provider_claim_is_rejected(self):
        stocktoday = FakeProvider(
            "stocktoday",
            [outcome("tencent", "success", data=full_snapshot_data("600519"))],
        )
        spoof = FakeProvider(
            "pytdx",
            [outcome("tencent", "success", data=full_snapshot_data("600519"))],
        )
        fallback = FakeProvider(
            "tencent",
            [outcome("tencent", "success", data=full_snapshot_data("600519"))],
        )
        with self.provider_patch(
            {"stocktoday": stocktoday, "pytdx": spoof, "tencent": fallback}
        ):
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
            with self.assertRaises(ValueError):
                query("review_sentiment", query="涨停", lang="Spanish")
            with self.assertRaises(ValueError):
                query("review_sentiment", query="涨停", version=" ")
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
                {
                    "stocktoday": FakeProvider(
                        "stocktoday",
                        [outcome("stocktoday", "success", data=full_index_data())],
                    )
                },
                ("realtime_market", {}),
            ),
            (
                "empty",
                {
                    "stocktoday": FakeProvider(
                        "stocktoday",
                        [outcome("stocktoday", "empty", data={"items": []})],
                    ),
                    "ths_industry": FakeProvider(
                        "ths_industry",
                        [outcome("ths_industry", "empty", data={"items": []})],
                    )
                },
                ("sector_index", {"names": ["不存在板块"]}),
            ),
            (
                "error",
                {},
                (
                    "wind_enrichment",
                    {
                        "capability": "fundamentals",
                        "params": {"question": "600519 ROE"},
                    },
                ),
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
        provider = FakeProvider("stocktoday", [KeyboardInterrupt()])
        with self.provider_patch({"stocktoday": provider}):
            with self.assertRaises(KeyboardInterrupt):
                query("realtime_market")


if __name__ == "__main__":
    unittest.main()
