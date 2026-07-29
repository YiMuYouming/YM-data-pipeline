"""Governed Wind CLI provider for explicit enrichment and narrow fallbacks."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..contracts import TZ_SHANGHAI
from .base import ProviderOutcome


WIND_CONFIG_PATH = Path.home() / ".wind-aifinmarket" / "config"
WIND_PROVIDER_NAMES = ("wind_mcp", "wind_documents")
# No stock-event subtype has yet demonstrated field-level semantic equivalence.
WIND_EVENT_ALLOWLIST = frozenset()
WIND_ENRICHMENT_CAPABILITIES = {
    "company_profile": {
        "server_type": "stock_data",
        "tool_name": "get_stock_basicinfo",
        "parameter": "question",
    },
    "fundamentals": {
        "server_type": "stock_data",
        "tool_name": "get_stock_fundamentals",
        "parameter": "question",
    },
    "equity_holders": {
        "server_type": "stock_data",
        "tool_name": "get_stock_equity_holders",
        "parameter": "question",
    },
    "company_events": {
        "server_type": "stock_data",
        "tool_name": "get_stock_events",
        "parameter": "question",
    },
    "risk_metrics": {
        "server_type": "stock_data",
        "tool_name": "get_risk_metrics",
        "parameter": "question",
    },
    "index_fundamentals": {
        "server_type": "index_data",
        "tool_name": "get_index_fundamentals",
        "parameter": "question",
    },
    "announcements": {
        "server_type": "financial_docs",
        "tool_name": "get_company_announcements",
        "parameter": "query",
    },
}
_AUTH_CODES = frozenset({"AUTH_ERROR", "HTTP_401", "HTTP_403"})


@dataclass(frozen=True)
class WindRuntime:
    path: Path
    scope: str


def _has_cli(path: Path) -> bool:
    return (path / "scripts" / "cli.mjs").is_file()


def discover_wind_runtime(
    *,
    skill_dir: str | Path | None = None,
    global_dir: str | Path | None = None,
    project_compat_dir: str | Path | None = None,
) -> WindRuntime | None:
    configured = skill_dir or os.environ.get("WIND_MCP_SKILL_DIR")
    global_path = Path(global_dir).expanduser() if global_dir else (
        Path.home() / ".agents" / "skills" / "wind-mcp-skill"
    )
    project_path = Path(project_compat_dir).expanduser() if project_compat_dir else (
        Path.home()
        / "Documents"
        / "YM_Capital"
        / "YiMu_IR"
        / ".agents"
        / "skills"
        / "wind-mcp-skill"
    )
    candidates = []
    if configured:
        candidates.append((Path(configured).expanduser(), "global"))
    candidates.extend(((global_path, "global"), (project_path, "project_compat")))
    seen = set()
    for candidate, scope in candidates:
        normalized = candidate.resolve()
        if normalized in seen:
            continue
        seen.add(normalized)
        if _has_cli(normalized):
            return WindRuntime(normalized, scope)
    return None


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _failed(payload: dict) -> bool:
    if payload.get("error") not in (None, False, ""):
        return True
    if payload.get("isError") is True or payload.get("success") is False:
        return True
    return str(payload.get("status") or "").lower() in {
        "error",
        "failed",
        "failure",
        "auth_error",
        "unavailable",
    }


def _extract_payload(envelope: object) -> dict:
    if not isinstance(envelope, dict) or _failed(envelope):
        raise ValueError("WIND_PAYLOAD_ERROR")
    content = envelope.get("content")
    if isinstance(content, list):
        texts = [
            item.get("text")
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        if len(texts) != 1:
            raise ValueError("INVALID_RESPONSE")
        try:
            payload = json.loads(texts[0])
        except json.JSONDecodeError as exc:
            raise ValueError("INVALID_RESPONSE") from exc
    else:
        payload = envelope
    if not isinstance(payload, dict) or _failed(payload):
        raise ValueError("WIND_PAYLOAD_ERROR")
    return payload


def _enrichment_rows(payload: dict) -> list:
    for key in ("rows", "items", "data"):
        if key in payload:
            value = payload[key]
            if not isinstance(value, list):
                raise ValueError("INVALID_RESPONSE")
            return value
    raise ValueError("INVALID_RESPONSE")


def _filing_rows(payload: dict) -> list:
    rows = payload.get("filings")
    if not isinstance(rows, list):
        raise ValueError("INVALID_RESPONSE")
    return rows


class WindMcpProvider:
    def __init__(
        self,
        name: str,
        *,
        skill_dir: str | Path | None = None,
        config_path: str | Path = WIND_CONFIG_PATH,
        runtime_scope: str | None = None,
        runner: Callable = subprocess.run,
        node_bin: str = "node",
        timeout: float = 60,
    ):
        if name not in WIND_PROVIDER_NAMES:
            raise ValueError("unknown Wind provider")
        self.name = name
        self.skill_dir = Path(skill_dir).expanduser() if skill_dir is not None else None
        # Compatibility-only argument. Authentication discovery belongs to the
        # official CLI, which may use global, Skill-local, or environment state.
        self.config_path = Path(config_path).expanduser()
        self.runtime_scope = runtime_scope
        self.runner = runner
        self.node_bin = node_bin
        self.timeout = timeout

    def _runtime(self) -> WindRuntime | None:
        runtime = discover_wind_runtime(skill_dir=self.skill_dir)
        if runtime is not None and self.runtime_scope in {"global", "project_compat"}:
            return WindRuntime(runtime.path, self.runtime_scope)
        return runtime

    def probe(self) -> dict:
        runtime = self._runtime()
        if runtime is None:
            return {
                "provider": self.name,
                "status": "dependency_missing",
                "runtime_scope": "missing",
                "action": "install wind-mcp-skill or configure WIND_MCP_SKILL_DIR",
                "auth": {"required": True, "status": "unverified"},
            }
        return {
            "provider": self.name,
            "status": "configured_unverified",
            "runtime_scope": runtime.scope,
            "auth": {"required": True, "status": "unverified"},
        }

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        compatible = self._compatible_call(intent, params)
        if compatible is None:
            return ProviderOutcome(
                provider=self.name,
                status="incompatible",
                error_code="INCOMPATIBLE_INTENT",
                auth={"required": True, "status": "unverified"},
            )
        capability, question, top_k, lang = compatible
        runtime = self._runtime()
        if runtime is None:
            return self._failure("dependency_missing", "CLI_NOT_FOUND", "unverified")
        spec = WIND_ENRICHMENT_CAPABILITIES[capability]
        arguments = {spec["parameter"]: _compact(question)}
        if not arguments[spec["parameter"]]:
            return self._failure("provider_error", "INVALID_PARAMS", "present")
        if spec["parameter"] == "question":
            arguments["lang"] = lang
            if top_k is not None:
                arguments["top_k"] = top_k
        else:
            arguments["top_k"] = top_k or 5
        command = [
            self.node_bin,
            str(runtime.path / "scripts" / "cli.mjs"),
            "call",
            spec["server_type"],
            spec["tool_name"],
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        ]
        started = time.perf_counter()
        try:
            completed = self.runner(
                command,
                cwd=str(runtime.path),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return self._failure("timeout", "TIMEOUT", "present", started)
        except (OSError, ValueError):
            return self._failure("dependency_missing", "RUNTIME_ERROR", "present", started)
        try:
            envelope = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            return self._failure("provider_error", "INVALID_RESPONSE", "present", started)
        if completed.returncode != 0:
            error = envelope.get("error") if isinstance(envelope, dict) else None
            remote_code = error.get("code") if isinstance(error, dict) else None
            code = "AUTH_ERROR" if remote_code in _AUTH_CODES else "WIND_CLI_ERROR"
            status = "auth_error" if code == "AUTH_ERROR" else "provider_error"
            return self._failure(status, code, "error" if status == "auth_error" else "present", started)
        try:
            payload = _extract_payload(envelope)
            rows = (
                _filing_rows(payload)
                if intent == "filings"
                else _enrichment_rows(payload)
            )
            data = self._normalize(intent, params, capability, payload, rows)
        except ValueError as error:
            error_code = (
                "INVALID_RESPONSE"
                if str(error) == "INVALID_RESPONSE"
                else "WIND_PAYLOAD_ERROR"
            )
            return self._failure(
                "provider_error",
                error_code,
                "present",
                started,
            )
        count = len(rows)
        return ProviderOutcome(
            provider=self.name,
            status="success" if count else "empty",
            data=data,
            fetched_at=datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds"),
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            quality={
                "status": "normal" if count else "empty",
                "returned_count": count,
                "reason_codes": [],
            },
            auth={"required": True, "status": "ok"},
        )

    def _compatible_call(
        self, intent: str, params: dict
    ) -> tuple[str, str, int | None, str] | None:
        if self.name == "wind_mcp" and intent == "wind_enrichment":
            capability = params.get("capability")
            if capability not in WIND_ENRICHMENT_CAPABILITIES:
                return None
            nested = params.get("params") or {}
            if not isinstance(nested, dict):
                return None
            question = nested.get("question")
            if not question:
                values = [params.get("code"), *(params.get("codes") or []), *(params.get("fields") or [])]
                question = " ".join(str(value) for value in values if value)
            top_k = nested.get("top_k")
            if top_k is not None and (
                not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0
            ):
                return None
            lang = nested.get("lang", "中文")
            if not isinstance(lang, str) or not lang.strip():
                return None
            return capability, str(question or ""), top_k, lang.strip()
        if self.name == "wind_documents" and intent == "filings":
            top_k = params.get("max_pages", 3)
            return (
                "announcements",
                f"{params.get('code', '')} 最近{params.get('days', 90)}天公告",
                max(1, int(top_k)),
                "中文",
            )
        return None

    @staticmethod
    def _normalize(
        intent: str,
        params: dict,
        capability: str,
        payload: dict,
        rows: list,
    ) -> dict:
        if intent == "filings":
            return {"filings": rows}
        return {"capability": capability, "items": rows}

    def _failure(
        self,
        status: str,
        error_code: str,
        auth_status: str,
        started: float | None = None,
    ) -> ProviderOutcome:
        return ProviderOutcome(
            provider=self.name,
            status=status,
            error_code=error_code,
            latency_ms=0 if started is None else max(0, int((time.perf_counter() - started) * 1000)),
            auth={"required": True, "status": auth_status},
        )
