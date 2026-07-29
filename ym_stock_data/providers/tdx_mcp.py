"""Pipeline-owned, read-only TDX MCP provider."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .base import ProviderOutcome


SERVER_URL = "https://txmcp.tdx.com.cn:3001/txmcp"
TOKEN_URL = "https://auth.tdx.com.cn/token"
REQUEST_TIMEOUT_SEC = 90
REFRESH_SKEW_MS = 5 * 60 * 1000
TDX_AUTH_PATH = Path.home() / ".ym-stock-data" / "auth" / "tdx.json"
WORKBUDDY_CONNECTORS_DIR = Path.home() / ".workbuddy" / "connectors"

TOOL_ALLOWLIST = frozenset(
    {
        "tdx_screener",
        "tdx_quotes",
        "tdx_kline",
        "wenda_report_query",
        "wenda_notice_query",
        "wenda_news_query",
    }
)
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


class CredentialImportError(RuntimeError):
    """A bounded credential import could not be completed safely."""


class TdxAuthMissing(RuntimeError):
    """No pipeline-owned TDX credentials exist."""


class TdxAuthExpired(RuntimeError):
    """TDX authorization cannot be refreshed."""


class TdxProtocolError(RuntimeError):
    """TDX returned an invalid or explicit error response."""


class TdxTransportError(RuntimeError):
    """TDX transport failed without exposing response content."""


def _now_ms() -> int:
    return int(time.time() * 1000)


class TdxCredentialStore:
    """Minimal owned OAuth store with private, atomic writes."""

    def __init__(self, path: str | Path = TDX_AUTH_PATH):
        self.path = Path(path).expanduser()

    def _load(self) -> dict:
        if not self.path.is_file():
            raise TdxAuthMissing("owned TDX credentials are missing")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TdxAuthExpired("owned TDX credentials are invalid") from exc
        if not isinstance(payload, dict):
            raise TdxAuthExpired("owned TDX credentials are invalid")
        return payload

    def probe(self) -> str:
        try:
            payload = self._load()
        except TdxAuthMissing:
            return "auth_missing"
        except TdxAuthExpired:
            return "auth_expired"
        expires_at = payload.get("expires_at_ms")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        client_id = payload.get("client_id")
        if not isinstance(expires_at, int):
            return "auth_expired"
        if expires_at <= _now_ms() + REFRESH_SKEW_MS and not (
            isinstance(refresh_token, str)
            and refresh_token
            and isinstance(client_id, str)
            and client_id
        ):
            return "auth_expired"
        if not isinstance(access_token, str) or not access_token:
            return "auth_expired" if not refresh_token else "configured_unverified"
        return "configured_unverified"

    def save(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".tdx-auth-",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                os.chmod(temporary_path, 0o600)
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def authorization(
        self,
        *,
        refresher: Callable[[dict], dict] = None,
    ) -> str:
        payload = self._load()
        access_token = payload.get("access_token")
        expires_at = payload.get("expires_at_ms")
        if (
            isinstance(access_token, str)
            and access_token
            and isinstance(expires_at, int)
            and expires_at > _now_ms() + REFRESH_SKEW_MS
        ):
            return f"{payload.get('token_type') or 'Bearer'} {access_token}"
        refresh_token = payload.get("refresh_token")
        client_id = payload.get("client_id")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise TdxAuthExpired("TDX refresh is unavailable")
        if not isinstance(client_id, str) or not client_id:
            raise TdxAuthExpired("TDX refresh is unavailable")
        refresh = refresher or refresh_access_token
        try:
            refreshed = refresh(dict(payload))
        except Exception as exc:
            raise TdxAuthExpired("TDX refresh failed") from exc
        new_access = refreshed.get("access_token") if isinstance(refreshed, dict) else None
        try:
            expires_in = int(refreshed.get("expires_in", 0))
        except (TypeError, ValueError, AttributeError):
            expires_in = 0
        if not isinstance(new_access, str) or not new_access or expires_in <= 0:
            raise TdxAuthExpired("TDX refresh failed")
        updated = {
            "schema_version": "1",
            "client_id": client_id,
            "access_token": new_access,
            "refresh_token": refreshed.get("refresh_token") or refresh_token,
            "token_type": refreshed.get("token_type") or payload.get("token_type") or "Bearer",
            "expires_at_ms": _now_ms() + expires_in * 1000,
        }
        self.save(updated)
        return f"{updated['token_type']} {updated['access_token']}"


def refresh_access_token(payload: dict) -> dict:
    form = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": payload["refresh_token"],
            "client_id": payload["client_id"],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise TdxAuthExpired("TDX refresh failed") from exc
    if not isinstance(result, dict):
        raise TdxAuthExpired("TDX refresh failed")
    return result


def _minimal_owned_credentials(data: dict) -> dict:
    oauth = data.get("mcpOAuth") if isinstance(data, dict) else None
    clients = data.get("mcpClientInfo") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not isinstance(clients, dict):
        raise CredentialImportError("credential container is invalid")
    matches = [
        (key, entry)
        for key, entry in oauth.items()
        if isinstance(entry, dict)
        and (
            entry.get("serverUrl") == SERVER_URL
            or entry.get("serverName") == "tdx-connector"
        )
    ]
    if len(matches) != 1:
        raise CredentialImportError("TDX credential selection is ambiguous")
    key, entry = matches[0]
    client = clients.get(key)
    client_id = client.get("client_id") if isinstance(client, dict) else None
    refresh_token = entry.get("refreshToken")
    if not isinstance(client_id, str) or not client_id:
        raise CredentialImportError("TDX client metadata is incomplete")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CredentialImportError("TDX refresh metadata is incomplete")
    try:
        expires_at = int(entry.get("expiresAt") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    return {
        "schema_version": "1",
        "client_id": client_id,
        "access_token": entry.get("accessToken"),
        "refresh_token": refresh_token,
        "token_type": entry.get("tokenType") or "Bearer",
        "expires_at_ms": expires_at,
    }


def import_workbuddy_credentials(
    *,
    source_root: str | Path = WORKBUDDY_CONNECTORS_DIR,
    target: str | Path = TDX_AUTH_PATH,
    emit: Callable[[str], object] = print,
) -> dict:
    """Read exactly one bounded WorkBuddy candidate and import one TDX entry."""

    target_path = Path(target).expanduser()
    emit(str(target_path))
    root = Path(source_root).expanduser()
    candidates = sorted(
        path for path in root.glob("*/.credentials.json") if path.is_file()
    ) if root.is_dir() else []
    if len(candidates) != 1:
        raise CredentialImportError("credential candidate selection is ambiguous")
    try:
        source = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CredentialImportError("credential candidate is invalid") from exc
    store = TdxCredentialStore(target_path)
    store.save(_minimal_owned_credentials(source))
    return {"status": "ready", "target": str(target_path)}


def import_tdx_credentials(*, from_workbuddy: bool = False) -> dict:
    if not from_workbuddy:
        return {
            "status": "unavailable",
            "action": "pass --from-workbuddy for an explicit bounded import",
        }
    return import_workbuddy_credentials()


def _parse_messages(body: str) -> list[dict]:
    body = body.strip()
    if not body:
        return []
    if body.startswith("{"):
        value = json.loads(body)
        return [value] if isinstance(value, dict) else []
    messages = []
    for block in body.split("\n\n"):
        data = "\n".join(
            line[5:].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ).strip()
        if data and data != "[DONE]":
            value = json.loads(data)
            if isinstance(value, dict):
                messages.append(value)
    return messages


class _HttpSession:
    def __init__(self):
        self.session_id: str | None = None

    def send(self, message: dict, authorization: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": authorization,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            SERVER_URL,
            data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise TdxAuthExpired("TDX HTTP authorization failed") from exc
            raise TdxTransportError("TDX HTTP request failed") from exc
        except (TimeoutError, socket.timeout):
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise TdxTransportError("TDX transport failed") from exc
        try:
            messages = _parse_messages(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TdxProtocolError("TDX returned invalid JSON") from exc
        message_id = message.get("id")
        if message_id is None:
            return {}
        for candidate in messages:
            if candidate.get("id") == message_id:
                return candidate
        raise TdxProtocolError("TDX response did not match the request")


class TdxMcpClient:
    def __init__(
        self,
        *,
        sender: Callable[[dict, str], dict] | None = None,
        skip_initialize: bool = False,
    ):
        self._session = _HttpSession() if sender is None else None
        self._sender = sender or self._session.send
        self._initialized = bool(skip_initialize)
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _request(self, method: str, params: dict, authorization: str) -> dict:
        response = self._sender(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": method,
                "params": params,
            },
            authorization,
        )
        if not isinstance(response, dict) or response.get("error"):
            raise TdxProtocolError("TDX MCP returned an error")
        result = response.get("result")
        if not isinstance(result, dict):
            raise TdxProtocolError("TDX MCP returned an invalid result")
        return result

    def _ensure_initialized(self, authorization: str) -> None:
        if self._initialized:
            return
        self._request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ym-stock-data", "version": "1.0"},
            },
            authorization,
        )
        self._sender(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            authorization,
        )
        self._initialized = True

    def call_tool(self, tool_name: str, arguments: dict, authorization: str) -> dict:
        if tool_name not in TOOL_ALLOWLIST:
            raise ValueError("TDX tool is not allowlisted")
        self._ensure_initialized(authorization)
        result = self._request(
            "tools/call",
            {"name": tool_name, "arguments": dict(arguments)},
            authorization,
        )
        if _payload_failed(result):
            raise TdxProtocolError("TDX MCP tool returned an error")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            payload = structured
        else:
            content = result.get("content")
            if not isinstance(content, list):
                raise TdxProtocolError("TDX MCP payload is malformed")
            texts = [
                item.get("text")
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ]
            if len(texts) != 1:
                raise TdxProtocolError("TDX MCP payload is malformed")
            try:
                payload = json.loads(texts[0])
            except json.JSONDecodeError as exc:
                raise TdxProtocolError("TDX MCP payload is malformed") from exc
        if not isinstance(payload, dict) or _payload_failed(payload):
            raise TdxProtocolError("TDX MCP payload is an error")
        return payload


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


class TdxMcpProvider:
    def __init__(
        self,
        name: str,
        *,
        credential_store: TdxCredentialStore | None = None,
        client: TdxMcpClient | None = None,
        refresher: Callable[[dict], dict] | None = None,
    ):
        if name not in TDX_DIAGNOSTIC_NAMES:
            raise ValueError("unknown TDX provider")
        self.name = name
        self.store = credential_store or TdxCredentialStore()
        self.client = client or TdxMcpClient()
        self.refresher = refresher or refresh_access_token

    def probe(self) -> dict:
        status = self.store.probe()
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
            authorization = self.store.authorization(refresher=self.refresher)
            payload = self.client.call_tool(
                spec[1], _arguments(self.name, params), authorization
            )
            data, count = _normalize(self.name, payload)
        except TdxAuthMissing:
            return self._failure(started, "auth_error", "AUTH_MISSING", "missing")
        except TdxAuthExpired:
            return self._failure(started, "auth_error", "AUTH_EXPIRED", "expired")
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
