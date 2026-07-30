"""Governed read-only TDX provider using the official MCP Python SDK."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import asynccontextmanager
from typing import Callable

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .base import ProviderOutcome
from .tdx_auth import (
    DEFAULT_FILE_PATH,
    DEFAULT_RESOURCE_URL,
    FileCredentialStore,
    TdxAuthExpired,
    TdxAuthMissing,
    TdxOwnedAuth,
    TdxScopeError,
    default_credential_store,
)


SERVER_URL = DEFAULT_RESOURCE_URL
REQUEST_TIMEOUT_SEC = 90
TDX_AUTH_PATH = DEFAULT_FILE_PATH
TdxCredentialStore = FileCredentialStore

TOOL_SCHEMA_CONTRACTS = {
    "tdx_screener": {
        "required": frozenset({"query"}),
        "properties": {"query": ("string", None), "limit": ("integer", None)},
    },
    "tdx_quotes": {
        "required": frozenset({"codes"}),
        "properties": {"codes": ("array", "string")},
    },
    "tdx_kline": {
        "required": frozenset({"code"}),
        "properties": {
            "code": ("string", None),
            "period": ("string", None),
            "count": ("integer", None),
        },
    },
    "wenda_report_query": {
        "required": frozenset({"code"}),
        "properties": {"code": ("string", None), "days": ("integer", None)},
    },
    "wenda_notice_query": {
        "required": frozenset({"code"}),
        "properties": {"code": ("string", None), "days": ("integer", None)},
    },
    "wenda_news_query": {
        "required": frozenset(),
        "properties": {"limit": ("integer", None)},
    },
}
TOOL_ALLOWLIST = frozenset(TOOL_SCHEMA_CONTRACTS)
TDX_PROVIDER_SPECS = {
    "tdx_screener": ("review_sentiment", "tdx_screener"),
    "tdx_quotes": ("stock_snapshot", "tdx_quotes"),
    "tdx_kline": ("stock_kline", "tdx_kline"),
    "tdx_report": ("research", "wenda_report_query"),
    "tdx_notice": ("filings", "wenda_notice_query"),
    "tdx_news": ("news", "wenda_news_query"),
}
TDX_PROVIDER_NAMES = tuple(TDX_PROVIDER_SPECS)
TDX_DIAGNOSTIC_NAMES = ("tdx_mcp",) + TDX_PROVIDER_NAMES


class TdxProtocolError(RuntimeError):
    """The MCP response violated the governed provider contract."""


class TdxSchemaError(TdxProtocolError):
    """tools/list was missing a capability or its schema drifted."""


class TdxTransportError(RuntimeError):
    """The SDK transport failed without exposing response content."""


class TdxUnauthorized(RuntimeError):
    """The current access token was rejected with HTTP 401."""


class TdxForbidden(RuntimeError):
    """The read-only token lacks permission; scope escalation is forbidden."""


def _arguments(provider_name: str, params: dict) -> dict:
    if provider_name == "tdx_screener":
        return {"query": params["query"], "limit": params.get("limit", 50)}
    if provider_name == "tdx_quotes":
        return {"codes": list(params["codes"])}
    if provider_name == "tdx_kline":
        return {
            "code": params["code"],
            "period": params.get("period", "daily"),
            "count": params.get("count", 30),
        }
    if provider_name in {"tdx_report", "tdx_notice"}:
        result = {"code": params["code"]}
        if params.get("days") is not None:
            result["days"] = params["days"]
        return result
    if provider_name == "tdx_news":
        return {"limit": params.get("limit", 20)}
    raise ValueError("unsupported TDX provider")


def _payload_failed(payload: dict) -> bool:
    if payload.get("error") not in (None, False, ""):
        return True
    if payload.get("isError") is True or payload.get("success") is False:
        return True
    status = str(payload.get("status") or "").strip().lower()
    return status in {"error", "failed", "failure", "auth_error", "unavailable"}


def _required_rows(payload: dict, container: str) -> list:
    if container not in payload or not isinstance(payload[container], list):
        raise TdxProtocolError("TDX payload is missing its expected container")
    return payload[container]


def _normalize(provider_name: str, payload: dict) -> tuple[dict, int]:
    if _payload_failed(payload):
        raise TdxProtocolError("TDX payload is an explicit failure")
    if provider_name == "tdx_screener":
        rows = _required_rows(payload, "datas")
        return {"datas": rows, "row_count": len(rows)}, len(rows)
    if provider_name == "tdx_quotes":
        if "items" in payload:
            rows = _required_rows(payload, "items")
            data = {
                str(row.get("code") or row.get("股票代码")): row
                for row in rows
                if isinstance(row, dict) and (row.get("code") or row.get("股票代码"))
            }
        else:
            data = {
                str(key): value
                for key, value in payload.items()
                if isinstance(value, dict)
            }
            if not data:
                raise TdxProtocolError("TDX payload is missing its expected container")
        return data, len(data)
    container = {
        "tdx_kline": "bars",
        "tdx_report": "reports",
        "tdx_notice": "filings",
        "tdx_news": "items",
    }[provider_name]
    rows = _required_rows(payload, container)
    return {container: rows}, len(rows)


def _walk_exceptions(error: BaseException):
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        yield current
        related = []
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, (list, tuple)):
            related.extend(item for item in nested if isinstance(item, BaseException))
        for attribute in ("__cause__", "__context__"):
            item = getattr(current, attribute, None)
            if isinstance(item, BaseException) and item is not current:
                related.append(item)
        pending.extend(reversed(related))


def _contains_exception(error: BaseException, kinds: tuple[type, ...]) -> bool:
    return any(isinstance(item, kinds) for item in _walk_exceptions(error))


def _status_code(error: BaseException) -> int | None:
    for item in _walk_exceptions(error):
        response = getattr(item, "response", None)
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def _raise_sanitized_transport(error: BaseException) -> None:
    if isinstance(
        error,
        (
            TdxUnauthorized,
            TdxForbidden,
            TdxSchemaError,
            TdxProtocolError,
            TdxTransportError,
        ),
    ):
        raise error
    status = _status_code(error)
    if status == 401:
        raise TdxUnauthorized("TDX MCP authorization failed") from None
    if status == 403:
        raise TdxForbidden("TDX MCP permission denied") from None
    timeout_errors = (TimeoutError, socket.timeout, asyncio.TimeoutError)
    if _contains_exception(error, timeout_errors):
        raise TimeoutError("TDX MCP request timed out") from None
    if _contains_exception(error, (ValueError, TypeError, RuntimeError)):
        raise TdxProtocolError("TDX MCP protocol response invalid") from None
    raise TdxTransportError("TDX MCP transport failed") from None


@asynccontextmanager
async def _official_sdk_session(authorization: str):
    """Create one official SDK Streamable HTTP session for one attempt."""

    async with httpx2.AsyncClient(
        headers={"Authorization": authorization},
        timeout=REQUEST_TIMEOUT_SEC,
    ) as http_client:
        async with streamable_http_client(
            SERVER_URL,
            http_client=http_client,
        ) as streams:
            read_stream, write_stream = streams
            async with ClientSession(
                read_stream,
                write_stream,
                client_info=types.Implementation(
                    name="ym-stock-data",
                    version="2.0.0",
                ),
            ) as session:
                yield session


def _tool_value(tool, name: str, default=None):
    if isinstance(tool, dict):
        return tool.get(name, default)
    return getattr(tool, name, default)


def _annotation_value(annotations, snake: str, camel: str):
    if annotations is None:
        return None
    if isinstance(annotations, dict):
        return annotations.get(snake, annotations.get(camel))
    return getattr(annotations, snake, None)


def _validate_tool_schema(tool) -> None:
    name = _tool_value(tool, "name")
    contract = TOOL_SCHEMA_CONTRACTS.get(name)
    if contract is None:
        return
    schema = _tool_value(tool, "input_schema")
    if schema is None:
        schema = _tool_value(tool, "inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise TdxSchemaError("TDX MCP tool schema drifted")
    required = schema.get("required", [])
    if not isinstance(required, list) or set(required) != set(contract["required"]):
        raise TdxSchemaError("TDX MCP tool schema drifted")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise TdxSchemaError("TDX MCP tool schema drifted")
    for property_name, (expected_type, expected_item_type) in contract[
        "properties"
    ].items():
        definition = properties.get(property_name)
        if not isinstance(definition, dict) or definition.get("type") != expected_type:
            raise TdxSchemaError("TDX MCP tool schema drifted")
        if expected_item_type is not None:
            items = definition.get("items")
            if not isinstance(items, dict) or items.get("type") != expected_item_type:
                raise TdxSchemaError("TDX MCP tool schema drifted")
    annotations = _tool_value(tool, "annotations")
    if _annotation_value(annotations, "destructive_hint", "destructiveHint") is True:
        raise TdxSchemaError("TDX MCP tool is marked destructive")
    if _annotation_value(annotations, "read_only_hint", "readOnlyHint") is False:
        raise TdxSchemaError("TDX MCP tool is not marked read-only")


def _validate_arguments(tool_name: str, arguments: dict) -> None:
    if not isinstance(arguments, dict):
        raise ValueError("TDX tool arguments must be an object")
    contract = TOOL_SCHEMA_CONTRACTS[tool_name]
    if not set(contract["required"]).issubset(arguments):
        raise ValueError("TDX tool arguments are incomplete")
    if not set(arguments).issubset(contract["properties"]):
        raise ValueError("TDX tool argument is not allowlisted")
    for name, value in arguments.items():
        expected_type, item_type = contract["properties"][name]
        valid = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "array": isinstance(value, list),
        }[expected_type]
        if not valid:
            raise ValueError("TDX tool argument type is invalid")
        if item_type == "string" and not all(isinstance(item, str) for item in value):
            raise ValueError("TDX tool argument type is invalid")


def _extract_payload(result) -> dict:
    if _tool_value(result, "is_error", _tool_value(result, "isError", False)) is True:
        raise TdxProtocolError("TDX MCP tool returned an error")
    payload = _tool_value(result, "structured_content")
    if payload is None:
        payload = _tool_value(result, "structuredContent")
    if payload is None:
        content = _tool_value(result, "content")
        if not isinstance(content, list):
            raise TdxProtocolError("TDX MCP payload is malformed")
        texts = [
            _tool_value(item, "text")
            for item in content
            if _tool_value(item, "type") == "text"
            and isinstance(_tool_value(item, "text"), str)
        ]
        if len(texts) != 1:
            raise TdxProtocolError("TDX MCP payload is malformed")
        try:
            payload = json.loads(texts[0])
        except json.JSONDecodeError:
            raise TdxProtocolError("TDX MCP payload is malformed") from None
    if not isinstance(payload, dict) or _payload_failed(payload):
        raise TdxProtocolError("TDX MCP payload is an error")
    return payload


class TdxMcpClient:
    """Synchronous governed facade over one official SDK session per attempt."""

    def __init__(self, *, session_factory: Callable = _official_sdk_session):
        self.session_factory = session_factory
        self.protocol_evidence: dict | None = None

    def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        auth_manager: TdxOwnedAuth,
    ) -> dict:
        if tool_name not in TOOL_ALLOWLIST:
            raise ValueError("TDX tool is not allowlisted")
        _validate_arguments(tool_name, arguments)
        self.protocol_evidence = {
            "initialize": "fail",
            "tools_list": "fail",
            "schema": "fail",
            "read_only": "fail",
            "tool_call": "fail",
            "page_count": 0,
            "session_count": 0,
            "refresh_count": 0,
            "call_count": 0,
        }
        authorization = auth_manager.authorization()
        try:
            return _run_async(
                lambda: self._call_once(tool_name, arguments, authorization)
            )
        except TdxForbidden:
            raise TdxForbidden("TDX MCP permission denied") from None
        except TdxUnauthorized:
            self.protocol_evidence["refresh_count"] += 1
            refreshed = auth_manager.authorization(
                force_refresh=True,
                rejected_authorization=authorization,
            )
            try:
                return _run_async(
                    lambda: self._call_once(tool_name, arguments, refreshed)
                )
            except TdxUnauthorized:
                raise TdxUnauthorized("TDX MCP authorization failed") from None
            except TdxForbidden:
                raise TdxForbidden("TDX MCP permission denied") from None

    async def _call_once(
        self,
        tool_name: str,
        arguments: dict,
        authorization: str,
    ) -> dict:
        try:
            async with self.session_factory(authorization) as session:
                self.protocol_evidence["session_count"] += 1
                await session.initialize()
                self.protocol_evidence["initialize"] = "pass"
                page_count = await self._gate_tool(session, tool_name)
                self.protocol_evidence.update(
                    {
                        "tools_list": "pass",
                        "schema": "pass",
                        "read_only": "pass",
                        "page_count": self.protocol_evidence["page_count"]
                        + page_count,
                    }
                )
                self.protocol_evidence["call_count"] += 1
                result = await session.call_tool(tool_name, dict(arguments))
                payload = _extract_payload(result)
                self.protocol_evidence["tool_call"] = "pass"
                return payload
        except Exception as error:
            _raise_sanitized_transport(error)
        raise AssertionError("unreachable")

    @staticmethod
    async def _gate_tool(session, tool_name: str) -> int:
        cursor = None
        for _page in range(10):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await session.list_tools(params=params)
            page_tools = _tool_value(result, "tools")
            if not isinstance(page_tools, list):
                raise TdxSchemaError("TDX MCP tools/list is malformed")
            for item in page_tools:
                name = _tool_value(item, "name")
                if name == tool_name:
                    _validate_tool_schema(item)
                    return _page + 1
            cursor = _tool_value(result, "next_cursor")
            if cursor is None:
                cursor = _tool_value(result, "nextCursor")
            if not cursor:
                break
        raise TdxSchemaError("TDX MCP requested tool is unavailable")


def _run_async(coroutine_factory: Callable):
    """Run one async MCP attempt from synchronous code, including loop threads."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine_factory())
    result = []
    failure = []

    def worker() -> None:
        try:
            result.append(asyncio.run(coroutine_factory()))
        except BaseException as error:
            failure.append(error)

    thread = threading.Thread(target=worker, name="ym-tdx-mcp-sync-bridge")
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0]


