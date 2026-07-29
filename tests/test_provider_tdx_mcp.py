import io
import json
import os
import stat
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import ym_stock_data.api as api
from ym_stock_data.__main__ import main
from ym_stock_data.doctor import collect_diagnostics
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.tdx_mcp import (
    SERVER_URL,
    TOOL_ALLOWLIST,
    CredentialImportError,
    TdxAuthExpired,
    TdxCredentialStore,
    TdxMcpClient,
    TdxMcpProvider,
    TdxProtocolError,
    import_workbuddy_credentials,
)
from ym_stock_data.routing import route_for


class StubStore:
    def __init__(self, *, status="configured_unverified", authorization="TestScheme REDACTED"):
        self.status = status
        self.authorization_value = authorization
        self.refresh_calls = 0

    def probe(self):
        return self.status

    def authorization(self, *, refresher):
        self.refresh_calls += 1
        if self.status == "auth_expired":
            raise TdxAuthExpired("sanitized")
        return self.authorization_value


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def call_tool(self, tool_name, arguments, authorization):
        self.calls.append((tool_name, arguments, authorization))
        return self.payload


def owned_payload(*, expires_at_ms=None):
    return {
        "schema_version": "1",
        "client_id": "TEST_ONLY_CLIENT",
        "access_token": "TEST_ONLY_ACCESS",
        "refresh_token": "TEST_ONLY_REFRESH",
        "token_type": "TestScheme",
        "expires_at_ms": expires_at_ms
        if expires_at_ms is not None
        else int(time.time() * 1000) + 3_600_000,
    }


def workbuddy_payload(*, duplicate_tdx=False):
    oauth = {
        "tdx-one": {
            "serverUrl": SERVER_URL,
            "serverName": "tdx-connector",
            "accessToken": "TEST_ONLY_ACCESS",
            "refreshToken": "TEST_ONLY_REFRESH",
            "tokenType": "TestScheme",
            "expiresAt": int(time.time() * 1000) + 3_600_000,
            "ignoredField": "must-not-copy",
        },
        "other": {
            "serverUrl": "https://example.invalid/mcp",
            "accessToken": "OTHER_TEST_ONLY",
        },
    }
    clients = {
        "tdx-one": {"client_id": "TEST_ONLY_CLIENT", "ignored": "drop"},
        "other": {"client_id": "OTHER_CLIENT"},
    }
    if duplicate_tdx:
        oauth["tdx-two"] = dict(oauth["tdx-one"])
        clients["tdx-two"] = {"client_id": "SECOND_CLIENT"}
    return {"mcpOAuth": oauth, "mcpClientInfo": clients, "unrelated": "drop"}


class TdxCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.target = self.root / "owned" / "tdx.json"

    def test_missing_owned_credentials_probe_auth_missing_without_discovery(self):
        store = TdxCredentialStore(self.target)

        self.assertEqual("auth_missing", store.probe())
        self.assertFalse(self.target.exists())

    def test_explicit_import_reads_one_candidate_and_writes_minimal_private_file(self):
        source_root = self.root / "connectors"
        candidate = source_root / "only" / ".credentials.json"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(json.dumps(workbuddy_payload()), encoding="utf-8")
        output = []

        result = import_workbuddy_credentials(
            source_root=source_root,
            target=self.target,
            emit=output.append,
        )

        self.assertEqual(str(self.target), output[0])
        self.assertEqual("ready", result["status"])
        self.assertEqual(0o700, stat.S_IMODE(self.target.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.target.stat().st_mode))
        stored = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "schema_version",
                "client_id",
                "access_token",
                "refresh_token",
                "token_type",
                "expires_at_ms",
            },
            set(stored),
        )
        rendered = json.dumps(result) + " ".join(output)
        for forbidden in ("TEST_ONLY_ACCESS", "TEST_ONLY_REFRESH", "TEST_ONLY_CLIENT"):
            self.assertNotIn(forbidden, rendered)

    def test_multiple_candidate_files_fail_closed_without_reading_or_writing(self):
        source_root = self.root / "connectors"
        for name in ("one", "two"):
            path = source_root / name / ".credentials.json"
            path.parent.mkdir(parents=True)
            path.write_text("not-json", encoding="utf-8")

        with self.assertRaises(CredentialImportError):
            import_workbuddy_credentials(
                source_root=source_root,
                target=self.target,
                emit=lambda _value: None,
            )

        self.assertFalse(self.target.exists())

    def test_multiple_tdx_entries_in_one_candidate_fail_closed(self):
        source_root = self.root / "connectors"
        candidate = source_root / "only" / ".credentials.json"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(
            json.dumps(workbuddy_payload(duplicate_tdx=True)),
            encoding="utf-8",
        )

        with self.assertRaises(CredentialImportError):
            import_workbuddy_credentials(
                source_root=source_root,
                target=self.target,
                emit=lambda _value: None,
            )

        self.assertFalse(self.target.exists())

    def test_expiring_token_refreshes_once_and_persists_private_file(self):
        store = TdxCredentialStore(self.target)
        store.save(owned_payload(expires_at_ms=1))
        refresher = Mock(
            return_value={
                "access_token": "TEST_ONLY_NEW_ACCESS",
                "refresh_token": "TEST_ONLY_NEW_REFRESH",
                "token_type": "TestScheme",
                "expires_in": 3600,
            }
        )

        authorization = store.authorization(refresher=refresher)

        self.assertEqual("TestScheme TEST_ONLY_NEW_ACCESS", authorization)
        refresher.assert_called_once()
        self.assertEqual(0o600, stat.S_IMODE(self.target.stat().st_mode))

    def test_missing_or_failed_refresh_is_auth_expired_and_called_at_most_once(self):
        for payload, refresher in (
            (
                {**owned_payload(expires_at_ms=1), "refresh_token": None},
                Mock(),
            ),
            (owned_payload(expires_at_ms=1), Mock(side_effect=OSError("secret body"))),
        ):
            with self.subTest(has_refresh=bool(payload.get("refresh_token"))):
                store = TdxCredentialStore(self.target)
                store.save(payload)
                with self.assertRaises(TdxAuthExpired):
                    store.authorization(refresher=refresher)
                self.assertLessEqual(refresher.call_count, 1)


