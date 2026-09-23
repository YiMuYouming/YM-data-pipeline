"""Independently observable WenCai providers."""

from __future__ import annotations

import importlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import PYWENCAI_MANAGED_PYTHON
from ..provider_state import ProviderState
from ..sources import iwencai as transport_source
from .base import ProviderOutcome


_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SETUP_ACTION = "ym-data setup pywencai"


@dataclass(frozen=True)
class PyWenCaiRuntime:
    python: Path
    source: str


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _current_runtime_has_dependencies() -> bool:
    for module in ("pywencai", "pandas"):
        try:
            importlib.import_module(module)
        except Exception:
            return False
    return True


def discover_pywencai_runtime() -> PyWenCaiRuntime | None:
    """Resolve only explicit, current, or project-managed runtimes."""

    configured = os.environ.get("YM_PYWENCAI_PYTHON")
    if configured:
        explicit = Path(configured).expanduser()
        if _is_executable(explicit):
            return PyWenCaiRuntime(explicit, "environment")
    if _current_runtime_has_dependencies():
        return PyWenCaiRuntime(Path(sys.executable), "current")
    if _is_executable(PYWENCAI_MANAGED_PYTHON):
        return PyWenCaiRuntime(PYWENCAI_MANAGED_PYTHON, "managed")
    return None


def _compatible_call(intent: str, params: dict) -> tuple[str, int, int] | None:
    query = params.get("query") if isinstance(params, dict) else None
    if intent != "review_sentiment" or not isinstance(query, str) or not query.strip():
        return None
    limit = params.get("limit", 50)
    page = params.get("page", 1)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 0
        or not isinstance(page, int)
        or isinstance(page, bool)
        or page <= 0
    ):
        return None
    return query, limit, page


def _safe_error_code(value: object, default: str) -> str:
    candidate = str(value or "")
    return candidate if _SAFE_ERROR_CODE.fullmatch(candidate) else default


def _limit_payload(payload: dict, limit: int) -> dict:
    limited = transport_source._limit_pywencai_rows(payload, limit)
    if not isinstance(limited, dict):
        return {
            "error": "provider returned an invalid payload",
            "error_type": "INVALID_PAYLOAD",
            "_source": payload.get("_source", "unknown")
            if isinstance(payload, dict)
            else "unknown",
        }
    return limited


