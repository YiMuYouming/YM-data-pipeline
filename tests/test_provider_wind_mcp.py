import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ym_stock_data.api as api
from ym_stock_data.doctor import collect_diagnostics
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.wind_mcp import (
    WIND_ENRICHMENT_CAPABILITIES,
    WIND_EVENT_ALLOWLIST,
    WIND_PROVIDER_NAMES,
    WindMcpProvider,
    discover_wind_runtime,
)
from ym_stock_data.routing import all_route_specs, route_for
from ym_stock_data.v2 import capability_manifest


class WindProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.skill_dir = self.root / "wind-skill"
        scripts = self.skill_dir / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "cli.mjs").write_text("// test fixture\n", encoding="utf-8")
        self.config_path = self.root / "wind-config"
        self.config_path.write_text("configured", encoding="utf-8")

    @staticmethod
    def completed(payload, *, returncode=0):
        return subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="redacted",
        )

    def provider(self, name="wind_mcp", *, runner=None, skill_dir=None):
        return WindMcpProvider(
            name,
            skill_dir=self.skill_dir if skill_dir is None else skill_dir,
            config_path=self.config_path,
            runner=runner or Mock(),
        )

    def test_real_cli_tabular_shape_is_normalized_without_relaxing_unknown_shapes(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "wind_company_profile_success.json"
        )
        envelope = json.loads(fixture_path.read_text(encoding="utf-8"))
        runner = Mock(return_value=self.completed(envelope))

        outcome = self.provider(runner=runner).call(
            "wind_enrichment",
            {"capability": "company_profile", "code": "SAMPLE_CODE"},
        )

        self.assertEqual("success", outcome.status)
        self.assertEqual(
            [
                {
                    "sample_code": "SAMPLE_CODE",
                    "sample_label": "SAMPLE_LABEL",
                }
            ],
            outcome.data["items"],
        )
        self.assertEqual(1, outcome.quality["returned_count"])

        malformed_payloads = (
            {"data": {"data": [{"rows": [["value"]]}]}},
            {"data": {"data": [{"columns": [], "rows": "not-a-list"}]}},
            {
                "data": {
                    "data": [
                        {
                            "columns": [{"name": "only", "type": "string"}],
                            "rows": [["one", "extra"]],
                        }
                    ]
                }
            },
            {"data": {"unknown": []}},
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                rejected = self.provider(
                    runner=Mock(
                        return_value=self.completed(
                            {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps(payload),
                                    }
                                ],
                                "isError": False,
                            }
                        )
                    )
                ).call(
                    "wind_enrichment",
                    {"capability": "company_profile", "code": "SAMPLE_CODE"},
                )
                self.assertEqual("provider_error", rejected.status)
                self.assertEqual("INVALID_RESPONSE", rejected.error_code)

    def test_only_non_realtime_research_capabilities_are_exposed(self):
        self.assertEqual(
            {
                "company_profile",
                "fundamentals",
                "equity_holders",
                "company_events",
                "risk_metrics",
                "index_fundamentals",
                "announcements",
            },
            set(WIND_ENRICHMENT_CAPABILITIES),
        )
        tools = {spec["tool_name"] for spec in WIND_ENRICHMENT_CAPABILITIES.values()}
        for forbidden in (
            "get_stock_price_indicators",
            "get_stock_kline",
            "get_minute_data",
            "search_stocks",
            "get_news",
        ):
            self.assertNotIn(forbidden, tools)

    def test_success_uses_list_argv_shell_false_and_never_passes_key(self):
        runner = Mock(
            return_value=self.completed(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"rows": [{"windcode": "600519.SH", "roe": 0.31}]}
                            ),
                        }
                    ]
                }
            )
        )
        provider = self.provider(runner=runner)

        outcome = provider.call(
            "wind_enrichment",
            {
                "capability": "fundamentals",
                "params": {"question": "贵州茅台 2025年 ROE"},
            },
        )

        self.assertEqual("success", outcome.status)
        self.assertEqual("wind_mcp", outcome.provider)
        self.assertEqual(1, len(outcome.data["items"]))
        command = runner.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertEqual("node", command[0])
        self.assertEqual("stock_data", command[3])
        self.assertEqual("get_stock_fundamentals", command[4])
        self.assertEqual(
            {"question": "贵州茅台 2025年 ROE", "lang": "中文"},
            json.loads(command[5]),
        )
        serialized = " ".join(command).lower()
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("authorization", serialized)
        self.assertFalse(runner.call_args.kwargs["shell"])

    @staticmethod
    def screener_envelope(rows, *, columns=None):
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "data": {
                                "data": [
                                    {
                                        "columns": columns
                                        if columns is not None
                                        else [
                                            {"name": "Wind代码", "type": "string"}
                                        ],
                                        "rows": rows,
                                    }
                                ]
                            },
                            "error": None,
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            "isError": False,
        }

    def test_wind_screener_uses_exact_search_stocks_argv_and_optional_version(self):
        runner = Mock(
            return_value=self.completed(self.screener_envelope([["600519.SH"]]))
        )
        provider = self.provider(name="wind_screener", runner=runner)

        outcome = provider.call(
            "review_sentiment",
            {"query": "A股 白酒 非ST", "limit": 20},
        )

        self.assertEqual("success", outcome.status)
        self.assertEqual(
            {
                "datas": [
                    {"股票代码": "600519", "Wind代码": "600519.SH"}
                ],
                "row_count": 1,
            },
            outcome.data,
        )
        command = runner.call_args.args[0]
        self.assertEqual("stock_data", command[3])
        self.assertEqual("search_stocks", command[4])
        self.assertEqual(
            {"question": "A股 白酒 非ST", "lang": "中文"},
            json.loads(command[5]),
        )
        self.assertNotIn("version", json.loads(command[5]))

        version_runner = Mock(
            return_value=self.completed(self.screener_envelope([["000858.SZ"]]))
        )
        versioned = self.provider(
            name="wind_screener", runner=version_runner
        ).call(
            "review_sentiment",
            {
                "query": "A share   liquor\tstocks",
                "limit": 20,
                "lang": "English",
                "version": "v2",
            },
        )

        self.assertEqual("success", versioned.status)
        self.assertEqual(
            {
                "question": "A share liquor stocks",
                "lang": "English",
                "version": "v2",
            },
            json.loads(version_runner.call_args.args[0][5]),
        )

    def test_wind_screener_stock_code_shape_is_snapshot_compatible(self):
        wind = self.provider(
            name="wind_screener",
            runner=Mock(
                return_value=self.completed(
                    self.screener_envelope([["600519.SH"]])
                )
            ),
        ).call("review_sentiment", {"query": "白酒", "limit": 20})
        stock_code = wind.data["datas"][0]["股票代码"]
        self.assertRegex(stock_code, r"^\d{6}$")

        class SnapshotProvider:
            name = "pytdx"

            def __init__(self):
                self.calls = []

            def call(self, intent, params):
                self.calls.append((intent, params))
                return ProviderOutcome(
                    provider=self.name,
                    status="success",
                    data={stock_code: {"price": 1400}},
                    latency_ms=1,
                    auth={"required": False, "status": "not_required"},
                )

        snapshot_provider = SnapshotProvider()
        with tempfile.TemporaryDirectory() as directory:
            state = ProviderState(Path(directory) / "state.sqlite3")
            with patch.object(api, "_STATE", state), patch.object(
                api, "_provider_for", return_value=snapshot_provider
            ):
                snapshot = api.query("stock_snapshot", codes=[stock_code])

        self.assertEqual("success", snapshot["_meta"]["status"])
        self.assertEqual([stock_code], snapshot_provider.calls[0][1]["codes"])

    def test_wind_screener_empty_error_auth_and_malformed_are_explicit(self):
        empty = self.provider(
            name="wind_screener",
            runner=Mock(return_value=self.completed(self.screener_envelope([]))),
        ).call("review_sentiment", {"query": "没有匹配股票", "limit": 20})
        auth = self.provider(
            name="wind_screener",
            runner=Mock(
                return_value=self.completed(
                    {"error": {"code": "AUTH_ERROR"}}, returncode=1
                )
            ),
        ).call("review_sentiment", {"query": "白酒", "limit": 20})
        error = self.provider(
            name="wind_screener",
            runner=Mock(
                return_value=self.completed(
                    {"error": {"code": "BACKEND_ERROR"}}, returncode=1
                )
            ),
        ).call("review_sentiment", {"query": "白酒", "limit": 20})

        self.assertEqual("empty", empty.status)
        self.assertEqual({"datas": [], "row_count": 0}, empty.data)
        self.assertEqual(("auth_error", "AUTH_ERROR"), (auth.status, auth.error_code))
        self.assertEqual(
            ("provider_error", "WIND_CLI_ERROR"),
            (error.status, error.error_code),
        )

        malformed_payloads = (
            {"rows": [{"Wind代码": "600519.SH"}]},
            {
                "data": {
                    "data": [
                        {
                            "columns": [{"name": "股票代码", "type": "string"}],
                            "rows": [["600519.SH"]],
                        }
                    ]
                }
            },
            self.screener_envelope([["AAPL.O"]]),
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                envelope = (
                    payload
                    if "content" in payload
                    else {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(payload, ensure_ascii=False),
                            }
                        ]
                    }
                )
                rejected = self.provider(
                    name="wind_screener",
                    runner=Mock(return_value=self.completed(envelope)),
                ).call("review_sentiment", {"query": "白酒", "limit": 20})
                self.assertEqual("provider_error", rejected.status)
                self.assertEqual("INVALID_RESPONSE", rejected.error_code)

    def test_wind_screener_empty_table_still_requires_exact_code_column(self):
        for columns in (
            [],
            [{"name": "股票代码", "type": "string"}],
            [{"name": " Wind代码 ", "type": "string"}],
        ):
            with self.subTest(columns=columns):
                rejected = self.provider(
                    name="wind_screener",
                    runner=Mock(
                        return_value=self.completed(
                            self.screener_envelope([], columns=columns)
                        )
                    ),
                ).call("review_sentiment", {"query": "白酒", "limit": 20})

                self.assertEqual("provider_error", rejected.status)
                self.assertEqual("INVALID_RESPONSE", rejected.error_code)

    def test_wind_screener_rejects_non_a_share_and_exchange_mismatched_codes(self):
        for code in (
            "000001.SH",
            "600519.SZ",
            "510300.SH",
            "999999.BJ",
            "430047.BJ",
            "830001.BJ",
            "870001.BJ",
            "880001.BJ",
        ):
            with self.subTest(code=code):
                rejected = self.provider(
                    name="wind_screener",
                    runner=Mock(
                        return_value=self.completed(self.screener_envelope([[code]]))
                    ),
                ).call("review_sentiment", {"query": "A股", "limit": 20})

                self.assertEqual("provider_error", rejected.status)
                self.assertEqual("INVALID_RESPONSE", rejected.error_code)

        beijing = self.provider(
            name="wind_screener",
            runner=Mock(
                return_value=self.completed(
                    self.screener_envelope([["920001.BJ"]])
                )
            ),
        ).call("review_sentiment", {"query": "北交所股票", "limit": 20})
        self.assertEqual("success", beijing.status)

    def test_missing_fixed_config_does_not_preempt_cli_auth_resolution(self):
        missing_config = self.root / "not-present"
        success_runner = Mock(
            return_value=self.completed(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"rows": [{"value": 1}]}),
                        }
                    ]
                }
            )
        )
        provider = WindMcpProvider(
            "wind_mcp",
            skill_dir=self.skill_dir,
            config_path=missing_config,
            runner=success_runner,
        )

        success = provider.call(
            "wind_enrichment",
            {"capability": "fundamentals", "params": {"question": "600519 ROE"}},
        )
        auth_runner = Mock(
            return_value=self.completed(
                {"error": {"code": "AUTH_ERROR"}}, returncode=1
            )
        )
        auth_failure = WindMcpProvider(
            "wind_mcp",
            skill_dir=self.skill_dir,
            config_path=missing_config,
            runner=auth_runner,
        ).call(
            "wind_enrichment",
            {"capability": "fundamentals", "params": {"question": "600519 ROE"}},
        )

        self.assertEqual("success", success.status)
        self.assertEqual("auth_error", auth_failure.status)
        self.assertEqual("AUTH_ERROR", auth_failure.error_code)
        self.assertEqual("configured_unverified", provider.probe()["status"])
        self.assertEqual("unverified", provider.probe()["auth"]["status"])

    def test_cli_missing_auth_timeout_invalid_json_and_embedded_errors_are_explicit(self):
        missing = self.provider(skill_dir=self.root / "missing")
        missing._runtime = Mock(return_value=None)
        cases = (
            (
                missing,
                "dependency_missing",
                "CLI_NOT_FOUND",
            ),
            (
                self.provider(
                    runner=Mock(
                        return_value=self.completed(
                            {"error": {"code": "AUTH_ERROR"}}, returncode=1
                        )
                    )
                ),
                "auth_error",
                "AUTH_ERROR",
            ),
            (
                self.provider(
                    runner=Mock(
                        side_effect=subprocess.TimeoutExpired(cmd=["node"], timeout=5)
                    )
                ),
                "timeout",
                "TIMEOUT",
            ),
            (
                self.provider(
                    runner=Mock(
                        return_value=subprocess.CompletedProcess(
                            args=[], returncode=0, stdout="not-json", stderr="redacted"
                        )
                    )
                ),
                "provider_error",
                "INVALID_RESPONSE",
            ),
            (
                self.provider(
                    runner=Mock(
                        return_value=self.completed(
                            {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps(
                                            {
                                                "error": {
                                                    "code": "NO_RESULTS",
                                                    "message": "PSEUDO_TOKEN SECRET_PAYLOAD",
                                                }
                                            }
                                        ),
                                    }
                                ]
                            }
                        )
                    )
                ),
                "provider_error",
                "WIND_PAYLOAD_ERROR",
            ),
            (
                self.provider(
                    runner=Mock(
                        return_value=self.completed(
                            {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps({"success": False, "rows": []}),
                                    }
                                ]
                            }
                        )
                    )
                ),
                "provider_error",
                "WIND_PAYLOAD_ERROR",
            ),
            (
                self.provider(
                    runner=Mock(side_effect=OSError("PSEUDO_KEY SECRET_OSERROR"))
                ),
                "dependency_missing",
                "RUNTIME_ERROR",
            ),
            (
                self.provider(
                    runner=Mock(
                        return_value=subprocess.CompletedProcess(
                            args=[],
                            returncode=1,
                            stdout=json.dumps(
                                {
                                    "error": {
                                        "code": "REMOTE_INTERNAL_DETAIL",
                                        "message": "PSEUDO_TOKEN SECRET_STDOUT",
                                    }
                                }
                            ),
                            stderr="PSEUDO_KEY SECRET_STDERR",
                        )
                    )
                ),
                "provider_error",
                "WIND_CLI_ERROR",
            ),
        )
        for provider, status, error_code in cases:
            with self.subTest(status=status, error_code=error_code):
                outcome = provider.call(
                    "wind_enrichment",
                    {
                        "capability": "fundamentals",
                        "params": {"question": "600519.SH ROE"},
                    },
                )
                self.assertEqual(status, outcome.status)
                self.assertEqual(error_code, outcome.error_code)
                self.assertIsNone(outcome.data)
                rendered = json.dumps(outcome.__dict__, ensure_ascii=False)
                for forbidden in (
                    "PSEUDO_KEY",
                    "PSEUDO_TOKEN",
                    "SECRET_OSERROR",
                    "SECRET_PAYLOAD",
                    "SECRET_STDOUT",
                    "SECRET_STDERR",
                    "REMOTE_INTERNAL_DETAIL",
                ):
                    self.assertNotIn(forbidden, rendered)

    def test_unproven_stock_events_are_incompatible_and_filings_are_strict(self):
        self.assertEqual(set(), set(WIND_EVENT_ALLOWLIST))
        runner = Mock(
            return_value=self.completed(
                {"content": [{"type": "text", "text": json.dumps({"filings": []})}]}
            )
        )
        event_provider = self.provider(runner=runner)
        blocked = [
            event_provider.call("stock_event", {"event": event, "code": "600519"})
            for event in ("lockup", "margin", "block_trade", "holder_num", "dividend")
        ]
        documents = self.provider(name="wind_documents", runner=runner).call(
            "filings", {"code": "600519", "days": 90, "max_pages": 1}
        )

        self.assertTrue(all(outcome.status == "incompatible" for outcome in blocked))
        self.assertEqual("empty", documents.status)

    def test_filing_fallback_rejects_missing_wrong_or_generic_containers(self):
        payloads = ({}, {"filings": {}}, {"rows": []}, {"text": "没有公告"})
        for payload in payloads:
            with self.subTest(payload=payload):
                runner = Mock(
                    return_value=self.completed(
                        {"content": [{"type": "text", "text": json.dumps(payload)}]}
                    )
                )
                outcome = self.provider(
                    name="wind_documents", runner=runner
                ).call("filings", {"code": "600519"})
                self.assertEqual("provider_error", outcome.status)
                self.assertEqual("INVALID_RESPONSE", outcome.error_code)

    def test_public_query_uses_exact_canonical_nested_params(self):
        runner = Mock(
            return_value=self.completed(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"rows": [{"value": 1}]}),
                        }
                    ]
                }
            )
        )
        provider = self.provider(runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            state = ProviderState(Path(directory) / "state.sqlite3")
            with patch.object(api, "_STATE", state), patch.object(
                api, "_provider_for", return_value=provider
            ):
                result = api.query(
                    "wind_enrichment",
                    capability="fundamentals",
                    params={
                        "question": "600519.SH 2025 ROE",
                        "lang": "英文",
                    },
                )

                with self.assertRaisesRegex(ValueError, "unsupported wind_enrichment params"):
                    api.query(
                        "wind_enrichment",
                        capability="fundamentals",
                        params={"question": "600519.SH", "unexpected": True},
                    )
                with self.assertRaisesRegex(
                    ValueError, "top_k is only supported for announcements"
                ):
                    api.query(
                        "wind_enrichment",
                        capability="fundamentals",
                        params={"question": "600519.SH", "top_k": 2},
                    )

        self.assertEqual("success", result["_meta"]["status"])
        command_params = json.loads(runner.call_args.args[0][5])
        self.assertEqual("英文", command_params["lang"])
        self.assertEqual("600519.SH 2025 ROE", command_params["question"])
        self.assertNotIn("top_k", command_params)

    def test_announcements_use_query_top_k_and_never_send_lang(self):
        runner = Mock(
            return_value=self.completed(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"rows": [{"title": "年报"}]}),
                        }
                    ]
                }
            )
        )
        provider = self.provider(runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            state = ProviderState(Path(directory) / "state.sqlite3")
            with patch.object(api, "_STATE", state), patch.object(
                api, "_provider_for", return_value=provider
            ):
                result = api.query(
                    "wind_enrichment",
                    capability="announcements",
                    params={"question": "600519.SH 2025年报", "top_k": 3},
                )

        self.assertEqual("success", result["_meta"]["status"])
        command = runner.call_args.args[0]
        self.assertEqual(
            [
                "node",
                str(self.skill_dir.resolve() / "scripts" / "cli.mjs"),
                "call",
                "financial_docs",
                "get_company_announcements",
                '{"query":"600519.SH 2025年报","top_k":3}',
            ],
            command,
        )
        self.assertNotIn("lang", json.loads(command[5]))

    def test_canonical_wind_enrichment_enforces_single_target(self):
        invalid_params = (
            {"codes": ["600519", "000001"]},
            {"code": "600519", "codes": ["600519"]},
        )
        provider_loader = Mock(side_effect=AssertionError("provider must not run"))
        for target_params in invalid_params:
            with self.subTest(target_params=target_params), patch.object(
                api, "_provider_for", provider_loader
            ):
                with self.assertRaisesRegex(ValueError, "single target"):
                    api.query(
                        "wind_enrichment",
                        capability="fundamentals",
                        **target_params,
                    )
        self.assertEqual(0, provider_loader.call_count)

        runner = Mock(
            return_value=self.completed(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"rows": [{"value": 1}]}),
                        }
                    ]
                }
            )
        )
        provider = self.provider(runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            state = ProviderState(Path(directory) / "state.sqlite3")
            with patch.object(api, "_STATE", state), patch.object(
                api, "_provider_for", return_value=provider
            ):
                result = api.query(
                    "wind_enrichment",
                    capability="fundamentals",
                    codes=["600519"],
                )

        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual(
            {
                "question": "600519",
                "lang": "中文",
            },
            json.loads(runner.call_args.args[0][5]),
        )

    def test_forbidden_intents_never_call_wind(self):
        provider = self.provider(runner=Mock(side_effect=AssertionError("must not run")))
        for intent, params in (
            ("realtime_market", {}),
            ("stock_snapshot", {"codes": ["600519"]}),
            ("stock_kline", {"code": "600519"}),
            ("review_sentiment", {"query": "非ST"}),
            ("news", {"limit": 1}),
            ("sector_index", {"names": ["半导体"]}),
        ):
            with self.subTest(intent=intent):
                outcome = provider.call(intent, params)
                self.assertEqual("incompatible", outcome.status)

        screener = self.provider(
            name="wind_screener",
            runner=Mock(side_effect=AssertionError("must not run")),
        )
        for intent, params in (
            ("realtime_market", {}),
            ("stock_snapshot", {"codes": ["600519"]}),
            ("stock_kline", {"code": "600519"}),
            ("review_sentiment", {}),
            ("news", {"limit": 1}),
            ("sector_index", {"names": ["半导体"]}),
        ):
            with self.subTest(provider="wind_screener", intent=intent):
                outcome = screener.call(intent, params)
                self.assertEqual("incompatible", outcome.status)


class WindDiscoveryDoctorManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    @staticmethod
    def make_skill(path):
        scripts = path / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "cli.mjs").write_text("// test fixture\n", encoding="utf-8")
        return path

    def test_discovery_order_is_configured_then_global_then_project_compat(self):
        configured = self.make_skill(self.root / "configured")
        global_dir = self.make_skill(self.root / "global")
        project = self.make_skill(self.root / "project")

        runtime = discover_wind_runtime(
            skill_dir=configured,
            global_dir=global_dir,
            project_compat_dir=project,
        )
        self.assertEqual(configured.resolve(), runtime.path)
        self.assertEqual("global", runtime.scope)

        runtime = discover_wind_runtime(
            skill_dir=self.root / "missing",
            global_dir=global_dir,
            project_compat_dir=project,
        )
        self.assertEqual(global_dir.resolve(), runtime.path)
        self.assertEqual("global", runtime.scope)

        runtime = discover_wind_runtime(
            skill_dir=self.root / "missing",
            global_dir=self.root / "also-missing",
            project_compat_dir=project,
        )
        self.assertEqual(project.resolve(), runtime.path)
        self.assertEqual("project_compat", runtime.scope)

    def test_doctor_reports_runtime_scope_without_config_contents(self):
        skill = self.make_skill(self.root / "project")
        config = self.root / "config"
        config.write_text("SECRET_CONFIG_CONTENT", encoding="utf-8")
        provider = WindMcpProvider(
            "wind_mcp",
            skill_dir=skill,
            config_path=config,
            runtime_scope="project_compat",
        )
        report = collect_diagnostics(
            provider_names=("wind_mcp",),
            provider_loader=lambda _name: provider,
            wind_config_path=config,
        )

        item = report["providers"]["wind_mcp"]
        self.assertEqual("configured_unverified", item["status"])
        self.assertEqual("project_compat", item["runtime_scope"])
        self.assertNotIn("SECRET_CONFIG_CONTENT", json.dumps(report))

    def test_manifest_is_derived_from_actual_registry_and_routes(self):
        manifest = capability_manifest()
        wind = manifest["providers"]["wind_mcp"]
        actual_specs = all_route_specs()
        actual_routes = sorted(
            {
                spec.intent
                for spec in actual_specs
                if any(name in spec.providers for name in WIND_PROVIDER_NAMES)
            }
        )

        self.assertIn("wind_mcp", api.PROVIDER_REGISTRY)
        self.assertIn("wind_documents", api.PROVIDER_REGISTRY)
        self.assertIn("wind_screener", api.PROVIDER_REGISTRY)
        self.assertEqual(actual_routes, wind["routes"])
        self.assertEqual(
            ["filings", "review_sentiment"],
            wind["automatic_fallback_intents"],
        )
        self.assertEqual(["wind_enrichment"], wind["explicit_intents"])
        self.assertEqual(
            sorted([*WIND_ENRICHMENT_CAPABILITIES, "stock_screener"]),
            wind["capabilities"],
        )
        self.assertTrue(manifest["providers"]["tdx_mcp"]["registered"])
        self.assertNotEqual(
            "manual_cross_check_only",
            manifest["providers"]["tdx_mcp"].get("status"),
        )

    def test_routes_add_only_dedicated_wind_screener_to_explicit_review(self):
        self.assertEqual(("wind_mcp",), route_for("wind_enrichment", {}).providers)
        self.assertNotIn("wind_mcp", route_for("stock_event", {}).providers)
        self.assertIn("wind_documents", route_for("filings", {}).providers)
        self.assertEqual(
            "wind_screener",
            route_for("review_sentiment", {"query": "非ST"}).providers[-1],
        )
        self.assertFalse(
            any(
                "wind" in provider
                for provider in route_for("review_sentiment", {}).providers
            )
        )
        for intent, params in (
            ("realtime_market", {}),
            ("stock_snapshot", {}),
            ("stock_kline", {"period": "daily"}),
            ("news", {}),
            ("sector_index", {}),
        ):
            self.assertFalse(
                any("wind" in provider for provider in route_for(intent, params).providers)
            )


if __name__ == "__main__":
    unittest.main()