class TdxMcpProviderTests(unittest.TestCase):
    def test_allowlist_contains_only_six_read_only_tools(self):
        self.assertEqual(
            {
                "tdx_screener",
                "tdx_quotes",
                "tdx_kline",
                "wenda_report_query",
                "wenda_notice_query",
                "wenda_news_query",
            },
            set(TOOL_ALLOWLIST),
        )
        for forbidden in ("trade", "order", "write", "ticket", "cancel"):
            self.assertFalse(any(forbidden in name.lower() for name in TOOL_ALLOWLIST))

    def test_provider_capabilities_map_only_to_compatible_intents(self):
        cases = {
            "tdx_screener": ("review_sentiment", {"query": "非ST", "limit": 1}, {"datas": [{}]}),
            "tdx_quotes": ("stock_snapshot", {"codes": ["600519"]}, {"600519": {"price": 1}}),
            "tdx_kline": ("stock_kline", {"code": "600519", "period": "daily", "count": 1}, {"bars": [{}]}),
            "tdx_report": ("research", {"code": "600519"}, {"reports": [{}]}),
            "tdx_notice": ("filings", {"code": "600519"}, {"filings": [{}]}),
            "tdx_news": ("news", {"limit": 1}, {"items": [{}]}),
        }
        expected_tools = {
            "tdx_screener": "tdx_screener",
            "tdx_quotes": "tdx_quotes",
            "tdx_kline": "tdx_kline",
            "tdx_report": "wenda_report_query",
            "tdx_notice": "wenda_notice_query",
            "tdx_news": "wenda_news_query",
        }
        for provider_name, (intent, params, payload) in cases.items():
            with self.subTest(provider=provider_name):
                client = FakeClient(payload)
                provider = TdxMcpProvider(
                    provider_name,
                    credential_store=StubStore(),
                    client=client,
                )
                result = provider.call(intent, params)
                self.assertEqual("success", result.status)
                self.assertEqual(provider_name, result.provider)
                self.assertEqual(expected_tools[provider_name], client.calls[0][0])
                incompatible = provider.call("realtime_market", {})
                self.assertEqual("incompatible", incompatible.status)
                self.assertEqual(1, len(client.calls))

    def test_arbitrary_or_trading_tool_is_rejected_before_transport(self):
        sender = Mock()
        client = TdxMcpClient(sender=sender)

        for tool_name in ("place_order", "arbitrary_tool"):
            with self.assertRaises(ValueError):
                client.call_tool(tool_name, {}, "TestScheme REDACTED")

        sender.assert_not_called()

    def test_jsonrpc_error_malformed_payload_and_embedded_error_never_succeed(self):
        bad_payloads = (
            {"error": {"code": -32000, "message": "secret body"}},
            {"result": {"content": [{"type": "text", "text": "not-json"}]}},
            {"result": {"isError": True, "content": []}},
            {"result": {"content": [{"type": "text", "text": json.dumps({"error": "bad"})}]}},
        )
        for response in bad_payloads:
            with self.subTest(response_keys=tuple(response)):
                client = TdxMcpClient(
                    sender=Mock(return_value=response),
                    skip_initialize=True,
                )
                provider = TdxMcpProvider(
                    "tdx_screener",
                    credential_store=StubStore(),
                    client=client,
                )
                outcome = provider.call(
                    "review_sentiment", {"query": "非ST", "limit": 1}
                )
                self.assertEqual("provider_error", outcome.status)
                self.assertIsNone(outcome.data)
                self.assertNotIn("secret body", outcome.detail or "")

    def test_missing_wrong_or_explicit_failure_containers_are_provider_errors(self):
        cases = (
            ("tdx_report", "research", {"code": "600519"}, {}),
            ("tdx_report", "research", {"code": "600519"}, {"reports": {}}),
            ("tdx_notice", "filings", {"code": "600519"}, {"success": False, "filings": []}),
            ("tdx_news", "news", {"limit": 1}, {"status": "failed", "items": []}),
            ("tdx_kline", "stock_kline", {"code": "600519"}, {"isError": True, "bars": []}),
        )
        for provider_name, intent, params, payload in cases:
            with self.subTest(provider=provider_name, payload=payload):
                provider = TdxMcpProvider(
                    provider_name,
                    credential_store=StubStore(),
                    client=FakeClient(payload),
                )
                outcome = provider.call(intent, params)
                self.assertEqual("provider_error", outcome.status)
                self.assertEqual("MCP_ERROR", outcome.error_code)
                self.assertIsNone(outcome.data)

    def test_only_explicit_empty_expected_container_becomes_empty(self):
        cases = (
            ("tdx_screener", "review_sentiment", {"query": "无匹配"}, {"datas": []}),
            ("tdx_quotes", "stock_snapshot", {"codes": ["600519"]}, {"items": []}),
            ("tdx_kline", "stock_kline", {"code": "600519"}, {"bars": []}),
            ("tdx_report", "research", {"code": "600519"}, {"reports": []}),
            ("tdx_notice", "filings", {"code": "600519"}, {"filings": []}),
            ("tdx_news", "news", {"limit": 1}, {"items": []}),
        )
        for provider_name, intent, params, payload in cases:
            with self.subTest(provider=provider_name):
                outcome = TdxMcpProvider(
                    provider_name,
                    credential_store=StubStore(),
                    client=FakeClient(payload),
                ).call(intent, params)
                self.assertEqual("empty", outcome.status)

    def test_sender_mode_runs_initialize_notification_then_allowlisted_tool_call(self):
        messages = []

        def sender(message, _authorization):
            messages.append(message)
            if message.get("method") == "initialize":
                return {"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-03-26"}}
            if message.get("method") == "notifications/initialized":
                return {}
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"structuredContent": {"datas": []}},
            }

        client = TdxMcpClient(sender=sender)
        result = client.call_tool("tdx_screener", {"query": "无匹配"}, "TestScheme REDACTED")

        self.assertEqual({"datas": []}, result)
        self.assertEqual(
            ["initialize", "notifications/initialized", "tools/call"],
            [message["method"] for message in messages],
        )
        self.assertEqual("tdx_screener", messages[-1]["params"]["name"])

    def test_refresh_failure_returns_auth_error_with_expired_auth_state(self):
        provider = TdxMcpProvider(
            "tdx_screener",
            credential_store=StubStore(status="auth_expired"),
            client=FakeClient({"datas": [{}]}),
        )

        outcome = provider.call(
            "review_sentiment", {"query": "非ST", "limit": 1}
        )

        self.assertEqual("auth_error", outcome.status)
        self.assertEqual("AUTH_EXPIRED", outcome.error_code)
        self.assertEqual("expired", outcome.auth["status"])

    def test_http_oauth_error_never_becomes_success_or_leaks_response_body(self):
        provider = TdxMcpProvider(
            "tdx_screener",
            credential_store=StubStore(),
            client=TdxMcpClient(),
        )
        error = urllib.error.HTTPError(
            SERVER_URL,
            401,
            "secret response body",
            hdrs=None,
            fp=io.BytesIO(b"redacted"),
        )
        self.addCleanup(error.close)

        with patch("urllib.request.urlopen", side_effect=error):
            outcome = provider.call(
                "review_sentiment", {"query": "非ST", "limit": 1}
            )

        self.assertEqual("auth_error", outcome.status)
        self.assertEqual("AUTH_EXPIRED", outcome.error_code)
        self.assertIsNone(outcome.data)
        self.assertNotIn("secret", outcome.detail or "")


