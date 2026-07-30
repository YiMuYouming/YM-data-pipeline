import asyncio
import importlib.metadata
import inspect
import io
import json
import tempfile
import unittest
import warnings
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx2

try:
    from builtins import ExceptionGroup
except ImportError:  # Python 3.10; the SDK dependency supplies the backport.
    from exceptiongroup import ExceptionGroup

import ym_stock_data.api as api
from ym_stock_data.__main__ import _parser, main
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.tdx_auth import (
    FileCredentialStore,
    TdxAuthMissing,
    TdxOwnedAuth,
)
from ym_stock_data.providers.tdx_mcp import (
    SERVER_URL,
    TOOL_ALLOWLIST,
    TOOL_SCHEMA_CONTRACTS,
    TdxForbidden,
    TdxMcpClient,
    TdxMcpProvider,
    TdxProtocolError,
    TdxSchemaError,
    TdxUnauthorized,
)
from ym_stock_data.routing import route_for


def valid_tool_schemas():
    return {
        "tdx_screener": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        "tdx_quotes": {
            "type": "object",
            "properties": {
                "codes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["codes"],
        },
        "tdx_kline": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "period": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["code"],
        },
        "wenda_report_query": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "days": {"type": "integer"},
            },
            "required": ["code"],
        },
        "wenda_notice_query": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "days": {"type": "integer"},
            },
            "required": ["code"],
        },
        "wenda_news_query": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "required": [],
        },
    }


def tool(name, schema):
    return SimpleNamespace(
        name=name,
        input_schema=schema,
        annotations=SimpleNamespace(read_only_hint=True, destructive_hint=False),
    )


def http_status_error(status_code):
    request = httpx2.Request("POST", SERVER_URL)
    response = httpx2.Response(status_code, request=request)
    return httpx2.HTTPStatusError(
        "SECRET_RESPONSE_BODY",
        request=request,
        response=response,
    )


_DEFAULT_PAYLOAD = object()


class FakeSession:
    def __init__(self, *, schemas=None, payload=_DEFAULT_PAYLOAD, call_error=None):
        schemas = schemas or valid_tool_schemas()
        self.tools = [tool(name, schema) for name, schema in schemas.items()]
        self.payload = {"datas": []} if payload is _DEFAULT_PAYLOAD else payload
        self.call_error = call_error
        self.calls = []

    async def initialize(self):
        self.calls.append(("initialize", None))

    async def list_tools(self, *, params=None):
        self.calls.append(("tools/list", params))
        return SimpleNamespace(tools=self.tools, next_cursor=None)

    async def call_tool(self, name, arguments):
        self.calls.append(("tools/call", (name, arguments)))
        if self.call_error:
            raise self.call_error
        return SimpleNamespace(
            structured_content=self.payload,
            content=[],
            is_error=False,
        )


class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeAuth:
    def __init__(self, *, status="configured_unverified"):
        self.status = status
        self.calls = []

    def probe(self):
        return self.status

    def authorization(self, *, force_refresh=False, rejected_authorization=None):
        self.calls.append((force_refresh, rejected_authorization))
        if self.status == "auth_missing":
            raise TdxAuthMissing("sanitized")
        return "Bearer ROTATED" if force_refresh else "Bearer INITIAL"


class FakeProviderClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def call_tool(self, name, arguments, auth_manager):
        self.calls.append((name, arguments, auth_manager))
        auth_manager.authorization()
        if self.error:
            raise self.error
        return self.payload


