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

# Schemas verified against the live TDX MCP server 2026-07-31.
# Note: server properties use `anyOf` for numeric-or-string fields; the gate
# treats the contract type as an acceptable member of that set.
TOOL_SCHEMA_CONTRACTS = {
    "tdx_screener": {
        "required": frozenset({"message"}),
        "properties": {
            "message": ("string", None),
            "rang": ("string", None),
            "pageNo": ("string", None),
            "pageSize": ("string", None),
        },
    },
    "tdx_quotes": {
        "required": frozenset({"code", "setcode"}),
        "properties": {
            "code": ("string", None),
            "setcode": ("string", None),
        },
    },
    "tdx_kline": {
        "required": frozenset({"code", "setcode"}),
        "properties": {
            "code": ("string", None),
            "setcode": ("string", None),
            "period": ("string", None),
            "wantNum": ("string", None),
        },
    },
    "wenda_report_query": {
        "required": frozenset(),
        "properties": {
            "query": ("string", None),
            "symbol": ("string", None),
            "name": ("string", None),
            "bdate": ("string", None),
            "edate": ("string", None),
        },
    },
    "wenda_notice_query": {
        "required": frozenset(),
        "properties": {
            "query": ("string", None),
            "symbol": ("string", None),
            "name": ("string", None),
            "bdate": ("string", None),
            "edate": ("string", None),
        },
    },
    "wenda_news_query": {
        "required": frozenset(),
        "properties": {
            "query": ("string", None),
            "symbol": ("string", None),
            "name": ("string", None),
            "bdate": ("string", None),
            "edate": ("string", None),
        },
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


def _setcode(code: str) -> str:
    """Market code required by the TDX MCP server (1=SH, 0=SZ)."""
    return "1" if str(code).startswith(("6", "68")) else "0"


def _arguments(provider_name: str, params: dict) -> dict:
    if provider_name == "tdx_screener":
        return {
            "message": params["query"],
            "rang": "AG",
            "pageNo": "1",
            "pageSize": str(params.get("limit", 50)),
        }
    if provider_name == "tdx_quotes":
        # TDX MCP serves one code per call; the router falls back to it only
        # when other quote sources are unavailable, so the first code is used
        # and the rest are reported as missing by the router's coverage check.
        code = str(params["codes"][0])
        return {"code": code, "setcode": _setcode(code)}
    if provider_name == "tdx_kline":
        # TDX MCP encodes periods as numbers: "4"=daily, "5"=weekly,
        # "6"=monthly, "3"=60m, "1"=15m, "0"=5m; bars are requested via wantNum.
        period_map = {
            "daily": "4",
            "weekly": "5",
            "monthly": "6",
            "60m": "3",
            "30m": "2",
            "15m": "1",
            "5m": "0",
            "1m": "7",
        }
        period = period_map.get(str(params.get("period", "daily")), "4")
        return {
            "code": str(params["code"]),
            "setcode": _setcode(str(params["code"])),
            "period": period,
            "wantNum": str(params.get("count", 100)),
        }
    if provider_name in {"tdx_report", "tdx_notice"}:
        result = {"query": f"{params['code']} 公告/研报"}
        if params.get("days") is not None:
            result["bdate"] = "20200101"
            result["edate"] = "20991231"
        return result
    if provider_name == "tdx_news":
        return {"query": "A股 要闻", "symbol": ""}
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


def _normalize(
    provider_name: str, payload: dict, *, code: str | None = None
) -> tuple[dict, int]:
    if _payload_failed(payload):
        raise TdxProtocolError("TDX payload is an explicit failure")
    if provider_name == "tdx_screener":
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise TdxProtocolError("TDX payload is missing its expected container")
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                raise TdxProtocolError("TDX screener row is invalid")
            normalized.append(
                {
                    "股票代码": str(row.get("sec_code") or row.get("POS") or ""),
                    "股票简称": str(row.get("sec_name") or ""),
                    "最新价": row.get("now_price"),
                    "涨幅": row.get("chg"),
                }
            )
        return {"datas": normalized, "row_count": len(normalized)}, len(normalized)
    if provider_name == "tdx_quotes":
        hq = payload.get("HQInfo")
        if not isinstance(hq, dict):
            raise TdxProtocolError("TDX payload is missing its expected container")
        if not code:
            raise TdxProtocolError("TDX quotes requires code context")
        ext = payload.get("ExtInfo") if isinstance(payload.get("ExtInfo"), dict) else {}
        row = {
            "code": code,
            "price": hq.get("Now"),
            "open": hq.get("Open"),
            "high": hq.get("MaxP"),
            "low": hq.get("MinP"),
            "last_close": hq.get("Close"),
            "volume": hq.get("Volume"),
            "amount": hq.get("Amount"),
            "turnover_rate": hq.get("HSL"),
            "name": ext.get("Name") or "",
        }
        return {str(code): row}, 1
    if provider_name == "tdx_kline":
        rows = payload.get("Rows")
        if not isinstance(rows, list):
            raise TdxProtocolError("TDX payload is missing its expected container")
        attach = payload.get("AttachInfo") if isinstance(payload.get("AttachInfo"), dict) else {}
        bars = []
        for row in rows:
            if not isinstance(row, dict):
                raise TdxProtocolError("TDX kline row is invalid")
            bars.append(
                {
                    "time": str(row.get("Data") or row.get("date") or row.get("time") or ""),
                    "open": row.get("Open"),
                    "high": row.get("High"),
                    "low": row.get("Low"),
                    "close": row.get("Close"),
                    "volume": row.get("Volume") if row.get("Volume") is not None else row.get("VolInStock"),
                    "amount": row.get("Amount"),
                }
            )
        return {"bars": bars, "name": attach.get("Name") or ""}, len(bars)
    container = {
        "tdx_report": "reports",
        "tdx_notice": "filings",
        "tdx_news": "items",
    }[provider_name]
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        return {container: []}, 0
    header = rows[0]
    if not isinstance(header, list):
        raise TdxProtocolError("TDX payload is missing its expected container")
    items = []
    for row in rows[1:]:
        if not isinstance(row, list):
            raise TdxProtocolError("TDX table row is invalid")
        items.append(dict(zip(header, row)))
    return {container: items}, len(items)


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


def _property_type_set(definition: object) -> set[str]:
    """Acceptable JSON-Schema types for one property definition.

    Handles both `{"type": "string"}` and the server's
    `{"anyOf": [{"type": "string"}, {"type": "number"}]}` form.
    """
    if not isinstance(definition, dict):
        return set()
    direct = definition.get("type")
    if isinstance(direct, str):
        return {direct}
    anyof = definition.get("anyOf")
    if isinstance(anyof, list):
        return {
            item.get("type")
            for item in anyof
            if isinstance(item, dict) and isinstance(item.get("type"), str)
        }
    return set()


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
        allowed = _property_type_set(definition)
        if not allowed or expected_type not in allowed:
            if not (expected_type == "integer" and "number" in allowed):
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
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
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
                await self._gate_tool(session, tool_name)
                self.protocol_evidence["call_count"] += 1
                result = await session.call_tool(tool_name, dict(arguments))
                payload = _extract_payload(result)
                self.protocol_evidence["tool_call"] = "pass"
                return payload
        except Exception as error:
            _raise_sanitized_transport(error)
        raise AssertionError("unreachable")

    async def _gate_tool(self, session, tool_name: str) -> None:
        cursor = None
        for _page in range(10):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await session.list_tools(params=params)
            page_tools = _tool_value(result, "tools")
            if not isinstance(page_tools, list):
                raise TdxSchemaError("TDX MCP tools/list is malformed")
            self.protocol_evidence["page_count"] += 1
            self.protocol_evidence["tools_list"] = "pass"
            for item in page_tools:
                name = _tool_value(item, "name")
                if name == tool_name:
                    _validate_tool_schema(item)
                    self.protocol_evidence["schema"] = "pass"
                    self.protocol_evidence["read_only"] = "pass"
                    return
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
            call_code = None
            if self.name == "tdx_quotes" and isinstance(params.get("codes"), list):
                codes = params["codes"]
                if codes:
                    call_code = str(codes[0])
            data, count = _normalize(self.name, payload, code=call_code)
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
            provenance={"smoke_protocol": dict(self.client.protocol_evidence)}
            if isinstance(getattr(self.client, "protocol_evidence", None), dict)
            else None,
        )