class TdxRegistryRoutingDoctorTests(unittest.TestCase):
    def test_canonical_query_audits_missing_owned_auth_after_compatible_failures(self):
        class FailedProvider:
            def __init__(self, name):
                self.name = name

            def call(self, _intent, _params):
                return ProviderOutcome(
                    provider=self.name,
                    status="dependency_missing",
                    error_code="TEST_UNAVAILABLE",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = ProviderState(root / "state.sqlite3")
            tdx = TdxMcpProvider(
                "tdx_quotes",
                credential_store=TdxCredentialStore(root / "missing.json"),
                client=FakeClient({"items": []}),
            )
            providers = {
                name: FailedProvider(name) for name in ("pytdx", "tencent", "sina")
            }
            providers["tdx_quotes"] = tdx
            with patch.object(api, "_STATE", state), patch.object(
                api,
                "_provider_for",
                side_effect=lambda name: providers[name],
            ):
                result = api.query("stock_snapshot", codes=["600519"])

        self.assertEqual("error", result["_meta"]["status"])
        self.assertEqual(
            ["pytdx", "tencent", "sina", "tdx_quotes"],
            result["_meta"]["source_chain"],
        )
        self.assertEqual("auth_error", result["_meta"]["attempts"][-1]["status"])
        self.assertEqual("AUTH_MISSING", result["_meta"]["attempts"][-1]["error_code"])

    def test_canonical_query_zero_auth_success_never_calls_tdx(self):
        class SuccessProvider:
            name = "pytdx"

            def call(self, _intent, _params):
                return ProviderOutcome(
                    provider="pytdx",
                    status="success",
                    data={"600519": {"price": 1}},
                    latency_ms=1,
                )

        with tempfile.TemporaryDirectory() as directory:
            state = ProviderState(Path(directory) / "state.sqlite3")
            tdx_client = FakeClient({"items": [{"code": "600519"}]})
            tdx = TdxMcpProvider(
                "tdx_quotes",
                credential_store=StubStore(),
                client=tdx_client,
            )
            with patch.object(api, "_STATE", state), patch.object(
                api,
                "_provider_for",
                side_effect=lambda name: SuccessProvider() if name == "pytdx" else tdx,
            ):
                result = api.query("stock_snapshot", codes=["600519"])

        self.assertEqual("success", result["_meta"]["status"])
        self.assertEqual(["pytdx"], result["_meta"]["source_chain"])
        self.assertEqual([], tdx_client.calls)

    def test_registry_and_routes_include_tdx_only_after_compatible_sources(self):
        for name in (
            "tdx_mcp",
            "tdx_screener",
            "tdx_quotes",
            "tdx_kline",
            "tdx_report",
            "tdx_notice",
            "tdx_news",
        ):
            self.assertIn(name, api.PROVIDER_REGISTRY)
        self.assertEqual("tdx_screener", route_for("review_sentiment", {"query": "非ST"}).providers[-1])
        self.assertEqual("tdx_quotes", route_for("stock_snapshot", {}).providers[-1])
        self.assertEqual("tdx_kline", route_for("stock_kline", {"period": "daily"}).providers[-1])
        self.assertEqual("tdx_report", route_for("research", {}).providers[-1])
        self.assertEqual(
            ("cninfo", "tdx_notice", "wind_documents"),
            route_for("filings", {}).providers,
        )
        self.assertEqual("tdx_news", route_for("news", {}).providers[-1])
        for intent, params in (
            ("realtime_market", {}),
            ("review_sentiment", {}),
            ("sector_index", {"names": ["半导体"]}),
        ):
            self.assertFalse(
                any(name.startswith("tdx_") for name in route_for(intent, params).providers)
            )

    def test_doctor_reports_total_and_all_six_capabilities(self):
        report = collect_diagnostics(
            provider_names=(
                "tdx_mcp",
                "tdx_screener",
                "tdx_quotes",
                "tdx_kline",
                "tdx_report",
                "tdx_notice",
                "tdx_news",
            ),
            tdx_auth_path=Path("/unused-by-provider-probes"),
        )

        expected = {
            "tdx_mcp",
            "tdx_screener",
            "tdx_quotes",
            "tdx_kline",
            "tdx_report",
            "tdx_notice",
            "tdx_news",
        }
        self.assertTrue(expected.issubset(report["providers"]))
        self.assertTrue(
            all(report["providers"][name]["status"] == "auth_missing" for name in expected)
        )

    def test_cli_import_failure_is_sanitized_and_never_scans_implicitly(self):
        output = io.StringIO()
        with patch(
            "ym_stock_data.__main__.import_tdx_credentials",
            side_effect=CredentialImportError("secret token body"),
        ) as importer, redirect_stdout(output):
            exit_code = main(["auth", "import-tdx", "--from-workbuddy"])

        importer.assert_called_once_with(from_workbuddy=True)
        self.assertEqual(2, exit_code)
        payload = json.loads(output.getvalue())
        self.assertEqual("unavailable", payload["status"])
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