class OfficialSdkClientTests(unittest.TestCase):
    def test_official_sdk_is_fixed_and_custom_jsonrpc_transport_is_gone(self):
        self.assertEqual("2.0.0", importlib.metadata.version("mcp"))
        import ym_stock_data.providers.tdx_mcp as module

        source = inspect.getsource(module)
        self.assertIn("streamable_http_client", source)
        self.assertIn("ClientSession", source)
        for forbidden in ("urllib.request", '"jsonrpc"', "_HttpSession"):
            self.assertNotIn(forbidden, source)

    def test_official_sdk_session_accepts_the_sdk_two_stream_transport(self):
        import ym_stock_data.providers.tdx_mcp as module

        read_stream = object()
        write_stream = object()
        http_client = object()
        sdk_session = object()

        @asynccontextmanager
        async def transport(url, *, http_client):
            self.assertEqual(SERVER_URL, url)
            self.assertIs(http_client, globals_http_client)
            yield read_stream, write_stream

        globals_http_client = http_client
        session_constructor = Mock(return_value=SessionContext(sdk_session))

        async def exercise():
            async with module._official_sdk_session("Bearer REDACTED") as session:
                self.assertIs(sdk_session, session)

        with (
            patch.object(
                module.httpx2,
                "AsyncClient",
                return_value=SessionContext(http_client),
            ),
            patch.object(module, "streamable_http_client", transport),
            patch.object(module, "ClientSession", session_constructor),
        ):
            asyncio.run(exercise())

        args, kwargs = session_constructor.call_args
        self.assertEqual((read_stream, write_stream), args)
        self.assertEqual("ym-stock-data", kwargs["client_info"].name)

    def test_allowlist_and_schema_contracts_are_exactly_six_read_only_tools(self):
        expected = {
            "tdx_screener",
            "tdx_quotes",
            "tdx_kline",
            "wenda_report_query",
            "wenda_notice_query",
            "wenda_news_query",
        }
        self.assertEqual(expected, set(TOOL_ALLOWLIST))
        self.assertEqual(expected, set(TOOL_SCHEMA_CONTRACTS))
        for forbidden in ("trade", "order", "write", "ticket", "cancel"):
            self.assertFalse(any(forbidden in item.lower() for item in expected))

    def test_initialize_list_schema_gate_then_call(self):
        session = FakeSession(payload={"datas": [{"code": "600519"}]})
        authorizations = []

        def factory(authorization):
            authorizations.append(authorization)
            return SessionContext(session)

        result = TdxMcpClient(session_factory=factory).call_tool(
            "tdx_screener", {"query": "非ST", "limit": 1}, FakeAuth()
        )

        self.assertEqual({"datas": [{"code": "600519"}]}, result)
        self.assertEqual(["Bearer INITIAL"], authorizations)
        self.assertEqual(
            ["initialize", "tools/list", "tools/call"],
            [name for name, _value in session.calls],
        )

    def test_extra_or_trading_tool_is_rejected_before_auth_and_transport(self):
        factory = Mock()
        auth = FakeAuth()
        client = TdxMcpClient(session_factory=factory)

        for name in ("place_order", "arbitrary_tool"):
            with self.assertRaises(ValueError):
                client.call_tool(name, {}, auth)

        self.assertEqual([], auth.calls)
        factory.assert_not_called()

    def test_tools_list_target_missing_or_schema_drift_fails_before_call(self):
        cases = []
        missing = valid_tool_schemas()
        missing.pop("tdx_screener")
        cases.append(missing)
        wrong_type = valid_tool_schemas()
        wrong_type["tdx_quotes"] = {
            **wrong_type["tdx_quotes"],
            "properties": {"codes": {"type": "string"}},
        }
        cases.append(wrong_type)
        extra_required = valid_tool_schemas()
        extra_required["tdx_screener"] = {
            **extra_required["tdx_screener"],
            "required": ["query", "trade_confirmation"],
        }
        cases.append(extra_required)

        for schemas in cases:
            with self.subTest(tool_count=len(schemas)):
                session = FakeSession(schemas=schemas)
                client = TdxMcpClient(
                    session_factory=lambda _authorization, value=session: SessionContext(value)
                )
                with self.assertRaises(TdxSchemaError):
                    client.call_tool("tdx_screener", {"query": "非ST"}, FakeAuth())
                self.assertNotIn("tools/call", [name for name, _ in session.calls])

    def test_unrelated_missing_or_drifted_tool_does_not_disable_target(self):
        cases = []
        missing = valid_tool_schemas()
        missing.pop("wenda_news_query")
        cases.append(missing)
        drifted = valid_tool_schemas()
        drifted["tdx_quotes"] = {
            **drifted["tdx_quotes"],
            "properties": {"codes": {"type": "string"}},
        }
        cases.append(drifted)

        for schemas in cases:
            with self.subTest(tool_count=len(schemas)):
                session = FakeSession(schemas=schemas, payload={"datas": []})
                result = TdxMcpClient(
                    session_factory=lambda _authorization, value=session: SessionContext(value)
                ).call_tool("tdx_screener", {"query": "非ST"}, FakeAuth())

                self.assertEqual({"datas": []}, result)
                self.assertIn("tools/call", [name for name, _ in session.calls])

    def test_sync_client_runs_inside_existing_event_loop_without_coroutine_warning(self):
        session = FakeSession(payload={"datas": []})
        client = TdxMcpClient(
            session_factory=lambda _authorization: SessionContext(session)
        )

        async def exercise():
            with warnings.catch_warnings():
                warnings.simplefilter("error", RuntimeWarning)
                return client.call_tool(
                    "tdx_screener", {"query": "非ST"}, FakeAuth()
                )

        self.assertEqual({"datas": []}, asyncio.run(exercise()))

    def test_explicit_destructive_annotation_fails_schema_gate(self):
        session = FakeSession()
        target = next(item for item in session.tools if item.name == "tdx_screener")
        target.annotations.destructive_hint = True
        client = TdxMcpClient(
            session_factory=lambda _authorization: SessionContext(session)
        )

        with self.assertRaises(TdxSchemaError):
            client.call_tool("tdx_screener", {"query": "非ST"}, FakeAuth())

        self.assertNotIn("tools/call", [name for name, _ in session.calls])

    def test_401_refreshes_once_rebuilds_session_and_retries_once(self):
        first = FakeSession(
            call_error=ExceptionGroup("sdk transport", [http_status_error(401)])
        )
        second = FakeSession(payload={"datas": []})
        sessions = iter((first, second))
        factory_calls = []

        def factory(authorization):
            factory_calls.append(authorization)
            return SessionContext(next(sessions))

        auth = FakeAuth()
        result = TdxMcpClient(session_factory=factory).call_tool(
            "tdx_screener", {"query": "无匹配"}, auth
        )

        self.assertEqual({"datas": []}, result)
        self.assertEqual(["Bearer INITIAL", "Bearer ROTATED"], factory_calls)
        self.assertEqual(
            [(False, None), (True, "Bearer INITIAL")],
            auth.calls,
        )
        self.assertEqual(1, len([x for x in second.calls if x[0] == "tools/call"]))

    def test_second_401_fails_without_third_session_or_refresh(self):
        sessions = iter(
            (
                FakeSession(call_error=TdxUnauthorized("first")),
                FakeSession(call_error=TdxUnauthorized("second secret body")),
            )
        )
        factory = Mock(side_effect=lambda _authorization: SessionContext(next(sessions)))
        auth = FakeAuth()

        with self.assertRaisesRegex(TdxUnauthorized, "authorization failed") as caught:
            TdxMcpClient(session_factory=factory).call_tool(
                "tdx_screener", {"query": "非ST"}, auth
            )

        self.assertEqual(2, factory.call_count)
        self.assertEqual(2, len(auth.calls))
        self.assertNotIn("secret body", str(caught.exception))

    def test_403_fails_closed_without_refresh_or_retry(self):
        session = FakeSession(
            call_error=ExceptionGroup("sdk transport", [http_status_error(403)])
        )
        factory = Mock(return_value=SessionContext(session))
        auth = FakeAuth()

        with self.assertRaisesRegex(TdxForbidden, "permission denied") as caught:
            TdxMcpClient(session_factory=factory).call_tool(
                "tdx_screener", {"query": "非ST"}, auth
            )

        factory.assert_called_once()
        self.assertEqual([(False, None)], auth.calls)
        self.assertNotIn("secret permission body", str(caught.exception))

    def test_malformed_or_embedded_error_payload_never_succeeds(self):
        payloads = (
            None,
            {"error": "bad"},
            {"isError": True},
            ["not-a-dict"],
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                session = FakeSession(payload=payload)
                client = TdxMcpClient(
                    session_factory=lambda _authorization, value=session: SessionContext(value)
                )
                with self.assertRaises(TdxProtocolError):
                    client.call_tool("tdx_screener", {"query": "非ST"}, FakeAuth())

    def test_sdk_runtime_error_is_a_provider_protocol_failure(self):
        session = FakeSession(
            call_error=RuntimeError("SECRET invalid structured content")
        )
        client = TdxMcpClient(
            session_factory=lambda _authorization: SessionContext(session)
        )
        outcome = TdxMcpProvider(
            "tdx_screener",
            auth_manager=FakeAuth(),
            client=client,
        ).call("review_sentiment", {"query": "非ST", "limit": 1})

        self.assertEqual("provider_error", outcome.status)
        self.assertEqual("MCP_ERROR", outcome.error_code)
        self.assertEqual({"required": True, "status": "present"}, outcome.auth)


class TdxMcpProviderTests(unittest.TestCase):
    def test_provider_capabilities_map_only_to_compatible_intents(self):
        cases = {
            "tdx_screener": ("review_sentiment", {"query": "非ST", "limit": 1}, {"datas": [{}]}),
            "tdx_quotes": ("stock_snapshot", {"codes": ["600519"]}, {"600519": {"price": 1}}),
            "tdx_kline": ("stock_kline", {"code": "600519", "period": "daily", "count": 1}, {"bars": [{}]}),
            "tdx_report": ("research", {"code": "600519"}, {"reports": [{}]}),
            "tdx_notice": ("filings", {"code": "600519"}, {"filings": [{}]}),
            "tdx_news": ("news", {"limit": 1}, {"items": [{}]}),
        }
        for name, (intent, params, payload) in cases.items():
            with self.subTest(name=name):
                client = FakeProviderClient(payload=payload)
                auth = FakeAuth()
                provider = TdxMcpProvider(name, auth_manager=auth, client=client)
                outcome = provider.call(intent, params)
                self.assertEqual("success", outcome.status)
                self.assertIs(auth, client.calls[0][2])
                self.assertEqual("incompatible", provider.call("realtime_market", {}).status)
                self.assertEqual(1, len(client.calls))

    def test_only_explicit_empty_expected_container_becomes_empty(self):
        cases = (
            ("tdx_screener", "review_sentiment", {"query": "none"}, {"datas": []}),
            ("tdx_quotes", "stock_snapshot", {"codes": ["600519"]}, {"items": []}),
            ("tdx_kline", "stock_kline", {"code": "600519"}, {"bars": []}),
            ("tdx_report", "research", {"code": "600519"}, {"reports": []}),
            ("tdx_notice", "filings", {"code": "600519"}, {"filings": []}),
            ("tdx_news", "news", {"limit": 1}, {"items": []}),
        )
        for name, intent, params, payload in cases:
            outcome = TdxMcpProvider(
                name,
                auth_manager=FakeAuth(),
                client=FakeProviderClient(payload=payload),
            ).call(intent, params)
            self.assertEqual("empty", outcome.status)

    def test_missing_auth_403_and_protocol_error_are_distinct_sanitized_states(self):
        cases = (
            (FakeAuth(status="auth_missing"), FakeProviderClient(payload={"datas": []}), "AUTH_MISSING", "missing"),
            (FakeAuth(), FakeProviderClient(error=TdxForbidden("secret")), "AUTH_FORBIDDEN", "forbidden"),
            (FakeAuth(), FakeProviderClient(error=TdxProtocolError("secret")), "MCP_ERROR", "present"),
        )
        for auth, client, code, auth_state in cases:
            with self.subTest(code=code):
                outcome = TdxMcpProvider(
                    "tdx_screener", auth_manager=auth, client=client
                ).call("review_sentiment", {"query": "非ST"})
                self.assertEqual(code, outcome.error_code)
                self.assertEqual(auth_state, outcome.auth["status"])
                self.assertNotIn("secret", outcome.detail or "")


class OwnedAuthCliTests(unittest.TestCase):
    def test_parser_exposes_only_login_and_status_with_explicit_store_choice(self):
        help_text = _parser().format_help()
        auth_parser = next(
            action for action in _parser()._actions if action.dest == "command"
        ).choices["auth"]
        auth_help = auth_parser.format_help()

        self.assertIn("login-tdx", auth_help)
        self.assertIn("status-tdx", auth_help)
        self.assertNotIn("import-tdx", auth_help)
        self.assertNotIn("workbuddy", (help_text + auth_help).lower())

    def test_login_cli_calls_owned_flow_and_prints_only_sanitized_state(self):
        output = io.StringIO()
        auth = Mock()
        auth.login.return_value = "configured_unverified"
        with patch(
            "ym_stock_data.__main__.create_tdx_auth", return_value=auth
        ) as factory, redirect_stdout(output):
            exit_code = main(["auth", "login-tdx", "--store", "file"])

        self.assertEqual(0, exit_code)
        factory.assert_called_once_with(mode="file", file_path=None)
        auth.login.assert_called_once_with()
        self.assertEqual(
            {
                "scope": "mcp.read",
                "status": "configured_unverified",
                "store": "file",
            },
            json.loads(output.getvalue()),
        )

    def test_login_switches_persisted_selector_only_after_success(self):
        custom_path = Path("/private/custom/tdx-owned.json")
        output = io.StringIO()
        auth = Mock()
        auth.store = FileCredentialStore(custom_path)
        auth.login.return_value = "configured_unverified"
        with (
            patch("ym_stock_data.__main__.create_tdx_auth", return_value=auth),
            patch(
                "ym_stock_data.__main__.persist_credential_store_selection",
                create=True,
            ) as persist,
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "auth",
                    "login-tdx",
                    "--store",
                    "file",
                    "--file-path",
                    str(custom_path),
                ]
            )

        self.assertEqual(0, exit_code)
        persist.assert_called_once_with("file", file_path=custom_path)
        self.assertNotIn(str(custom_path), output.getvalue())

        failed = Mock()
        failed.login.side_effect = TdxAuthMissing("SECRET failed login")
        with (
            patch("ym_stock_data.__main__.create_tdx_auth", return_value=failed),
            patch(
                "ym_stock_data.__main__.persist_credential_store_selection",
                create=True,
            ) as persist,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                2,
                main(["auth", "login-tdx", "--store", "keychain"]),
            )
        persist.assert_not_called()

    def test_status_uses_persisted_selector_when_store_is_omitted(self):
        output = io.StringIO()
        auth = Mock()
        auth.store = FileCredentialStore("/private/custom/tdx-owned.json")
        auth.probe.return_value = "auth_expired"
        with patch(
            "ym_stock_data.__main__.create_tdx_auth", return_value=auth
        ) as factory, redirect_stdout(output):
            exit_code = main(["auth", "status-tdx"])

        self.assertEqual(2, exit_code)
        factory.assert_called_once_with(mode=None, file_path=None)
        self.assertEqual("file", json.loads(output.getvalue())["store"])
        self.assertNotIn("/private/custom", output.getvalue())

    def test_status_cli_is_offline_sanitized_and_never_runs_login(self):
        output = io.StringIO()
        auth = Mock()
        auth.probe.return_value = "auth_expired"
        with patch(
            "ym_stock_data.__main__.create_tdx_auth", return_value=auth
        ), redirect_stdout(output):
            exit_code = main(["auth", "status-tdx"])

        self.assertEqual(2, exit_code)
        auth.probe.assert_called_once_with()
        auth.login.assert_not_called()
        self.assertEqual(
            {"scope": "mcp.read", "status": "auth_expired", "store": "keychain"},
            json.loads(output.getvalue()),
        )

    def test_removed_import_flag_fails_at_parser_without_scanning(self):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), self.assertRaises(SystemExit):
            main(["auth", "import-tdx", "--from-workbuddy"])

        self.assertEqual("", output.getvalue())
        import ym_stock_data.__main__ as cli_module
        import ym_stock_data.providers.tdx_mcp as provider_module

        source = inspect.getsource(cli_module) + inspect.getsource(provider_module)
        self.assertNotIn("workbuddy", source.lower())
        self.assertNotIn("import_tdx_credentials", source)


