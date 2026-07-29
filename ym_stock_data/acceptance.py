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


SCHEMA = "ym-stock-data.acceptance.daily"
SCHEMA_VERSION = "1.1"
LEGACY_SCHEMA_VERSION = "1.0"
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
_SAFE_PARAM_KEYS = frozenset(
    {"sample_id", "code", "codes", "event", "period", "count", "limit", "capability"}
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


def _project_attempts(value: object, code: str = "INVALID_INPUT") -> list[dict]:
    if not isinstance(value, list):
        _raise(code)
    result = []
    for raw in value:
        item = _mapping(raw, code)
        _exact_keys(
            item,
            required={"provider", "status", "error_code", "latency_ms"},
            code=code,
        )
        provider = _enum(item["provider"], code)
        status = _enum(item["status"], code)
        if status not in _ATTEMPT_STATUSES:
            _raise(code)
        error_code = item["error_code"]
        if error_code is not None:
            error_code = _enum(error_code, code)
        result.append(
            {
                "provider": provider,
                "status": status,
                "error_code": error_code,
                "latency_ms": _integer(item["latency_ms"], code),
            }
        )
    return result


def _project_provider_result(value: object, *, extra: frozenset[str] = frozenset()) -> dict:
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
        "attempts": _project_attempts(item["attempts"]),
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


def _project_case(value: object) -> dict:
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
        },
    )
    projected = _project_provider_result(
        {
            "status": item["status"],
            "provider_used": item["provider_used"],
            "attempts": item["attempts"],
            "row_count": item["row_count"],
            "error_code": item["error_code"],
            "latency_ms": item["latency_ms"],
        }
    )
    projected.update(
        {
            "case_id": _enum(item["case_id"]),
            "category": _enum(item["category"]),
            "intent": _enum(item["intent"]),
            "params": _project_safe_params(item["params"]),
        }
    )
    return {
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


def _project_smoke(path: Path, expected_date: str) -> dict:
    path = Path(path).expanduser().resolve()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        _raise("INPUT_UNAVAILABLE")
    if mode != 0o600:
        _raise("INVALID_RECEIPT_PERMISSIONS")
    value = _load_json(path)
    _reject_forbidden(value, code="FORBIDDEN_INPUT")
    _exact_keys(
        value,
        required={"schema_version", "live", "started_at", "completed_at", "summary", "cases"},
    )
    if value["schema_version"] != "1" or value["live"] is not True:
        _raise("INVALID_SMOKE_RECEIPT")
    started_at = _iso(value["started_at"], "INVALID_SMOKE_RECEIPT")
    completed_at = _iso(value["completed_at"], "INVALID_SMOKE_RECEIPT")
    if completed_at[:10] != expected_date or started_at[:10] != expected_date:
        _raise("SMOKE_DATE_MISMATCH")
    raw_cases = value["cases"]
    if not isinstance(raw_cases, list) or len(raw_cases) != 10:
        _raise("INVALID_CASE_COUNT")
    cases = [_project_case(item) for item in raw_cases]
    case_ids = [item["case_id"] for item in cases]
    if len(set(case_ids)) != 10:
        _raise("INVALID_CASE_COUNT")
    summary = _mapping(value["summary"])
    _exact_keys(summary, required={"total", "status_counts"})
    if _integer(summary["total"]) != 10:
        _raise("INVALID_CASE_COUNT")
    counts = dict(sorted(Counter(item["status"] for item in cases).items()))
    supplied_counts = _mapping(summary["status_counts"])
    normalized_counts = {
        _enum(key): _integer(item) for key, item in supplied_counts.items()
    }
    if normalized_counts != counts:
        _raise("INVALID_STATUS_COUNTS")
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "file_mode": "0600",
        "schema_version": "1",
        "live": True,
        "started_at": started_at,
        "completed_at": completed_at,
        "total_cases": 10,
        "status_counts": counts,
        "intent_status_counts": _intent_status_counts(cases),
        "cases": cases,
    }


def _intent_status_counts(cases: list[dict]) -> dict:
    result: dict[str, Counter] = {}
    for case in cases:
        result.setdefault(case["intent"], Counter())[case["status"]] += 1
    return {
        intent: dict(sorted(counts.items())) for intent, counts in sorted(result.items())
    }


