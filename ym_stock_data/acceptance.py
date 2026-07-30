"""Offline builder and validator for five-day acceptance metadata."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
from collections import Counter
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

from .smoke_contract import (
    CASE_SPECS,
    CURRENT_SMOKE_BASELINE,
    CURRENT_SMOKE_CASE_IDS,
    CURRENT_SMOKE_SCHEMA_VERSION,
    LEGACY_SMOKE_CASE_COUNT,
    LEGACY_SMOKE_SCHEMA_VERSION,
)


SCHEMA = "ym-stock-data.acceptance.daily"
SCHEMA_VERSION = "1.3"
PREVIOUS_SCHEMA_VERSION = "1.1"
LEGACY_SCHEMA_VERSION = "1.0"
UNPUBLISHED_SCHEMA_VERSION = "1.2"
UNPUBLISHED_SMOKE_BASELINE = "five-source-structured-v1"
REQUIRED_TRADING_DAYS = 5
EARLIEST_ACCEPTANCE_TIME = time(16, 10)
ACCEPTANCE_DIR = Path.home() / ".ym-stock-data" / "acceptance"
TZ_SHANGHAI = timezone(timedelta(hours=8))
SSE_EXCHANGE = "Shanghai Stock Exchange"
SSE_CALENDAR_BASE_URL = "https://www.sse.com.cn/"
PENDING_STATUS = "pending"

_SAFE_ENUM = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_HEAD = re.compile(r"^[0-9a-f]{40}$")
_FORBIDDEN_KEYS = frozenset(
    {
        "data",
        "rows",
        "raw",
        "query",
        "token",
        "key",
        "authorization",
        "cookie",
        "stderr",
        "stdout",
        "exception",
        "traceback",
        "session",
        "transport",
        "credential",
        "secret",
        "payload",
        "body",
    }
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:bearer\s+|authorization\s*[:=]|api[_-]?key\s*[:=]|"
    r"access[_-]?token\s*[:=]|refresh[_-]?token\s*[:=]|"
    r"client[_-]?secret\s*[:=]|cookie\s*[:=]|traceback\s*\()"
)
_ATTEMPT_STATUSES = frozenset(
    {
        "success",
        "empty",
        "auth_error",
        "dependency_missing",
        "timeout",
        "network_error",
        "provider_error",
        "breaker_open",
        "incompatible",
    }
)
_PROVIDER_STATES = frozenset(
    {
        "ready",
        "auth_missing",
        "auth_expired",
        "dependency_missing",
        "configured_unverified",
        "breaker_open",
        "unavailable",
    }
)
_CASE_STATUSES = _ATTEMPT_STATUSES | _PROVIDER_STATES | frozenset(
    {"degraded", "error"}
)
_DIRECT_SHORT_CIRCUIT_STATES = frozenset(
    {
        "auth_missing",
        "auth_expired",
        "dependency_missing",
        "breaker_open",
        "unavailable",
    }
)
_SAFE_PARAM_KEYS = frozenset(
    {
        "sample_id", "fixture_id", "code", "codes", "event", "period",
        "count", "limit", "capability", "days", "max_pages",
    }
)
_SAFETY_KEYS = frozenset(
    {
        "broker_or_trading_call",
        "business_or_production_data_write",
        "business_rows_stored",
        "credential_values_stored",
        "deployment",
        "exception_or_stderr_text_stored",
        "git_push",
        "http_8088_post",
        "metadata_only",
        "zero_secret_scan",
    }
)
_PROTECTED_ARTIFACTS = (
    "ym_stock_data/experimental/__pycache__/__init__.cpython-314.pyc",
    "ym_stock_data/experimental/__pycache__/wind_sidecar.cpython-314.pyc",
)


class AcceptanceError(ValueError):
    """Stable, sanitized acceptance failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _raise(code: str) -> None:
    raise AcceptanceError(code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        _raise("INPUT_UNAVAILABLE")
    return digest.hexdigest()


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _raise("INVALID_JSON")
    if not isinstance(value, dict):
        _raise("INVALID_JSON")
    return value


def _reject_forbidden(value: object, *, code: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.strip().lower() in _FORBIDDEN_KEYS:
                _raise(code)
            _reject_forbidden(item, code=code)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden(item, code=code)
    elif isinstance(value, str) and _SENSITIVE_VALUE.search(value):
        _raise(code)


def _mapping(value: object, code: str = "INVALID_INPUT") -> dict:
    if not isinstance(value, dict):
        _raise(code)
    return value


def _exact_keys(
    value: dict,
    *,
    required: set[str] | frozenset[str],
    optional: set[str] | frozenset[str] = frozenset(),
    code: str = "INVALID_INPUT",
) -> None:
    keys = set(value)
    if not set(required).issubset(keys) or not keys.issubset(set(required) | set(optional)):
        _raise(code)


def _enum(value: object, code: str = "INVALID_INPUT") -> str:
    if not isinstance(value, str) or not _SAFE_ENUM.fullmatch(value):
        _raise(code)
    return value


def _integer(value: object, code: str = "INVALID_INPUT") -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _raise(code)
    return value


def _boolean(value: object, code: str = "INVALID_INPUT") -> bool:
    if not isinstance(value, bool):
        _raise(code)
    return value


def _iso(value: object, code: str = "INVALID_INPUT") -> str:
    if not isinstance(value, str):
        _raise(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _raise(code)
    if parsed.tzinfo is None:
        _raise(code)
    return value


def _date(value: object, code: str = "INVALID_DATE") -> str:
    if not isinstance(value, str):
        _raise(code)
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError:
        _raise(code)
    if parsed.isoformat() != value:
        _raise(code)
    return value


def _short_text(value: object, code: str = "INVALID_INPUT", limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or _SENSITIVE_VALUE.search(value):
        _raise(code)
    return value


def _project_attempts(
    value: object,
    code: str = "INVALID_INPUT",
    *,
    include_origin: bool = False,
) -> list[dict]:
    if not isinstance(value, list):
        _raise(code)
    result = []
    for raw in value:
        item = _mapping(raw, code)
        required = {"provider", "status", "error_code", "latency_ms"}
        if include_origin:
            required.add("origin")
        _exact_keys(item, required=required, code=code)
        provider = _enum(item["provider"], code)
        status = _enum(item["status"], code)
        if status not in _ATTEMPT_STATUSES:
            _raise(code)
        error_code = item["error_code"]
        if error_code is not None:
            error_code = _enum(error_code, code)
        projected = {
            "provider": provider,
            "status": status,
            "error_code": error_code,
            "latency_ms": _integer(item["latency_ms"], code),
        }
        if include_origin:
            origin = _enum(item["origin"], code)
            if origin not in {"live", "injected"}:
                _raise(code)
            projected["origin"] = origin
        result.append(projected)
    return result


def _project_provider_result(
    value: object,
    *,
    extra: frozenset[str] = frozenset(),
    include_origin: bool = False,
) -> dict:
    item = _mapping(value)
    required = {"status", "provider_used", "attempts"}
    optional = {"row_count", "error_code", "latency_ms"} | set(extra)
    _exact_keys(item, required=required, optional=optional)
    status = _enum(item["status"])
    if status not in _CASE_STATUSES:
        _raise("INVALID_INPUT")
    provider = item["provider_used"]
    if provider is not None:
        provider = _enum(provider)
    result = {
        "status": status,
        "provider_used": provider,
        "attempts": _project_attempts(
            item["attempts"], include_origin=include_origin
        ),
    }
    if "row_count" in item:
        result["row_count"] = _integer(item["row_count"])
    if "error_code" in item:
        error_code = item["error_code"]
        result["error_code"] = None if error_code is None else _enum(error_code)
    if "latency_ms" in item:
        result["latency_ms"] = _integer(item["latency_ms"])
    return result


def _project_doctor(value: dict) -> dict:
    _reject_forbidden(value, code="FORBIDDEN_INPUT")
    _exact_keys(value, required={"schema_version", "providers", "summary"})
    if value["schema_version"] != "1":
        _raise("INVALID_INPUT")
    providers = _mapping(value["providers"])
    projected = {}
    for raw_name, raw_item in sorted(providers.items()):
        name = _enum(raw_name)
        item = _mapping(raw_item)
        _exact_keys(
            item,
            required={"provider", "status"},
            optional={"breaker", "action", "runtime_source", "runtime_scope", "auth"},
        )
        if item["provider"] != name:
            _raise("INVALID_INPUT")
        status = _enum(item["status"])
        if status not in _PROVIDER_STATES:
            _raise("INVALID_INPUT")
        output = {"status": status}
        if "breaker" in item:
            output["breaker"] = _boolean(item["breaker"])
        for key in ("action", "runtime_source", "runtime_scope"):
            if key in item:
                output[key] = _short_text(item[key], limit=128)
        if "auth" in item:
            auth = _mapping(item["auth"])
            _exact_keys(auth, required={"required", "status"})
            output["auth"] = {
                "required": _boolean(auth["required"]),
                "status": _enum(auth["status"]),
            }
        projected[name] = output
    counts = Counter(item["status"] for item in projected.values())
    return {
        "command": "./ym-data doctor --json",
        "schema_version": "1",
        "run_count_for_acceptance": 1,
        "providers": projected,
        "summary": dict(sorted(counts.items())),
    }


def _project_safe_params(value: object) -> dict:
    params = _mapping(value)
    if not set(params).issubset(_SAFE_PARAM_KEYS):
        _raise("INVALID_INPUT")
    projected = {}
    for key, raw in params.items():
        if isinstance(raw, bool) or not isinstance(raw, (str, int, list)):
            _raise("INVALID_INPUT")
        if isinstance(raw, str):
            projected[key] = _short_text(raw, limit=64)
        elif isinstance(raw, int):
            projected[key] = _integer(raw)
        else:
            if len(raw) > 20 or any(not isinstance(item, str) for item in raw):
                _raise("INVALID_INPUT")
            projected[key] = [_short_text(item, limit=32) for item in raw]
    return projected


def _project_protocol(value: object) -> dict | None:
    if value is None:
        return None
    item = _mapping(value)
    state_keys = {"initialize", "tools_list", "schema", "read_only", "tool_call"}
    count_keys = {"page_count", "session_count", "refresh_count", "call_count"}
    _exact_keys(item, required=state_keys | count_keys)
    result = {}
    for key in sorted(state_keys):
        state = _enum(item[key])
        if state not in {"pass", "fail"}:
            _raise("INVALID_INPUT")
        result[key] = state
    for key in sorted(count_keys):
        result[key] = _integer(item[key])
    return result


def _project_case(value: object, *, current: bool) -> dict:
    item = _mapping(value)
    _exact_keys(
        item,
        required={
            "case_id",
            "category",
            "intent",
            "params",
            "status",
            "provider_used",
            "attempts",
            "row_count",
            "error_code",
            "latency_ms",
        }
        | (
            {
                "direct_provider", "evidence_kind", "capability",
                "protocol_evidence",
            }
            if current
            else set()
        ),
    )
    projected = _project_provider_result(
        {
            "status": item["status"],
            "provider_used": item["provider_used"],
            "attempts": item["attempts"],
            "row_count": item["row_count"],
            "error_code": item["error_code"],
            "latency_ms": item["latency_ms"],
        },
        include_origin=current,
    )
    projected.update(
        {
            "case_id": _enum(item["case_id"]),
            "category": _enum(item["category"]),
            "intent": _enum(item["intent"]),
            "params": _project_safe_params(item["params"]),
        }
    )
    result = {
        "case_id": projected["case_id"],
        "category": projected["category"],
        "intent": projected["intent"],
        "params": projected["params"],
        "status": projected["status"],
        "provider_used": projected["provider_used"],
        "attempts": projected["attempts"],
        "row_count": projected["row_count"],
        "error_code": projected["error_code"],
        "latency_ms": projected["latency_ms"],
    }
    if current:
        direct_provider = item["direct_provider"]
        if direct_provider is not None:
            direct_provider = _enum(direct_provider)
        result.update(
            direct_provider=direct_provider,
            evidence_kind=_enum(item["evidence_kind"]),
            capability=_enum(item["capability"]),
            protocol_evidence=_project_protocol(item["protocol_evidence"]),
        )
    return result


def _project_smoke(path: Path, expected_date: str, *, current: bool) -> dict:
    path = Path(path).expanduser().resolve()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        _raise("INPUT_UNAVAILABLE")
    if mode != 0o600:
        _raise("INVALID_RECEIPT_PERMISSIONS")
    value = _load_json(path)
    _reject_forbidden(value, code="FORBIDDEN_INPUT")
    required = {
        "schema_version",
        "live",
        "started_at",
        "completed_at",
        "summary",
        "cases",
    }
    if current:
        required.update(
            {"baseline", "source_status", "chain_status", "gate_status"}
        )
    _exact_keys(value, required=required)
    expected_schema = (
        CURRENT_SMOKE_SCHEMA_VERSION if current else LEGACY_SMOKE_SCHEMA_VERSION
    )
    if value["schema_version"] != expected_schema or value["live"] is not True:
        _raise("INVALID_SMOKE_RECEIPT")
    if current and value["baseline"] != CURRENT_SMOKE_BASELINE:
        _raise("INVALID_SMOKE_BASELINE")
    started_at = _iso(value["started_at"], "INVALID_SMOKE_RECEIPT")
    completed_at = _iso(value["completed_at"], "INVALID_SMOKE_RECEIPT")
    if completed_at[:10] != expected_date or started_at[:10] != expected_date:
        _raise("SMOKE_DATE_MISMATCH")
    raw_cases = value["cases"]
    expected_count = len(CURRENT_SMOKE_CASE_IDS) if current else LEGACY_SMOKE_CASE_COUNT
    if not isinstance(raw_cases, list) or len(raw_cases) != expected_count:
        _raise("INVALID_CASE_IDS" if current else "INVALID_CASE_COUNT")
    cases = [_project_case(item, current=current) for item in raw_cases]
    case_ids = [item["case_id"] for item in cases]
    if current:
        if tuple(case_ids) != CURRENT_SMOKE_CASE_IDS:
            _raise("INVALID_CASE_IDS")
        for spec, case in zip(CASE_SPECS, cases):
            if (
                case["category"] != spec.category
                or case["intent"] != spec.intent
                or case["params"] != spec.safe_params()
                or case["direct_provider"] != spec.direct_provider
                or case["evidence_kind"] != spec.evidence_kind
                or case["capability"] != spec.capability
            ):
                _raise("INVALID_CASE_SPEC")
            if spec.direct_provider is not None:
                _validate_direct_provider(
                    case,
                    spec.direct_provider,
                    allow_unattempted_provider_state=(
                        spec.allow_unattempted_provider_state
                    ),
                )
    elif len(set(case_ids)) != LEGACY_SMOKE_CASE_COUNT:
        _raise("INVALID_CASE_COUNT")
    summary = _mapping(value["summary"])
    _exact_keys(summary, required={"total", "status_counts"})
    if _integer(summary["total"]) != expected_count:
        _raise("INVALID_CASE_IDS" if current else "INVALID_CASE_COUNT")
    counts = dict(sorted(Counter(item["status"] for item in cases).items()))
    supplied_counts = _mapping(summary["status_counts"])
    normalized_counts = {
        _enum(key): _integer(item) for key, item in supplied_counts.items()
    }
    if normalized_counts != counts:
        _raise("INVALID_STATUS_COUNTS")
    projected = {
        "path": str(path),
        "sha256": _sha256(path),
        "file_mode": "0600",
        "schema_version": expected_schema,
        "live": True,
        "started_at": started_at,
        "completed_at": completed_at,
        "total_cases": expected_count,
        "status_counts": counts,
        "intent_status_counts": _intent_status_counts(cases),
        "cases": cases,
    }
    if current:
        projected["baseline"] = CURRENT_SMOKE_BASELINE
        supplied_sources = _mapping(value["source_status"], "INVALID_SMOKE_GATE")
        expected_source_keys = {
            "iwencai_openapi", "pywencai", "tdx", "wind", "pytdx"
        }
        _exact_keys(
            supplied_sources,
            required=expected_source_keys,
            code="INVALID_SMOKE_GATE",
        )
        source_status = {}
        for name in sorted(expected_source_keys):
            status = _enum(supplied_sources[name], "INVALID_SMOKE_GATE")
            if status not in {"pass", "fail"}:
                _raise("INVALID_SMOKE_GATE")
            source_status[name] = status
        chain_status = _enum(value["chain_status"], "INVALID_SMOKE_GATE")
        gate_status = _enum(value["gate_status"], "INVALID_SMOKE_GATE")
        if chain_status not in {"pass", "fail"} or gate_status not in {"pass", "fail"}:
            _raise("INVALID_SMOKE_GATE")
        expected_sources, expected_chain, expected_gate = _smoke_gate(cases)
        if (
            source_status != expected_sources
            or chain_status != expected_chain
            or gate_status != expected_gate
        ):
            _raise("INVALID_SMOKE_GATE")
        projected.update(
            source_status=source_status,
            chain_status=chain_status,
            gate_status=gate_status,
        )
    return projected


def _smoke_gate(cases: list[dict]) -> tuple[dict, str, str]:
    by_id = {case["case_id"]: case for case in cases}

    def passed(case_id: str, provider: str, *, protocol: bool = False) -> bool:
        case = by_id.get(case_id, {})
        valid = (
            case.get("status") == "success"
            and case.get("provider_used") == provider
            and case.get("row_count", 0) > 0
            and any(
                attempt.get("provider") == provider
                and attempt.get("status") == "success"
                and attempt.get("origin") == "live"
                for attempt in case.get("attempts", [])
            )
        )
        if not protocol:
            return valid
        evidence = case.get("protocol_evidence")
        return valid and isinstance(evidence, dict) and all(
            evidence.get(key) == "pass"
            for key in ("initialize", "tools_list", "schema", "read_only", "tool_call")
        ) and all(
            evidence.get(key, 0) >= 1
            for key in ("page_count", "session_count", "call_count")
        )

    source_status = {
        "iwencai_openapi": "pass" if passed("direct_openapi_screener", "iwencai_openapi") else "fail",
        "pywencai": "pass" if passed("direct_pywencai_screener", "pywencai") else "fail",
        "tdx": "pass" if all(
            passed(case_id, provider, protocol=True)
            for case_id, provider in (
                ("tdx_probe", "tdx_quotes"),
                ("tdx_screener_probe", "tdx_screener"),
                ("tdx_kline_probe", "tdx_kline"),
                ("tdx_report_probe", "tdx_report"),
                ("tdx_notice_probe", "tdx_notice"),
                ("tdx_news_probe", "tdx_news"),
            )
        ) else "fail",
        "wind": "pass" if all(
            passed(case_id, provider)
            for case_id, provider in (
                ("wind_probe", "wind_mcp"),
                ("wind_screener_probe", "wind_screener"),
                ("wind_filings_probe", "wind_documents"),
            )
        ) else "fail",
        "pytdx": "pass" if passed("explicit_structured_screener", "pytdx_screener") else "fail",
    }
    fallback = by_id.get("canonical_five_source_fallback", {})
    attempts = fallback.get("attempts", [])
    chain = "pass" if (
        fallback.get("status") == "degraded"
        and fallback.get("provider_used") == "pytdx_screener"
        and fallback.get("row_count", 0) > 0
        and [item.get("provider") for item in attempts]
        == ["iwencai_openapi", "pywencai", "tdx_screener", "wind_screener", "pytdx_screener"]
        and [item.get("status") for item in attempts]
        == ["auth_error", "provider_error", "auth_error", "empty", "success"]
        and [item.get("origin") for item in attempts]
        == ["injected", "injected", "injected", "injected", "live"]
    ) else "fail"
    gate = "pass" if chain == "pass" and all(
        value == "pass" for value in source_status.values()
    ) else "fail"
    return dict(sorted(source_status.items())), chain, gate


def _validate_direct_provider(
    case: dict,
    expected_provider: str,
    *,
    allow_unattempted_provider_state: bool,
) -> None:
    provider_used = case["provider_used"]
    attempts = case["attempts"]
    if provider_used not in {None, expected_provider}:
        _raise("INVALID_DIRECT_PROVIDER")
    if any(attempt["provider"] != expected_provider for attempt in attempts):
        _raise("INVALID_DIRECT_PROVIDER")
    terminal_attempt = {
        "success": "success",
        "degraded": "success",
        "empty": "empty",
    }.get(case["status"])
    if terminal_attempt is not None:
        if provider_used != expected_provider or not any(
            attempt["status"] == terminal_attempt for attempt in attempts
        ):
            _raise("INVALID_DIRECT_PROVIDER")
        return
    if provider_used is not None:
        _raise("INVALID_DIRECT_PROVIDER")
    if not attempts:
        if case["error_code"] == "TOTAL_TIMEOUT":
            return
        if (
            allow_unattempted_provider_state
            and case["status"] in _DIRECT_SHORT_CIRCUIT_STATES
        ):
            return
        _raise("INVALID_DIRECT_PROVIDER")


def _intent_status_counts(cases: list[dict]) -> dict:
    result: dict[str, Counter] = {}
    for case in cases:
        result.setdefault(case["intent"], Counter())[case["status"]] += 1
    return {
        intent: dict(sorted(counts.items())) for intent, counts in sorted(result.items())
    }


def _project_calendar(
    value: dict, expected_date: str, *, require_previous: bool = True
) -> dict:
    _reject_forbidden(value, code="FORBIDDEN_INPUT")
    _exact_keys(
        value,
        required={
            "schema_version",
            "date",
            "timezone",
            "weekday",
            "is_trading_day",
            "confirmed",
            "official_calendar",
        }
        | ({"previous_trading_date"} if require_previous else set()),
    )
    if value["schema_version"] != "1" or _date(value["date"]) != expected_date:
        _raise("INVALID_CALENDAR")
    if value["timezone"] != "Asia/Shanghai":
        _raise("INVALID_CALENDAR")
    if value["is_trading_day"] is not True or value["confirmed"] is not True:
        _raise("TRADING_DAY_UNCONFIRMED")
    expected_weekday = date_type.fromisoformat(expected_date).strftime("%A")
    if value["weekday"] != expected_weekday:
        _raise("INVALID_CALENDAR")
    official = _mapping(value["official_calendar"])
    _exact_keys(official, required={"exchange", "url", "basis"})
    if official["exchange"] != SSE_EXCHANGE:
        _raise("INVALID_CALENDAR")
    if not isinstance(official["url"], str) or not official["url"].startswith(
        SSE_CALENDAR_BASE_URL
    ):
        _raise("INVALID_CALENDAR")
    result = {
        "date": expected_date,
        "timezone": "Asia/Shanghai",
        "weekday": _short_text(value["weekday"], limit=16),
        "is_trading_day": True,
        "confirmed": True,
        "official_calendar": {
            "exchange": _short_text(official["exchange"], limit=64),
            "url": _short_text(official["url"], limit=512),
            "basis": _short_text(official["basis"], limit=256),
        },
    }
    if require_previous:
        previous = _date(value["previous_trading_date"], "INVALID_CALENDAR")
        if previous >= expected_date:
            _raise("INVALID_CALENDAR")
        result["previous_trading_date"] = previous
    return result


def _pending_provider_result() -> dict:
    return {
        "status": PENDING_STATUS,
        "provider_used": None,
        "attempts": [],
    }


def acceptance_template(date: str) -> dict:
    """Return fail-closed calendar/downstream inputs without I/O."""

    observed_date = _date(date)
    mutation_gates = _SAFETY_KEYS - {"metadata_only", "zero_secret_scan"}
    safety = {key: False for key in sorted(mutation_gates)}
    safety.update({"metadata_only": True, "zero_secret_scan": PENDING_STATUS})
    return {
        "calendar": {
            "schema_version": "1",
            "date": observed_date,
            "timezone": "Asia/Shanghai",
            "weekday": date_type.fromisoformat(observed_date).strftime("%A"),
            "is_trading_day": False,
            "confirmed": False,
            "previous_trading_date": PENDING_STATUS,
            "official_calendar": {
                "exchange": SSE_EXCHANGE,
                "url": SSE_CALENDAR_BASE_URL,
                "basis": PENDING_STATUS,
            },
        },
        "downstream": {
            "schema_version": "1",
            "breaker_verification": {
                **_pending_provider_result(),
                "row_count": 0,
                "error_code": None,
                "latency_ms": 0,
            },
            "market_watch": {
                **_pending_provider_result(),
                "quality_status": PENDING_STATUS,
                "returned_count": 0,
                "observation_only": True,
            },
            "live_dashboard": {
                **_pending_provider_result(),
                "row_count": 0,
                "api_mode_tested": "unified",
                "default_api_mode": "legacy",
                "comparison_status": PENDING_STATUS,
                "saved": False,
            },
            "safety": safety,
        },
        "template_meta": {
            "smoke_schema_version": CURRENT_SMOKE_SCHEMA_VERSION,
            "smoke_baseline": CURRENT_SMOKE_BASELINE,
            "smoke_case_ids": list(CURRENT_SMOKE_CASE_IDS),
            "pending_sentinel": PENDING_STATUS,
            "allowed_result_statuses": sorted(_CASE_STATUSES),
            "allowed_attempt_statuses": sorted(_ATTEMPT_STATUSES),
            "required_replacements": [
                "calendar.is_trading_day",
                "calendar.confirmed",
                "calendar.official_calendar.basis",
                "calendar.previous_trading_date",
                "downstream.breaker_verification.status",
                "downstream.market_watch.status",
                "downstream.market_watch.quality_status",
                "downstream.live_dashboard.status",
                "downstream.live_dashboard.comparison_status",
                "downstream.safety.zero_secret_scan",
            ],
            "safety_gate": {
                "must_remain_false": sorted(mutation_gates),
                "metadata_only_required": True,
                "zero_secret_scan_required": "pass",
            },
        },
    }


def _project_safety(value: object, *, legacy: bool = False) -> dict:
    safety = _mapping(value)
    _exact_keys(
        safety,
        required=_SAFETY_KEYS,
        optional={"smoke_rerun"} if legacy else frozenset(),
    )
    if legacy and "smoke_rerun" in safety and safety["smoke_rerun"] is not False:
        _raise("INVALID_SAFETY_FLAGS")
    projected = {key: safety[key] for key in sorted(_SAFETY_KEYS)}
    if projected["metadata_only"] is not True or projected["zero_secret_scan"] != "pass":
        _raise("INVALID_SAFETY_FLAGS")
    for key in _SAFETY_KEYS - {"metadata_only", "zero_secret_scan"}:
        if projected[key] is not False:
            _raise("INVALID_SAFETY_FLAGS")
    return projected


def _project_downstream(value: dict) -> dict:
    _reject_forbidden(value, code="FORBIDDEN_INPUT")
    _exact_keys(
        value,
        required={
            "schema_version",
            "breaker_verification",
            "market_watch",
            "live_dashboard",
            "safety",
        },
    )
    if value["schema_version"] != "1":
        _raise("INVALID_INPUT")
    breaker = _project_provider_result(value["breaker_verification"])

    market = _mapping(value["market_watch"])
    _exact_keys(
        market,
        required={
            "status",
            "provider_used",
            "attempts",
            "quality_status",
            "returned_count",
            "observation_only",
        },
    )
    market_base = _project_provider_result(
        {
            "status": market["status"],
            "provider_used": market["provider_used"],
            "attempts": market["attempts"],
        }
    )
    market_base.update(
        {
            "quality_status": _enum(market["quality_status"]),
            "returned_count": _integer(market["returned_count"]),
            "observation_only": _boolean(market["observation_only"]),
        }
    )
    if market_base["observation_only"] is not True:
        _raise("INVALID_SAFETY_FLAGS")

    dashboard = _mapping(value["live_dashboard"])
    _exact_keys(
        dashboard,
        required={
            "status",
            "provider_used",
            "attempts",
            "row_count",
            "api_mode_tested",
            "default_api_mode",
            "comparison_status",
            "saved",
        },
    )
    dashboard_base = _project_provider_result(
        {
            "status": dashboard["status"],
            "provider_used": dashboard["provider_used"],
            "attempts": dashboard["attempts"],
            "row_count": dashboard["row_count"],
        }
    )
    dashboard_base.update(
        {
            "api_mode_tested": _enum(dashboard["api_mode_tested"]),
            "default_api_mode": _enum(dashboard["default_api_mode"]),
            "comparison_status": _enum(dashboard["comparison_status"]),
            "saved": _boolean(dashboard["saved"]),
        }
    )
    if dashboard_base["saved"] is not False:
        _raise("INVALID_SAFETY_FLAGS")
    expected_comparison = {
        "legacy": "exact_code_set_match",
        "unified": "unified_default_observed",
    }
    default_mode = dashboard_base["default_api_mode"]
    if (
        dashboard_base["api_mode_tested"] != "unified"
        or default_mode not in expected_comparison
        or dashboard_base["comparison_status"] != expected_comparison[default_mode]
    ):
        _raise("INVALID_INPUT")
    return {
        "breaker_verification": breaker,
        "market_watch": market_base,
        "live_dashboard": dashboard_base,
        "safety": _project_safety(value["safety"]),
    }


def _run_git(repo_root: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        _raise("GIT_UNAVAILABLE")
    if completed.returncode != 0:
        _raise("GIT_UNAVAILABLE")
    return completed.stdout.strip()


def _git_is_ignored(repo_root: Path, relative: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-q", "--", relative],
            check=False,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        _raise("GIT_UNAVAILABLE")
    return completed.returncode == 0


def _git_blob_sha256(repo_root: Path, revision: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "show", revision],
            check=False,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        _raise("GIT_UNAVAILABLE")
    if completed.returncode != 0:
        _raise("GIT_UNAVAILABLE")
    return hashlib.sha256(completed.stdout).hexdigest()


def _protected_snapshot(repo_root: Path) -> list[dict]:
    protected = []
    for relative in _PROTECTED_ARTIFACTS:
        path = repo_root / relative
        if not path.is_file():
            _raise("PROTECTED_ARTIFACT_MISSING")
        if not _git_is_ignored(repo_root, relative):
            _raise("PROTECTED_ARTIFACT_NOT_IGNORED")
        protected.append(
            {
                "path": relative,
                "exists": True,
                "git_status": "ignored",
                "sha256": _sha256(path),
            }
        )
    return protected


def _git_snapshot(repo_root: Path) -> dict:
    repo_root = Path(repo_root).resolve()
    branch = _run_git(repo_root, ["branch", "--show-current"])
    head = _run_git(repo_root, ["rev-parse", "HEAD"])
    if not branch or not _GIT_HEAD.fullmatch(head):
        _raise("GIT_UNAVAILABLE")
    tracked = _run_git(repo_root, ["status", "--porcelain=v1", "--untracked-files=no"])
    staged = _run_git(repo_root, ["diff", "--cached", "--name-only"])
    if tracked or staged:
        _raise("GIT_NOT_CLEAN")
    tree = _run_git(repo_root, ["rev-parse", f"{head}:ym_stock_data"])
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        _raise("GIT_UNAVAILABLE")
    launcher = repo_root / "ym-data"
    if not launcher.is_file():
        _raise("GIT_UNAVAILABLE")
    launcher_sha256 = _git_blob_sha256(repo_root, f"{head}:ym-data")
    if _sha256(launcher) != launcher_sha256:
        _raise("GIT_NOT_CLEAN")
    return {
        "path": str(repo_root),
        "branch": branch,
        "head": head,
        "tracked_clean": True,
        "staged_clean": True,
        "ym_stock_data_tree": tree,
        "launcher": {"path": str(launcher), "sha256": launcher_sha256},
        "ignored_artifacts": _protected_snapshot(repo_root),
    }


def _latency(cases: list[dict]) -> dict:
    values = sorted(case["latency_ms"] for case in cases)

    def percentile(value: float) -> int:
        return values[max(0, math.ceil(value * len(values)) - 1)]

    return {
        "method": "nearest-rank",
        "algorithm": "sort ascending; rank=ceil(percentile*n), one-based",
        "sample_size": len(values),
        "unit": "ms",
        "sorted_case_latencies": values,
        "p50": percentile(0.50),
        "p95": percentile(0.95),
    }


def _case(cases: list[dict], case_id: str) -> dict:
    return next((item for item in cases if item["case_id"] == case_id), {})


def _provider_acceptance(
    doctor: dict,
    smoke: dict,
    downstream: dict,
    *,
    include_pytdx_screener: bool = True,
) -> dict:
    cases = smoke["cases"]
    all_smoke_attempts = [
        attempt for case in cases for attempt in case["attempts"]
    ]
    smoke_attempts = (
        [
            attempt
            for attempt in all_smoke_attempts
            if attempt.get("origin") == "live"
        ]
        if include_pytdx_screener
        else all_smoke_attempts
    )
    breaker_attempts = downstream["breaker_verification"]["attempts"]
    http_401_count = sum(
        1
        for attempt in smoke_attempts
        if attempt["provider"] == "iwencai_openapi"
        and attempt["status"] == "auth_error"
        and attempt["error_code"] == "HTTP_401"
    )
    breaker_prevented = any(
        attempt["provider"] == "iwencai_openapi"
        and attempt["status"] == "breaker_open"
        and attempt["error_code"] == "HTTP_401"
        for attempt in breaker_attempts
    )
    pywencai = [attempt for attempt in smoke_attempts if attempt["provider"] == "pywencai"]
    pywencai_successes = sum(attempt["status"] == "success" for attempt in pywencai)
    providers = doctor["providers"]
    tdx_case = _case(cases, "tdx_probe")
    wind_case = _case(cases, "wind_probe")
    wind_state = providers.get("wind_mcp", {})
    result = {
        "iwencai_openapi": {
            "http_401_count": http_401_count,
            "breaker_prevented_repeat": breaker_prevented,
            "breaker_verification": downstream["breaker_verification"],
        },
        "pywencai": {
            "attempts": len(pywencai),
            "successes": pywencai_successes,
            "success_rate": f"{pywencai_successes}/{len(pywencai)}",
            "error_codes": sorted(
                {attempt["error_code"] for attempt in pywencai if attempt["error_code"]}
            ),
            "doctor_status": providers.get("pywencai", {}).get("status", "unavailable"),
        },
        "tdx": {
            "doctor_status": providers.get("tdx_mcp", {}).get("status", "unavailable"),
            "capability_statuses": {
                name: item["status"]
                for name, item in providers.items()
                if name.startswith("tdx_") and name != "tdx_mcp"
            },
            "smoke_probe_status": tdx_case.get("status", "unavailable"),
            "smoke_probe_row_count": tdx_case.get("row_count", 0),
        },
        "wind": {
            "doctor_status": wind_state.get("status", "unavailable"),
            "auth": wind_state.get("auth", {"required": True, "status": "unverified"}),
            "runtime_scope": wind_state.get("runtime_scope"),
            "live_status": wind_case.get("status", "unavailable"),
            "provider_used": wind_case.get("provider_used"),
            "row_count": wind_case.get("row_count", 0),
            "latency_ms": wind_case.get("latency_ms", 0),
        },
    }
    if include_pytdx_screener:
        pytdx_case = _case(cases, "explicit_structured_screener")
        pytdx_state = providers.get("pytdx_screener", {})
        result["pytdx_screener"] = {
            "doctor_status": pytdx_state.get("status", "unavailable"),
            "auth": pytdx_state.get(
                "auth", {"required": False, "status": "not_required"}
            ),
            "live_status": pytdx_case.get("status", "unavailable"),
            "provider_used": pytdx_case.get("provider_used"),
            "row_count": pytdx_case.get("row_count", 0),
            "latency_ms": pytdx_case.get("latency_ms", 0),
            "error_code": pytdx_case.get("error_code"),
        }
        controlled = _case(cases, "canonical_five_source_fallback")
        controlled_attempts = controlled.get("attempts", [])
        result["controlled_fallback"] = {
            "case_status": controlled.get("status", "unavailable"),
            "provider_used": controlled.get("provider_used"),
            "chain_status": smoke.get("chain_status", "fail"),
            "injected_attempts": [
                attempt
                for attempt in controlled_attempts
                if attempt.get("origin") == "injected"
            ],
            "live_attempts": [
                attempt
                for attempt in controlled_attempts
                if attempt.get("origin") == "live"
            ],
        }
    return result


def _report_integrity(report: dict) -> dict:
    unsigned = {key: value for key, value in report.items() if key != "integrity"}
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {"algorithm": "sha256", "digest": hashlib.sha256(payload).hexdigest()}


def _acceptance_mode(path: Path) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        directory_mode = stat.S_IMODE(path.parent.stat().st_mode)
    except OSError:
        _raise("INPUT_UNAVAILABLE")
    if mode != 0o600 or directory_mode != 0o700:
        _raise("INVALID_PERMISSIONS")


def _validate_unpublished_v12(report: dict, path: Path) -> None:
    """Validate a known immutable 1.2 receipt before excluding it from history."""

    _reject_forbidden(report, code="FORBIDDEN_FIELD")
    _exact_keys(
        report,
        required={
            "schema",
            "schema_version",
            "generated_at",
            "observation",
            "canonical_checkout",
            "doctor",
            "smoke_evidence",
            "provider_acceptance",
            "latency",
            "downstream_checks",
            "safety",
            "integrity",
        },
        code="INVALID_ACCEPTANCE",
    )
    if (
        report["schema"] != SCHEMA
        or report["schema_version"] != UNPUBLISHED_SCHEMA_VERSION
    ):
        _raise("INVALID_SCHEMA")
    integrity = _mapping(report["integrity"], "INVALID_ACCEPTANCE")
    _exact_keys(
        integrity,
        required={"algorithm", "digest"},
        code="INVALID_ACCEPTANCE",
    )
    if integrity != _report_integrity(report):
        _raise("INTEGRITY_MISMATCH")

    generated_at = _iso(report["generated_at"], "INVALID_ACCEPTANCE")
    observation = _mapping(report["observation"], "INVALID_ACCEPTANCE")
    required_observation = {
        "date",
        "timezone",
        "weekday",
        "is_trading_day",
        "confirmed",
        "official_calendar",
        "day_count",
        "required_trading_days",
        "window_complete",
    }
    if not required_observation.issubset(observation):
        _raise("INVALID_ACCEPTANCE")
    observed_date = _date(observation["date"], "INVALID_ACCEPTANCE")
    if (
        observation["timezone"] != "Asia/Shanghai"
        or observation["is_trading_day"] is not True
        or _integer(observation["day_count"], "INVALID_ACCEPTANCE") < 1
        or observation["required_trading_days"] != REQUIRED_TRADING_DAYS
    ):
        _raise("INVALID_ACCEPTANCE")
    day_count = observation["day_count"]
    if observation["window_complete"] is not (day_count >= REQUIRED_TRADING_DAYS):
        _raise("INVALID_ACCEPTANCE")
    generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    if generated.astimezone(TZ_SHANGHAI).date().isoformat() != observed_date:
        _raise("INVALID_ACCEPTANCE")

    checkout = _mapping(report["canonical_checkout"], "INVALID_ACCEPTANCE")
    if (
        not isinstance(checkout.get("branch"), str)
        or not checkout["branch"]
        or not _GIT_HEAD.fullmatch(str(checkout.get("head", "")))
        or checkout.get("tracked_clean") is not True
        or checkout.get("staged_clean") is not True
    ):
        _raise("INVALID_ACCEPTANCE")
    doctor = _mapping(report["doctor"], "INVALID_ACCEPTANCE")
    _exact_keys(
        doctor,
        required={
            "command",
            "schema_version",
            "run_count_for_acceptance",
            "providers",
            "summary",
        },
        code="INVALID_ACCEPTANCE",
    )
    provider_acceptance = _mapping(
        report["provider_acceptance"], "INVALID_ACCEPTANCE"
    )
    _exact_keys(
        provider_acceptance,
        required={
            "iwencai_openapi",
            "pywencai",
            "tdx",
            "wind",
            "pytdx_screener",
        },
        code="INVALID_ACCEPTANCE",
    )
    latency = _mapping(report["latency"], "INVALID_ACCEPTANCE")
    _exact_keys(
        latency,
        required={
            "method",
            "algorithm",
            "sample_size",
            "unit",
            "sorted_case_latencies",
            "p50",
            "p95",
        },
        code="INVALID_ACCEPTANCE",
    )
    downstream = _mapping(report["downstream_checks"], "INVALID_ACCEPTANCE")
    _exact_keys(
        downstream,
        required={"market_watch", "live_dashboard", "breaker_verification"},
        code="INVALID_ACCEPTANCE",
    )
    safety = _project_safety(report["safety"])
    if report["safety"] != safety:
        _raise("INVALID_ACCEPTANCE")

    smoke = _mapping(report["smoke_evidence"], "INVALID_ACCEPTANCE")
    required_smoke = {"baseline", "path", "sha256", "total_cases"}
    if not required_smoke.issubset(smoke):
        _raise("INVALID_ACCEPTANCE")
    if (
        smoke["baseline"] != UNPUBLISHED_SMOKE_BASELINE
        or smoke["total_cases"] != 11
        or not isinstance(smoke["sha256"], str)
        or not _SHA256.fullmatch(smoke["sha256"])
    ):
        _raise("INVALID_ACCEPTANCE")
    smoke_path = Path(str(smoke["path"])).expanduser().resolve()
    try:
        smoke_mode = stat.S_IMODE(smoke_path.stat().st_mode)
    except OSError:
        _raise("INPUT_UNAVAILABLE")
    if smoke_mode != 0o600:
        _raise("INVALID_RECEIPT_PERMISSIONS")
    if _sha256(smoke_path) != smoke["sha256"]:
        _raise("RECEIPT_HASH_MISMATCH")
    smoke_report = _load_json(smoke_path)
    _reject_forbidden(smoke_report, code="FORBIDDEN_FIELD")
    if (
        smoke_report.get("schema_version") != "2"
        or smoke_report.get("baseline") != UNPUBLISHED_SMOKE_BASELINE
        or smoke_report.get("live") is not True
    ):
        _raise("INVALID_SMOKE_RECEIPT")
    smoke_started = _iso(
        smoke_report.get("started_at"), "INVALID_SMOKE_RECEIPT"
    )
    smoke_completed = _iso(
        smoke_report.get("completed_at"), "INVALID_SMOKE_RECEIPT"
    )
    if smoke_started[:10] != observed_date or smoke_completed[:10] != observed_date:
        _raise("SMOKE_DATE_MISMATCH")
    smoke_summary = _mapping(
        smoke_report.get("summary"), "INVALID_SMOKE_RECEIPT"
    )
    smoke_cases = smoke_report.get("cases")
    if (
        smoke_summary.get("total") != 11
        or not isinstance(smoke_cases, list)
        or len(smoke_cases) != 11
    ):
        _raise("INVALID_CASE_COUNT")

    path = Path(path)
    _acceptance_mode(path)
    if path.name != f"{observed_date}.json":
        _raise("INVALID_DATE")


def _validate_head_binding(checkout: dict) -> None:
    repo_root = checkout.get("path")
    head = checkout.get("head")
    if not isinstance(repo_root, str) or not Path(repo_root).is_dir():
        _raise("INVALID_HEAD_BINDING")
    try:
        completed = subprocess.run(
            ["git", "-C", repo_root, "cat-file", "-e", f"{head}^{{commit}}"],
            check=False,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        _raise("INVALID_HEAD_BINDING")
    if completed.returncode != 0:
        _raise("INVALID_HEAD_BINDING")


def _validate_checkout(value: object) -> dict:
    checkout = _mapping(value, "INVALID_ACCEPTANCE")
    _exact_keys(
        checkout,
        required={
            "path",
            "branch",
            "head",
            "tracked_clean",
            "staged_clean",
            "ym_stock_data_tree",
            "launcher",
            "ignored_artifacts",
        },
        code="INVALID_ACCEPTANCE",
    )
    if not isinstance(checkout["branch"], str) or not checkout["branch"]:
        _raise("INVALID_ACCEPTANCE")
    if not _GIT_HEAD.fullmatch(str(checkout["head"])):
        _raise("INVALID_ACCEPTANCE")
    if checkout["tracked_clean"] is not True or checkout["staged_clean"] is not True:
        _raise("INVALID_ACCEPTANCE")
    repo_root = Path(str(checkout["path"])).expanduser().resolve()
    _validate_head_binding(checkout)
    expected_tree = _run_git(
        repo_root, ["rev-parse", f"{checkout['head']}:ym_stock_data"]
    )
    if checkout["ym_stock_data_tree"] != expected_tree:
        _raise("INVALID_HEAD_BINDING")
    launcher = _mapping(checkout["launcher"], "INVALID_ACCEPTANCE")
    _exact_keys(
        launcher,
        required={"path", "sha256"},
        code="INVALID_ACCEPTANCE",
    )
    expected_launcher_path = str(repo_root / "ym-data")
    expected_launcher_sha = _git_blob_sha256(
        repo_root, f"{checkout['head']}:ym-data"
    )
    if (
        launcher["path"] != expected_launcher_path
        or launcher["sha256"] != expected_launcher_sha
    ):
        _raise("INVALID_HEAD_BINDING")
    ignored = checkout["ignored_artifacts"]
    if not isinstance(ignored, list) or ignored != _protected_snapshot(repo_root):
        _raise("INVALID_PROTECTED_ARTIFACTS")
    return checkout


def _validate_projected_doctor(value: object) -> dict:
    doctor = _mapping(value, "INVALID_ACCEPTANCE")
    _exact_keys(
        doctor,
        required={"command", "schema_version", "run_count_for_acceptance", "providers", "summary"},
        code="INVALID_ACCEPTANCE",
    )
    if doctor["command"] != "./ym-data doctor --json" or doctor["run_count_for_acceptance"] != 1:
        _raise("INVALID_ACCEPTANCE")
    providers = _mapping(doctor["providers"], "INVALID_ACCEPTANCE")
    source_providers = {}
    for name, item in providers.items():
        projected_item = _mapping(item, "INVALID_ACCEPTANCE")
        source_providers[name] = {"provider": name, **projected_item}
    projected = _project_doctor(
        {
            "schema_version": doctor["schema_version"],
            "providers": source_providers,
            "summary": doctor["summary"],
        }
    )
    if doctor != projected:
        _raise("INVALID_ACCEPTANCE")
    return projected


def _validate_downstream(value: object, safety: dict) -> dict:
    downstream = _mapping(value, "INVALID_ACCEPTANCE")
    _exact_keys(
        downstream,
        required={"market_watch", "live_dashboard", "breaker_verification"},
        code="INVALID_ACCEPTANCE",
    )
    projected = _project_downstream(
        {
            "schema_version": "1",
            "breaker_verification": downstream["breaker_verification"],
            "market_watch": downstream["market_watch"],
            "live_dashboard": downstream["live_dashboard"],
            "safety": safety,
        }
    )
    expected = {
        "market_watch": projected["market_watch"],
        "live_dashboard": projected["live_dashboard"],
        "breaker_verification": projected["breaker_verification"],
    }
    if downstream != expected:
        _raise("INVALID_ACCEPTANCE")
    return projected


def _validate_v11(
    report: dict,
    observed_date: str,
    smoke: dict,
    *,
    include_pytdx_screener: bool,
) -> None:
    current = report.get("schema_version") == SCHEMA_VERSION
    _exact_keys(
        report,
        required={
            "schema",
            "schema_version",
            "generated_at",
            "observation",
            "canonical_checkout",
            "doctor",
            "smoke_evidence",
            "provider_acceptance",
            "latency",
            "downstream_checks",
            "safety",
            "integrity",
        },
        code="INVALID_ACCEPTANCE",
    )
    integrity = _mapping(report["integrity"], "INVALID_ACCEPTANCE")
    _exact_keys(
        integrity,
        required={"algorithm", "digest"},
        code="INVALID_ACCEPTANCE",
    )
    if integrity != _report_integrity(report):
        _raise("INTEGRITY_MISMATCH")
    observation = _mapping(report["observation"], "INVALID_ACCEPTANCE")
    calendar_keys = {
            "date",
            "timezone",
            "weekday",
            "is_trading_day",
            "confirmed",
            "official_calendar",
    }
    _exact_keys(
        observation,
        required=calendar_keys
        | (
            {
                "previous_trading_date",
                "observation_day_count",
                "pass_day_count",
                "required_trading_days",
                "window_complete",
                "gate_status",
                "epoch_start_date",
                "epoch_status",
            }
            if current
            else {
            "day_count",
            "required_trading_days",
            "window_complete",
            }
        ),
        code="INVALID_ACCEPTANCE",
    )
    projected_calendar = _project_calendar(
        {
            "schema_version": "1",
            "date": observation["date"],
            "timezone": observation["timezone"],
            "weekday": observation["weekday"],
            "is_trading_day": observation["is_trading_day"],
            "confirmed": observation["confirmed"],
            "official_calendar": observation["official_calendar"],
            **(
                {"previous_trading_date": observation["previous_trading_date"]}
                if current
                else {}
            ),
        },
        observed_date,
        require_previous=current,
    )
    if {key: observation[key] for key in projected_calendar} != projected_calendar:
        _raise("INVALID_ACCEPTANCE")
    if current:
        observation_count = _integer(
            observation["observation_day_count"], "INVALID_ACCEPTANCE"
        )
        pass_count = _integer(observation["pass_day_count"], "INVALID_ACCEPTANCE")
        if observation_count < 1 or pass_count < 1 or pass_count > observation_count:
            _raise("INVALID_ACCEPTANCE")
        if observation["required_trading_days"] != REQUIRED_TRADING_DAYS:
            _raise("INVALID_ACCEPTANCE")
        if observation["window_complete"] is not (pass_count >= REQUIRED_TRADING_DAYS):
            _raise("INVALID_ACCEPTANCE")
        if observation["gate_status"] != "pass":
            _raise("SMOKE_GATE_FAILED")
        epoch_start = _date(observation["epoch_start_date"], "INVALID_ACCEPTANCE")
        if epoch_start > observed_date:
            _raise("INVALID_ACCEPTANCE")
        expected_epoch_status = "complete" if pass_count >= REQUIRED_TRADING_DAYS else "open"
        if observation["epoch_status"] != expected_epoch_status:
            _raise("INVALID_ACCEPTANCE")
    generated = datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00"))
    generated_local = generated.astimezone(TZ_SHANGHAI)
    if generated_local.date().isoformat() != observed_date or generated_local.time() < EARLIEST_ACCEPTANCE_TIME:
        _raise("INVALID_ACCEPTANCE")
    _validate_checkout(report["canonical_checkout"])
    doctor = _validate_projected_doctor(report["doctor"])
    if report["smoke_evidence"] != smoke:
        _raise("RECEIPT_CONTENT_MISMATCH")
    safety = _project_safety(report["safety"])
    if report["safety"] != safety:
        _raise("INVALID_ACCEPTANCE")
    downstream = _validate_downstream(report["downstream_checks"], safety)
    if report["provider_acceptance"] != _provider_acceptance(
        doctor,
        smoke,
        downstream,
        include_pytdx_screener=include_pytdx_screener,
    ):
        _raise("INVALID_ACCEPTANCE")
    if report["latency"] != _latency(smoke["cases"]):
        _raise("INVALID_ACCEPTANCE")


def _validate_legacy_cases(value: object) -> None:
    if not isinstance(value, list) or len(value) != 10:
        _raise("INVALID_CASE_COUNT")
    for raw in value:
        item = _mapping(raw, "INVALID_ACCEPTANCE")
        for required in ("intent", "status", "provider_used", "attempts", "row_count", "latency_ms"):
            if required not in item:
                _raise("INVALID_ACCEPTANCE")
        _enum(item["intent"], "INVALID_ACCEPTANCE")
        _enum(item["status"], "INVALID_ACCEPTANCE")
        if item["provider_used"] is not None:
            _enum(item["provider_used"], "INVALID_ACCEPTANCE")
        _project_attempts(item["attempts"], "INVALID_ACCEPTANCE")
        _integer(item["row_count"], "INVALID_ACCEPTANCE")
        _integer(item["latency_ms"], "INVALID_ACCEPTANCE")


def _validate_report(report: dict, path: Path | None = None) -> dict:
    _reject_forbidden(report, code="FORBIDDEN_FIELD")
    if report.get("schema") != SCHEMA:
        _raise("INVALID_SCHEMA")
    version = report.get("schema_version")
    if version not in {
        SCHEMA_VERSION,
        PREVIOUS_SCHEMA_VERSION,
        LEGACY_SCHEMA_VERSION,
    }:
        _raise("INVALID_SCHEMA")
    for key in ("generated_at", "observation", "canonical_checkout", "smoke_evidence", "safety"):
        if key not in report:
            _raise("INVALID_ACCEPTANCE")
    _iso(report["generated_at"], "INVALID_ACCEPTANCE")
    observation = _mapping(report["observation"], "INVALID_ACCEPTANCE")
    observed_date = _date(observation.get("date"), "INVALID_ACCEPTANCE")
    if observation.get("timezone") != "Asia/Shanghai" or observation.get("is_trading_day") is not True:
        _raise("INVALID_ACCEPTANCE")
    current = version == SCHEMA_VERSION
    if current:
        observation_day_count = _integer(
            observation.get("observation_day_count"), "INVALID_ACCEPTANCE"
        )
        pass_day_count = _integer(
            observation.get("pass_day_count"), "INVALID_ACCEPTANCE"
        )
        day_count = pass_day_count
        if (
            observation_day_count < 1
            or pass_day_count < 1
            or pass_day_count > observation_day_count
        ):
            _raise("INVALID_ACCEPTANCE")
    else:
        day_count = _integer(observation.get("day_count"), "INVALID_ACCEPTANCE")
        observation_day_count = day_count
        pass_day_count = day_count
    if day_count < 1 or observation.get("required_trading_days") != REQUIRED_TRADING_DAYS:
        _raise("INVALID_ACCEPTANCE")
    if observation.get("window_complete") is not (pass_day_count >= REQUIRED_TRADING_DAYS):
        _raise("INVALID_ACCEPTANCE")
    checkout = _mapping(report["canonical_checkout"], "INVALID_ACCEPTANCE")
    if not isinstance(checkout.get("branch"), str) or not _GIT_HEAD.fullmatch(str(checkout.get("head", ""))):
        _raise("INVALID_ACCEPTANCE")
    if checkout.get("tracked_clean") is not True or checkout.get("staged_clean") is not True:
        _raise("INVALID_ACCEPTANCE")
    if version in {SCHEMA_VERSION, PREVIOUS_SCHEMA_VERSION}:
        _validate_head_binding(checkout)
    smoke = _mapping(report["smoke_evidence"], "INVALID_ACCEPTANCE")
    smoke_path = Path(str(smoke.get("path", ""))).expanduser()
    recorded_hash = smoke.get("sha256")
    if not isinstance(recorded_hash, str) or not _SHA256.fullmatch(recorded_hash):
        _raise("INVALID_ACCEPTANCE")
    if _sha256(smoke_path) != recorded_hash:
        _raise("RECEIPT_HASH_MISMATCH")
    projected_smoke = _project_smoke(smoke_path, observed_date, current=current)
    expected_count = len(CURRENT_SMOKE_CASE_IDS) if current else LEGACY_SMOKE_CASE_COUNT
    if smoke.get("total_cases") != expected_count:
        _raise("INVALID_CASE_COUNT")
    if version in {SCHEMA_VERSION, PREVIOUS_SCHEMA_VERSION}:
        _validate_v11(
            report,
            observed_date,
            projected_smoke,
            include_pytdx_screener=current,
        )
    else:
        _validate_legacy_cases(smoke.get("cases"))
    _project_safety(report["safety"], legacy=version == LEGACY_SCHEMA_VERSION)
    if path is not None:
        path = Path(path)
        _acceptance_mode(path)
        if path.name != f"{observed_date}.json":
            _raise("INVALID_DATE")
    return {
        "status": "valid",
        "path": str(path) if path is not None else None,
        "date": observed_date,
        "day_count": day_count,
        "observation_day_count": observation_day_count,
        "pass_day_count": pass_day_count,
        "previous_trading_date": observation.get("previous_trading_date"),
        "epoch_start_date": observation.get("epoch_start_date"),
        "head": checkout.get("head"),
        "schema_version": version,
    }


def _history(directory: Path) -> list[dict]:
    if not directory.exists():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        report = _load_json(path)
        if report.get("schema_version") == UNPUBLISHED_SCHEMA_VERSION:
            _validate_unpublished_v12(report, path)
            continue
        summary = _validate_report(report, path)
        records.append({**summary, "path": path})
    dates = [item["date"] for item in records]
    if len(set(dates)) != len(dates):
        _raise("DUPLICATE_DATE")
    ordered = sorted(records, key=lambda item: item["date"])
    current_count = 0
    legacy_count = 0
    previous_current = None
    for item in ordered:
        if item["schema_version"] != SCHEMA_VERSION:
            legacy_count += 1
            if item["day_count"] != legacy_count:
                _raise("INVALID_DAY_SEQUENCE")
            continue
        current_count += 1
        if item["observation_day_count"] != current_count:
            _raise("INVALID_DAY_SEQUENCE")
        continues = (
            previous_current is not None
            and item["previous_trading_date"] == previous_current["date"]
            and item["head"] == previous_current["head"]
        )
        expected_pass = previous_current["pass_day_count"] + 1 if continues else 1
        expected_epoch = (
            previous_current["epoch_start_date"] if continues else item["date"]
        )
        if (
            item["pass_day_count"] != expected_pass
            or item["epoch_start_date"] != expected_epoch
        ):
            _raise("INVALID_DAY_SEQUENCE")
        previous_current = item
    return ordered


def _atomic_write(report: dict, output_dir: Path, date: str) -> Path:
    output_dir = Path(output_dir).expanduser()
    try:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(output_dir, 0o700)
    except OSError:
        _raise("WRITE_FAILED")
    destination = output_dir / f"{date}.json"
    if destination.exists():
        _raise("TARGET_EXISTS")
    temporary = output_dir / f".{date}.{os.getpid()}.tmp"
    payload = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    reserved = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        reserve_descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        os.close(reserve_descriptor)
        reserved = True
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        directory_descriptor = os.open(output_dir, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except FileExistsError:
        _raise("TARGET_EXISTS")
    except OSError:
        _raise("WRITE_FAILED")
    finally:
        if temporary.exists():
            temporary.unlink()
        if reserved and destination.exists() and destination.stat().st_size == 0:
            destination.unlink()
    return destination


def build_daily_acceptance(
    *,
    date: str,
    doctor_path: Path,
    smoke_path: Path,
    downstream_path: Path,
    calendar_path: Path,
    output_dir: Path = ACCEPTANCE_DIR,
    repo_root: Path | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(TZ_SHANGHAI),
) -> dict:
    """Build one offline daily receipt from existing sanitized inputs."""

    observed_date = _date(date)
    output_dir = Path(output_dir).expanduser()
    destination = output_dir / f"{observed_date}.json"
    if destination.exists():
        _raise("TARGET_EXISTS")
    doctor_input = _load_json(Path(doctor_path))
    downstream_input = _load_json(Path(downstream_path))
    calendar_input = _load_json(Path(calendar_path))
    for value in (doctor_input, downstream_input, calendar_input):
        _reject_forbidden(value, code="FORBIDDEN_INPUT")
    calendar = _project_calendar(calendar_input, observed_date)
    now = now_fn()
    if not isinstance(now, datetime) or now.tzinfo is None:
        _raise("INVALID_DATE")
    local_now = now.astimezone(TZ_SHANGHAI)
    if local_now.date().isoformat() != observed_date:
        _raise("OBSERVATION_DATE_MISMATCH")
    if local_now.time() < EARLIEST_ACCEPTANCE_TIME:
        _raise("OBSERVATION_TOO_EARLY")
    smoke = _project_smoke(Path(smoke_path), observed_date, current=True)
    if smoke["gate_status"] != "pass":
        _raise("SMOKE_GATE_FAILED")
    checkout = _git_snapshot(
        Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
    )
    history = _history(output_dir)
    current_history = [
        item for item in history if item["schema_version"] == SCHEMA_VERSION
    ]
    if history and observed_date <= history[-1]["date"]:
        _raise("DATE_NOT_INCREASING")
    observation_day_count = len(current_history) + 1
    previous = current_history[-1] if current_history else None
    continues = (
        previous is not None
        and calendar["previous_trading_date"] == previous["date"]
        and checkout["head"] == previous["head"]
    )
    if continues and previous["pass_day_count"] >= REQUIRED_TRADING_DAYS:
        _raise("WINDOW_COMPLETE")
    pass_day_count = previous["pass_day_count"] + 1 if continues else 1
    epoch_start_date = previous["epoch_start_date"] if continues else observed_date
    doctor = _project_doctor(doctor_input)
    downstream = _project_downstream(downstream_input)
    report = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": local_now.isoformat(timespec="seconds"),
        "observation": {
            **calendar,
            "observation_day_count": observation_day_count,
            "pass_day_count": pass_day_count,
            "required_trading_days": REQUIRED_TRADING_DAYS,
            "window_complete": pass_day_count >= REQUIRED_TRADING_DAYS,
            "gate_status": smoke["gate_status"],
            "epoch_start_date": epoch_start_date,
            "epoch_status": (
                "complete" if pass_day_count >= REQUIRED_TRADING_DAYS else "open"
            ),
        },
        "canonical_checkout": checkout,
        "doctor": doctor,
        "smoke_evidence": smoke,
        "provider_acceptance": _provider_acceptance(doctor, smoke, downstream),
        "latency": _latency(smoke["cases"]),
        "downstream_checks": {
            "market_watch": downstream["market_watch"],
            "live_dashboard": downstream["live_dashboard"],
            "breaker_verification": downstream["breaker_verification"],
        },
        "safety": downstream["safety"],
    }
    report["integrity"] = _report_integrity(report)
    _validate_report(report)
    path = _atomic_write(report, output_dir, observed_date)
    return {
        "path": str(path),
        "date": observed_date,
        "observation_day_count": observation_day_count,
        "pass_day_count": pass_day_count,
        "schema_version": SCHEMA_VERSION,
    }


def validate_daily_acceptance(path: Path) -> dict:
    """Validate one receipt and the complete unique sequence in its directory."""

    path = Path(path).expanduser().resolve()
    report = _load_json(path)
    summary = _validate_report(report, path)
    history = _history(path.parent)
    selected = next((item for item in history if item["path"] == path), None)
    if selected is None or (
        selected["observation_day_count"] != summary["observation_day_count"]
        or selected["pass_day_count"] != summary["pass_day_count"]
    ):
        _raise("INVALID_DAY_SEQUENCE")
    return summary