class TdxMcpProvider:
    def __init__(
        self,
        name: str,
        *,
        auth_manager: TdxOwnedAuth | None = None,
        client: TdxMcpClient | None = None,
        credential_store: FileCredentialStore | None = None,
    ):
        if name not in TDX_DIAGNOSTIC_NAMES:
            raise ValueError("unknown TDX provider")
        if auth_manager is not None and credential_store is not None:
            raise ValueError("provide auth_manager or credential_store, not both")
        self.name = name
        if auth_manager is None:
            store = credential_store or default_credential_store()
            auth_manager = TdxOwnedAuth(store=store)
        self.auth = auth_manager
        self.client = client or TdxMcpClient()

    def probe(self) -> dict:
        status = self.auth.probe()
        return {
            "provider": self.name,
            "status": status,
            "auth": {
                "required": True,
                "status": {
                    "auth_missing": "missing",
                    "auth_expired": "expired",
                }.get(status, "present"),
            },
        }

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        spec = TDX_PROVIDER_SPECS.get(self.name)
        if spec is None or spec[0] != intent:
            return ProviderOutcome(
                provider=self.name,
                status="incompatible",
                error_code="INCOMPATIBLE_INTENT",
                auth={"required": True, "status": "unverified"},
            )
        started = time.perf_counter()
        try:
            payload = self.client.call_tool(
                spec[1], _arguments(self.name, params), self.auth
            )
            data, count = _normalize(self.name, payload)
        except TdxAuthMissing:
            return self._failure(started, "auth_error", "AUTH_MISSING", "missing")
        except (TdxAuthExpired, TdxScopeError, TdxUnauthorized):
            return self._failure(started, "auth_error", "AUTH_EXPIRED", "expired")
        except TdxForbidden:
            return self._failure(started, "auth_error", "AUTH_FORBIDDEN", "forbidden")
        except (TimeoutError, socket.timeout):
            return self._failure(started, "timeout", "TIMEOUT", "present")
        except (TdxProtocolError, ValueError):
            return self._failure(started, "provider_error", "MCP_ERROR", "present")
        except TdxTransportError:
            return self._failure(started, "network_error", "NETWORK_ERROR", "present")
        except Exception:
            return self._failure(started, "provider_error", "PROVIDER_ERROR", "unverified")
        return ProviderOutcome(
            provider=self.name,
            status="success" if count else "empty",
            data=data,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            quality={
                "status": "normal" if count else "empty",
                "returned_count": count,
                "reason_codes": [],
            },
            auth={"required": True, "status": "ok"},
            provenance={"smoke_protocol": dict(self.client.protocol_evidence)}
            if isinstance(getattr(self.client, "protocol_evidence", None), dict)
            else None,
        )

    def _failure(
        self,
        started: float,
        status: str,
        error_code: str,
        auth_status: str,
    ) -> ProviderOutcome:
        return ProviderOutcome(
            provider=self.name,
            status=status,
            error_code=error_code,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            auth={"required": True, "status": auth_status},
        )