class IWenCaiOpenAPIProvider:
    name = "iwencai_openapi"

    def __init__(
        self,
        *,
        state: ProviderState | None = None,
        transport: Callable[[str, int, int], dict] | None = None,
    ):
        self.state = state if state is not None else ProviderState()
        self._transport = transport or transport_source._openapi_query

    def probe(self) -> dict:
        key_present = bool(transport_source._load_api_key())
        breaker = self.state.active_breaker(self.name)
        return {
            "provider": self.name,
            "status": "breaker_open"
            if breaker
            else ("configured_unverified" if key_present else "auth_missing"),
            "auth": {"required": True, "status": "present" if key_present else "missing"},
            "breaker": bool(breaker),
        }

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        parsed = _compatible_call(intent, params)
        if parsed is None:
            return ProviderOutcome(
                provider=self.name,
                status="incompatible",
                error_code="INCOMPATIBLE_INTENT",
            )
        query, limit, page = parsed
        breaker = self.state.active_breaker(self.name)
        if breaker:
            return ProviderOutcome(
                provider=self.name,
                status="breaker_open",
                error_code=breaker["error_code"],
                auth={"required": True, "status": "unverified"},
            )

        started = time.perf_counter()
        try:
            payload = self._transport(query, limit, page)
        except transport_source.IWenCaiAuthMissing:
            return self._outcome(
                started,
                status="auth_error",
                error_code="API_KEY_MISSING",
                auth_status="missing",
            )
        except urllib.error.HTTPError as error:
            error_code = f"HTTP_{error.code}"
            if error.code in {401, 403, 429}:
                failure_type = "rate_limit" if error.code == 429 else "auth_error"
                self.state.record_failure(
                    provider=self.name,
                    failure_type=failure_type,
                    error_code=error_code,
                    breaker_seconds=300,
                )
                return self._outcome(
                    started,
                    status="auth_error" if error.code in {401, 403} else "provider_error",
                    error_code=error_code,
                    auth_status="error" if error.code in {401, 403} else "present",
                )
            if 500 <= error.code <= 599:
                self.state.record_failure(
                    provider=self.name,
                    failure_type="provider_error",
                    error_code=error_code,
                    breaker_seconds=60,
                )
            return self._outcome(
                started,
                status="provider_error",
                error_code=error_code,
                auth_status="present",
            )
        except (TimeoutError, socket.timeout):
            self.state.record_failure(
                provider=self.name,
                failure_type="timeout",
                error_code="TIMEOUT",
                breaker_seconds=60,
            )
            return self._outcome(
                started,
                status="timeout",
                error_code="TIMEOUT",
                auth_status="present",
            )
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            is_timeout = isinstance(reason, (TimeoutError, socket.timeout))
            error_code = "TIMEOUT" if is_timeout else "NETWORK_ERROR"
            self.state.record_failure(
                provider=self.name,
                failure_type="timeout" if is_timeout else "network_error",
                error_code=error_code,
                breaker_seconds=60,
            )
            return self._outcome(
                started,
                status="timeout" if is_timeout else "network_error",
                error_code=error_code,
                auth_status="present",
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            error_code = _safe_error_code(type(error).__name__, "INVALID_RESPONSE")
            self.state.record_failure(
                provider=self.name,
                failure_type="provider_error",
                error_code=error_code,
                breaker_seconds=60,
            )
            return self._outcome(
                started,
                status="provider_error",
                error_code=error_code,
                auth_status="present",
            )
        except OSError:
            self.state.record_failure(
                provider=self.name,
                failure_type="network_error",
                error_code="NETWORK_ERROR",
                breaker_seconds=60,
            )
            return self._outcome(
                started,
                status="network_error",
                error_code="NETWORK_ERROR",
                auth_status="present",
            )
        except Exception as error:
            error_code = _safe_error_code(type(error).__name__, "PROVIDER_ERROR")
            return self._outcome(
                started,
                status="provider_error",
                error_code=error_code,
                auth_status="unverified",
            )

        limited = _limit_payload(payload, limit)
        if "error" in limited:
            return self._outcome(
                started,
                status="provider_error",
                error_code=_safe_error_code(
                    limited.get("error_type"), "INVALID_RESPONSE"
                ),
                auth_status="present",
            )
        rows = limited.get("datas")
        if not isinstance(rows, list):
            return self._outcome(
                started,
                status="provider_error",
                error_code="INVALID_RESPONSE",
                auth_status="present",
            )
        self.state.record_success(self.name)
        return ProviderOutcome(
            provider=self.name,
            status="success" if rows else "empty",
            data=limited,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            quality={
                "status": "normal",
                "returned_count": len(rows),
                "reason_codes": [],
            },
            auth={"required": True, "status": "ok"},
        )

    def _outcome(
        self,
        started: float,
        *,
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


class PyWenCaiProvider:
    name = "pywencai"

    def __init__(
        self,
        *,
        runtime_resolver: Callable[[], PyWenCaiRuntime | None] | None = None,
        runner: Callable[[Path, str, int], dict] | None = None,
    ):
        self._runtime_resolver = runtime_resolver or discover_pywencai_runtime
        self._runner = runner or self._run

    @staticmethod
    def _run(python: Path, query: str, limit: int) -> dict:
        return transport_source._pywencai_query(
            query,
            limit,
            python_executable=python,
        )

    def probe(self) -> dict:
        runtime = self._runtime_resolver()
        if runtime is None:
            return {
                "provider": self.name,
                "status": "dependency_missing",
                "action": _SETUP_ACTION,
            }
        return {
            "provider": self.name,
            "status": "configured_unverified",
            "runtime_source": runtime.source,
        }

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        parsed = _compatible_call(intent, params)
        if parsed is None:
            return ProviderOutcome(
                provider=self.name,
                status="incompatible",
                error_code="INCOMPATIBLE_INTENT",
            )
        query, limit, _page = parsed
        runtime = self._runtime_resolver()
        if runtime is None:
            return ProviderOutcome(
                provider=self.name,
                status="dependency_missing",
                error_code="PYWENCAI_RUNTIME_MISSING",
                detail=_SETUP_ACTION,
                auth={"required": False, "status": "not_required"},
            )

        started = time.perf_counter()
        try:
            payload = self._runner(runtime.python, query, limit)
        except Exception as error:
            return ProviderOutcome(
                provider=self.name,
                status="provider_error",
                error_code=_safe_error_code(type(error).__name__, "PROVIDER_ERROR"),
                detail="pywencai execution failed",
                latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                auth={"required": False, "status": "not_required"},
            )

        limited = _limit_payload(payload, limit)
        if "error" in limited:
            return ProviderOutcome(
                provider=self.name,
                status="provider_error",
                error_code=_safe_error_code(
                    limited.get("error_type"), "PYWENCAI_ERROR"
                ),
                # runner 注入 traceback 到 detail，失败时可远程定位（见 sources/iwencai.py）。
                detail=limited.get("detail") or "pywencai execution failed",
                latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                auth={"required": False, "status": "not_required"},
            )
        rows = limited.get("datas")
        if not isinstance(rows, list):
            return ProviderOutcome(
                provider=self.name,
                status="provider_error",
                error_code="INVALID_RESPONSE",
                detail="pywencai returned an invalid payload",
                latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                auth={"required": False, "status": "not_required"},
            )
        return ProviderOutcome(
            provider=self.name,
            status="success" if rows else "empty",
            data=limited,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            quality={
                "status": "normal",
                "returned_count": len(rows),
                "reason_codes": [],
            },
            auth={"required": False, "status": "not_required"},
        )