class TdxRegistryTests(unittest.TestCase):
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
                auth_manager=TdxOwnedAuth(
                    store=FileCredentialStore(root / "missing.json")
                ),
                client=FakeProviderClient(payload={"items": []}),
            )
            providers = {
                name: FailedProvider(name) for name in ("pytdx", "tencent", "sina")
            }
            providers["tdx_quotes"] = tdx
            with patch.object(api, "_STATE", state), patch.object(
                api, "_provider_for", side_effect=lambda name: providers[name]
            ):
                result = api.query("stock_snapshot", codes=["600519"])

        self.assertEqual("error", result["_meta"]["status"])
        self.assertEqual("AUTH_MISSING", result["_meta"]["attempts"][-1]["error_code"])

    def test_routes_keep_tdx_after_compatible_sources_only(self):
        self.assertEqual("tdx_quotes", route_for("stock_snapshot", {}).providers[-1])
        self.assertEqual("tdx_kline", route_for("stock_kline", {}).providers[-1])
        self.assertEqual("tdx_report", route_for("research", {}).providers[-1])
        self.assertEqual("tdx_news", route_for("news", {}).providers[-1])
        for intent, params in (
            ("realtime_market", {}),
            ("review_sentiment", {}),
            ("sector_index", {"names": ["半导体"]}),
        ):
            self.assertFalse(
                any(name.startswith("tdx_") for name in route_for(intent, params).providers)
            )


if __name__ == "__main__":
    unittest.main()