def _project_calendar(value: dict, expected_date: str) -> dict:
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
        },
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
    return {
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
            "pending_sentinel": PENDING_STATUS,
            "allowed_result_statuses": sorted(_CASE_STATUSES),
            "allowed_attempt_statuses": sorted(_ATTEMPT_STATUSES),
            "required_replacements": [
                "calendar.is_trading_day",
                "calendar.confirmed",
                "calendar.official_calendar.basis",
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


def _provider_acceptance(doctor: dict, smoke: dict, downstream: dict) -> dict:
    cases = smoke["cases"]
    smoke_attempts = [attempt for case in cases for attempt in case["attempts"]]
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
    return {
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


def _validate_v11(report: dict, observed_date: str, smoke: dict) -> None:
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
    _exact_keys(
        observation,
        required={
            "date",
            "timezone",
            "weekday",
            "is_trading_day",
            "confirmed",
            "official_calendar",
            "day_count",
            "required_trading_days",
            "window_complete",
        },
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
        },
        observed_date,
    )
    if {key: observation[key] for key in projected_calendar} != projected_calendar:
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
    if report["provider_acceptance"] != _provider_acceptance(doctor, smoke, downstream):
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
    if version not in {SCHEMA_VERSION, LEGACY_SCHEMA_VERSION}:
        _raise("INVALID_SCHEMA")
    for key in ("generated_at", "observation", "canonical_checkout", "smoke_evidence", "safety"):
        if key not in report:
            _raise("INVALID_ACCEPTANCE")
    _iso(report["generated_at"], "INVALID_ACCEPTANCE")
    observation = _mapping(report["observation"], "INVALID_ACCEPTANCE")
    observed_date = _date(observation.get("date"), "INVALID_ACCEPTANCE")
    if observation.get("timezone") != "Asia/Shanghai" or observation.get("is_trading_day") is not True:
        _raise("INVALID_ACCEPTANCE")
    day_count = _integer(observation.get("day_count"), "INVALID_ACCEPTANCE")
    if day_count < 1 or observation.get("required_trading_days") != REQUIRED_TRADING_DAYS:
        _raise("INVALID_ACCEPTANCE")
    if observation.get("window_complete") is not (day_count >= REQUIRED_TRADING_DAYS):
        _raise("INVALID_ACCEPTANCE")
    checkout = _mapping(report["canonical_checkout"], "INVALID_ACCEPTANCE")
    if not isinstance(checkout.get("branch"), str) or not _GIT_HEAD.fullmatch(str(checkout.get("head", ""))):
        _raise("INVALID_ACCEPTANCE")
    if checkout.get("tracked_clean") is not True or checkout.get("staged_clean") is not True:
        _raise("INVALID_ACCEPTANCE")
    if version == SCHEMA_VERSION:
        _validate_head_binding(checkout)
    smoke = _mapping(report["smoke_evidence"], "INVALID_ACCEPTANCE")
    smoke_path = Path(str(smoke.get("path", ""))).expanduser()
    recorded_hash = smoke.get("sha256")
    if not isinstance(recorded_hash, str) or not _SHA256.fullmatch(recorded_hash):
        _raise("INVALID_ACCEPTANCE")
    if _sha256(smoke_path) != recorded_hash:
        _raise("RECEIPT_HASH_MISMATCH")
    projected_smoke = _project_smoke(smoke_path, observed_date)
    if smoke.get("total_cases") != 10:
        _raise("INVALID_CASE_COUNT")
    if version == SCHEMA_VERSION:
        _validate_v11(report, observed_date, projected_smoke)
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
        "schema_version": version,
    }


def _history(directory: Path) -> list[dict]:
    if not directory.exists():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        report = _load_json(path)
        summary = _validate_report(report, path)
        records.append({**summary, "path": path})
    dates = [item["date"] for item in records]
    if len(set(dates)) != len(dates):
        _raise("DUPLICATE_DATE")
    ordered = sorted(records, key=lambda item: item["date"])
    for index, item in enumerate(ordered, start=1):
        if item["day_count"] != index:
            _raise("INVALID_DAY_SEQUENCE")
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
    history = _history(output_dir)
    if len(history) >= REQUIRED_TRADING_DAYS:
        _raise("WINDOW_COMPLETE")
    if history and observed_date <= history[-1]["date"]:
        _raise("DATE_NOT_INCREASING")
    day_count = len(history) + 1
    checkout = _git_snapshot(
        Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
    )
    doctor = _project_doctor(doctor_input)
    smoke = _project_smoke(Path(smoke_path), observed_date)
    downstream = _project_downstream(downstream_input)
    report = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": local_now.isoformat(timespec="seconds"),
        "observation": {
            **calendar,
            "day_count": day_count,
            "required_trading_days": REQUIRED_TRADING_DAYS,
            "window_complete": day_count >= REQUIRED_TRADING_DAYS,
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
        "day_count": day_count,
        "schema_version": SCHEMA_VERSION,
    }


def validate_daily_acceptance(path: Path) -> dict:
    """Validate one receipt and the complete unique sequence in its directory."""

    path = Path(path).expanduser().resolve()
    report = _load_json(path)
    summary = _validate_report(report, path)
    history = _history(path.parent)
    selected = next((item for item in history if item["path"] == path), None)
    if selected is None or selected["day_count"] != summary["day_count"]:
        _raise("INVALID_DAY_SEQUENCE")
    return summary
