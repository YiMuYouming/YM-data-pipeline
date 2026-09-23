"""Direct, bounded provider-smoke V3 matrix.

This module deliberately bypasses the canonical query router.  Each matrix
entry calls the registered provider adapter directly so a fallback cannot be
reported as proof that the named provider itself works.  Only sanitized
metadata is written to the receipt.
"""

from __future__ import annotations

import argparse
from collections import Counter
import multiprocessing
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from .api import PROVIDER_REGISTRY, _provider_for
from .contracts import TZ_SHANGHAI
from .providers.base import ProviderOutcome
from .routing import all_route_specs


ROOT = Path(__file__).resolve().parents[1]
PROBE_MANIFEST_PATH = Path(__file__).resolve().parent / "v3" / "provider-smoke.v3.json"
OUTPUT_ROOT = ROOT / "outputs" / "provider-audit"
BASELINE_PATH = Path("/Users/yimu/.ym-stock-data/smoke/2026-09-23T051625+0800.json")
BASELINE_LOGICAL_PATH = "ym-stock-data/smoke/2026-09-23T051625+0800.json"
POLICY_PATH = ROOT / "ym_stock_data" / "v3" / "provider-policy.v3.json"
POLICY_LOGICAL_PATH = "ym_stock_data/v3/provider-policy.v3.json"
_ADAPTER_SOURCE_PATHS = (
    "ym_stock_data/api.py",
    "ym_stock_data/routing.py",
    "ym_stock_data/providers/iwencai.py",
    "ym_stock_data/providers/local.py",
    "ym_stock_data/providers/pytdx_screener.py",
    "ym_stock_data/providers/stocktoday.py",
    "ym_stock_data/sources/eastmoney_index.py",
    "ym_stock_data/sources/eastmoney_stock.py",
    "ym_stock_data/sources/sina_index.py",
    "ym_stock_data/providers/tdx_mcp.py",
    "ym_stock_data/providers/wind_mcp.py",
)
SCHEMA_VERSION = "provider-smoke.v3"
RUNNER_VERSION = "provider-smoke-runner.v3.2"
DEFAULT_CASE_TIMEOUT_SEC = 25.0
DEFAULT_GLOBAL_TIMEOUT_SEC = 840.0
MAX_CASE_TIMEOUT_SEC = 30.0
MAX_GLOBAL_TIMEOUT_SEC = 900.0
PROVIDER_SMOKE_STATUSES = frozenset(
    {
        "success",
        "empty",
        "degraded",
        "configured_unverified",
        "dependency_missing",
        "auth_error",
        "rate_limited",
        "provider_error",
        "timeout",
        "invalid_params",
    }
)
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_FIELD_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_SENSITIVE_RE = re.compile(
    r"(?i)(?:api[_-]?key|private[_-]?key|secret|token|cookie|authorization|bearer|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|client[_-]?id|password|credential)"
)
_FORBIDDEN_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:sk|rk)-[A-Za-z0-9_-]{12,}|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|"
    r"token|credential|authorization)\s*[:=]\s*[^\s,;]+)"
)
_WINDOWS_ABSOLUTE_RE = re.compile(r"(?i)^(?:[A-Z]:[\\/]|\\\\)")
_ALLOWED_OPERATIONS = frozenset({"direct_provider_call", "direct_provider_probe"})
_FALLBACK_KINDS = frozenset({"source_internal", "provider_internal"})
_DATA_STATUSES = frozenset({"success", "empty", "degraded"})
_RECEIPT_TIME_TOLERANCE_MS = 1000
_EXPLICIT_ONLY_CASE_PAIRS = frozenset({("pytdx_screener", "review_sentiment")})
_MAX_METADATA_FIELDS = 200
_PROBE_MANIFEST_KEYS = frozenset(
    {"schema_version", "manifest_version", "providers", "external_pending"}
)
_PROBE_ENTRY_KEYS = frozenset(
    {"provider", "operation", "intent", "capability", "params", "read_only"}
)
_EXTERNAL_ENTRY_KEYS = frozenset(
    {"provider", "status", "reason", "counts_as_provider"}
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value))


def _safe_code(value: object, default: str | None = None) -> str | None:
    candidate = str(value or "")
    return candidate if _SAFE_CODE_RE.fullmatch(candidate) else default


def _safe_field(value: object) -> str | None:
    if not isinstance(value, str) or not _SAFE_FIELD_RE.fullmatch(value):
        return None
    if os.path.isabs(value) or _WINDOWS_ABSOLUTE_RE.search(value) or _SENSITIVE_RE.search(value):
        return None
    return value


def _absolute_like(value: str) -> bool:
    return os.path.isabs(value) or bool(_WINDOWS_ABSOLUTE_RE.search(value))


def _safe_timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.isoformat(timespec="milliseconds")


def _now_iso() -> str:
    return datetime.now(TZ_SHANGHAI).isoformat(timespec="milliseconds")


def _now_datetime() -> datetime:
    return datetime.now(TZ_SHANGHAI)


def _parse_timestamp(value: object, field: str) -> datetime:
    normalized = _safe_timestamp(value)
    if normalized is None:
        raise ValueError(f"{field} must be timezone-aware ISO-8601")
    return datetime.fromisoformat(normalized)


def _relative_timestamp(base: datetime, elapsed_sec: float) -> str:
    return _safe_timestamp(
        (base + timedelta(seconds=max(0.0, elapsed_sec))).isoformat()
    ) or _now_iso()


def _source_file_binding(logical_path: str) -> dict[str, str]:
    path = ROOT / logical_path
    if not path.is_file():
        raise ValueError(f"provider-smoke source binding is unavailable: {logical_path}")
    return {"logical_path": logical_path, "sha256": _sha256_bytes(path.read_bytes())}


def _policy_binding() -> dict[str, Any]:
    if not POLICY_PATH.is_file():
        raise ValueError("provider-smoke policy binding is unavailable")
    try:
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("provider-smoke policy binding is unreadable") from error
    if not isinstance(policy, dict) or policy.get("active") is not False:
        raise ValueError("provider-smoke requires inactive provider policy")
    return {
        **_source_file_binding(POLICY_LOGICAL_PATH),
        "active": False,
    }


def _resolve_manifest_value(value: Any, *, now: datetime) -> Any:
    if isinstance(value, str) and value == "${today_shanghai}":
        return now.strftime("%Y%m%d")
    if isinstance(value, dict):
        return {key: _resolve_manifest_value(child, now=now) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve_manifest_value(child, now=now) for child in value]
    return value


def _reject_sensitive_tree(value: Any, *, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or _SENSITIVE_RE.search(key):
                raise ValueError(f"sensitive manifest key: {path}.{key}")
            _reject_sensitive_tree(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_tree(child, path=f"{path}[{index}]")
    elif isinstance(value, str):
        if _absolute_like(value) or _FORBIDDEN_VALUE_RE.search(value):
            raise ValueError(f"unsafe manifest value: {path}")


def _validate_probe_manifest(manifest: Mapping[str, Any]) -> None:
    if set(manifest) != _PROBE_MANIFEST_KEYS:
        raise ValueError("provider-smoke probe manifest keys drifted")
    if manifest["schema_version"] != "provider-smoke-probe.v3":
        raise ValueError("unsupported provider-smoke probe manifest")
    providers = manifest["providers"]
    if not isinstance(providers, list):
        raise ValueError("provider-smoke providers must be a list")
    names = []
    for entry in providers:
        if not isinstance(entry, dict) or set(entry) != _PROBE_ENTRY_KEYS:
            raise ValueError("provider-smoke provider entry keys drifted")
        name = entry.get("provider")
        if not isinstance(name, str) or not _SAFE_CODE_RE.fullmatch(name):
            raise ValueError("invalid provider name in probe manifest")
        names.append(name)
        if entry["operation"] not in _ALLOWED_OPERATIONS:
            raise ValueError(f"unsupported operation for {name}")
        if entry["operation"] == "direct_provider_call" and not isinstance(entry["intent"], str):
            raise ValueError(f"direct provider call needs intent: {name}")
        if entry["operation"] == "direct_provider_probe" and entry["intent"] is not None:
            raise ValueError(f"provider probe intent must be null: {name}")
        if not isinstance(entry["capability"], str) or not entry["capability"]:
            raise ValueError(f"provider capability is required: {name}")
        if not isinstance(entry["params"], dict) or entry["read_only"] is not True:
            raise ValueError(f"provider probe params must be read-only: {name}")
    if set(names) != set(PROVIDER_REGISTRY):
        raise ValueError("probe manifest must cover PROVIDER_REGISTRY exactly")
    probe_entries = [
        entry for entry in providers if entry["operation"] == "direct_provider_probe"
    ]
    if len(probe_entries) != 1 or probe_entries[0]["provider"] != "tdx_mcp":
        raise ValueError("tdx_mcp must be the sole provider probe-only entry")
    direct_pairs = {
        (entry["provider"], entry["intent"])
        for entry in providers
        if entry["operation"] == "direct_provider_call"
    }
    expected_pairs = {
        (provider, spec.intent)
        for spec in all_route_specs()
        for provider in spec.providers
    }
    if len(direct_pairs) != len(
        [entry for entry in providers if entry["operation"] == "direct_provider_call"]
    ):
        raise ValueError("provider-smoke route/provider-intent pairs must be unique")
    if direct_pairs != expected_pairs | _EXPLICIT_ONLY_CASE_PAIRS:
        raise ValueError("provider-smoke manifest route/provider-intent universe drifted")
    external_pending = manifest["external_pending"]
    if not isinstance(external_pending, list):
        raise ValueError("external_pending must be a list")
    for entry in external_pending:
        if not isinstance(entry, dict) or set(entry) != _EXTERNAL_ENTRY_KEYS:
            raise ValueError("external_pending entry keys drifted")
        if entry["status"] != "external_pending" or entry["counts_as_provider"] is not False:
            raise ValueError("external_pending entries cannot count as providers")
        for key in ("provider", "reason"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise ValueError("external_pending metadata is incomplete")
    _reject_sensitive_tree(manifest)


def load_probe_manifest(path: Path | str = PROBE_MANIFEST_PATH) -> dict[str, Any]:
    manifest_path = Path(path)
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("provider-smoke probe manifest must be an object")
    _validate_probe_manifest(value)
    return value


def _source_binding(manifest: Mapping[str, Any]) -> dict[str, str]:
    if not BASELINE_PATH.is_file():
        raise ValueError("provider-smoke baseline binding is unavailable")
    return {
        "runner": "ym_stock_data/provider_smoke_v3.py",
        "runner_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "probe_manifest": "ym_stock_data/v3/provider-smoke.v3.json",
        "probe_manifest_sha256": _sha256_bytes(PROBE_MANIFEST_PATH.read_bytes()),
        "baseline_logical_path": BASELINE_LOGICAL_PATH,
        "baseline_sha256": _sha256_bytes(BASELINE_PATH.read_bytes()),
        "registry_source": _source_file_binding("ym_stock_data/api.py"),
        "adapter_sources": [
            _source_file_binding(path) for path in _ADAPTER_SOURCE_PATHS
        ],
        "policy": _policy_binding(),
    }


def _registry_binding() -> dict[str, Any]:
    names = sorted(PROVIDER_REGISTRY)
    return {
        "source": "ym_stock_data.api.PROVIDER_REGISTRY",
        "count": len(names),
        "providers": names,
        "sha256": _sha256_json(names),
    }


def _field_names(value: Any, *, depth: int = 0) -> set[str]:
    if depth > 4:
        return set()
    result: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            safe_key = _safe_field(key)
            if safe_key is not None:
                result.add(safe_key)
            if key in {"fields", "columns"} and isinstance(child, list):
                for field in child:
                    safe_field = _safe_field(field)
                    if safe_field is not None:
                        result.add(safe_field)
            elif key in {"items", "rows", "bars", "reports", "filings", "datas", "data"}:
                result.update(_field_names(child, depth=depth + 1))
    elif isinstance(value, list):
        for child in value[:100]:
            result.update(_field_names(child, depth=depth + 1))
    return result


def _schema_summary(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        kind = "mapping"
    elif isinstance(data, list):
        kind = "list"
    elif data is None:
        kind = "none"
    else:
        kind = type(data).__name__
    return {"kind": kind, "keys": sorted(_field_names(data))[:200]}


def _row_count(outcome: ProviderOutcome) -> int:
    quality = outcome.quality if isinstance(outcome.quality, dict) else {}
    returned = quality.get("returned_count")
    if isinstance(returned, int) and not isinstance(returned, bool) and returned >= 0:
        return returned
    data = outcome.data
    if isinstance(data, dict):
        for key in ("items", "rows", "bars", "reports", "filings", "datas"):
            if isinstance(data.get(key), list):
                return len(data[key])
    if isinstance(data, list):
        return len(data)
    return 0


def _auth_summary(value: object) -> dict[str, str | bool]:
    if not isinstance(value, dict):
        return {"required": False, "status": "unknown"}
    required = value.get("required") if isinstance(value.get("required"), bool) else False
    status = _safe_code(value.get("status"), "unknown") or "unknown"
    return {"required": required, "status": status}


def _provenance_summary(provider: str, value: object) -> dict[str, object]:
    provenance = value if isinstance(value, dict) else {}
    effective = _safe_code(provenance.get("effective_provider", provider), "unknown") or "unknown"
    fallback_from = _safe_code(provenance.get("fallback_from"))
    kind = _safe_code(provenance.get("kind"))
    verified = provenance.get("verified") if isinstance(provenance.get("verified"), bool) else False
    return {
        "effective_provider": effective,
        "fallback_from": fallback_from,
        "kind": kind,
        "verified": verified,
    }


def _has_declared_fallback(provenance: Mapping[str, Any]) -> bool:
    return (
        isinstance(provenance.get("fallback_from"), str)
        and bool(_SAFE_CODE_RE.fullmatch(provenance["fallback_from"]))
        and provenance.get("kind") in _FALLBACK_KINDS
        and provenance.get("verified") is True
    )


def _direct_provenance(provider: str) -> dict[str, object]:
    return {
        "effective_provider": provider,
        "fallback_from": None,
        "kind": None,
        "verified": False,
    }


def _valid_degraded_provenance(
    case_provider: str,
    provenance: Mapping[str, Any],
    *,
    registry_names: set[str] | frozenset[str] = frozenset(PROVIDER_REGISTRY),
) -> bool:
    effective = provenance.get("effective_provider")
    return (
        isinstance(effective, str)
        and effective in registry_names
        and effective != case_provider
        and provenance.get("fallback_from") == case_provider
        and provenance.get("kind") in _FALLBACK_KINDS
        and provenance.get("verified") is True
    )


def _valid_direct_provenance(
    case_provider: str,
    provenance: Mapping[str, Any],
    *,
    probe_only: bool = False,
) -> bool:
    return (
        provenance.get("effective_provider") == case_provider
        and provenance.get("fallback_from") is None
        and provenance.get("kind") in (
            {None, "provider_probe_only"} if probe_only else {None}
        )
        and provenance.get("verified") is False
    )


def _default_error_code(raw_status: str) -> str:
    candidate = _safe_code(raw_status.upper())
    return candidate or "PROVIDER_ERROR"


def _mapped_outcome_status(
    outcome: ProviderOutcome, *, case_provider: str
) -> tuple[str, str | None, str | None]:
    raw_status = str(outcome.status or "provider_error")
    provenance = _provenance_summary(outcome.provider, outcome.provenance)
    if raw_status in {"success", "empty"}:
        if _valid_degraded_provenance(case_provider, provenance):
            return "degraded", None, None
        if not _valid_direct_provenance(case_provider, provenance):
            return "provider_error", "INVALID_PROVIDER_PROVENANCE", "provider_provenance"
        return raw_status, None, None
    if raw_status == "degraded":
        return "provider_error", "UNDECLARED_DEGRADED", "provider_provenance"
    if raw_status in PROVIDER_SMOKE_STATUSES:
        if not _valid_direct_provenance(case_provider, provenance):
            return "provider_error", "INVALID_PROVIDER_PROVENANCE", "provider_provenance"
        if raw_status in _DATA_STATUSES:
            return raw_status, None, None
        return (
            raw_status,
            _safe_code(outcome.error_code) or _default_error_code(raw_status),
            _safe_code(raw_status, "PROVIDER_ERROR") or "PROVIDER_ERROR",
        )
    mapping = {
        "breaker_open": "provider_error",
        "network_error": "provider_error",
        "incompatible": "invalid_params",
        "error": "provider_error",
        "unavailable": "provider_error",
    }
    status = mapping.get(raw_status, "provider_error")
    if not _valid_direct_provenance(case_provider, provenance):
        return "provider_error", "INVALID_PROVIDER_PROVENANCE", "provider_provenance"
    return status, _safe_code(outcome.error_code) or _default_error_code(raw_status), _safe_code(raw_status, "PROVIDER_ERROR")


def _case_from_outcome(
    entry: Mapping[str, Any],
    outcome: ProviderOutcome,
    *,
    observed_at: str,
    latency_ms: int,
) -> dict[str, Any]:
    status, fallback_code, error_type = _mapped_outcome_status(
        outcome, case_provider=entry["provider"]
    )
    error_code = fallback_code or _safe_code(outcome.error_code)
    provenance = _provenance_summary(outcome.provider, outcome.provenance)
    if status == "provider_error" and not _valid_direct_provenance(entry["provider"], provenance):
        provenance = _direct_provenance(entry["provider"])
    row_count = _row_count(outcome) if status in _DATA_STATUSES else 0
    if status == "success" and row_count == 0:
        status = "empty"
    elif status == "empty" and row_count > 0:
        status = "success"
    elif status == "degraded" and row_count == 0:
        status = "provider_error"
        error_code = "DEGRADED_EMPTY"
        error_type = "provider_provenance"
        provenance = _direct_provenance(entry["provider"])
    if status in _DATA_STATUSES:
        error_code = None
        error_type = None
    fetched_at = _safe_timestamp(outcome.fetched_at)
    return {
        "provider": entry["provider"],
        "intent": entry["intent"],
        "capability": entry["capability"],
        "operation": entry["operation"],
        "direct_probe": True,
        "status": status,
        "error_code": error_code,
        "error_type": error_type,
        "row_count": row_count if status in _DATA_STATUSES else 0,
        "latency_ms": max(0, int(latency_ms)),
        "observed_at": observed_at,
        "fields": sorted(_field_names(outcome.data))[:200] if status in {"success", "empty", "degraded"} else [],
        "schema": _schema_summary(outcome.data) if status in {"success", "empty", "degraded"} else {"kind": "none", "keys": []},
        "freshness": {"fetched_at": fetched_at, "status": "observed" if fetched_at else "unknown"},
        "provenance": provenance,
        "auth": _auth_summary(outcome.auth),
    }


def _case_from_probe(
    entry: Mapping[str, Any],
    raw: object,
    *,
    observed_at: str,
    latency_ms: int,
) -> dict[str, Any]:
    raw_status = raw.get("status") if isinstance(raw, dict) else None
    if raw_status == "auth_missing":
        status, error_code = "auth_error", "AUTH_MISSING"
    elif raw_status == "auth_expired":
        status, error_code = "auth_error", "AUTH_EXPIRED"
    elif raw_status == "dependency_missing":
        status, error_code = "dependency_missing", "DEPENDENCY_MISSING"
    elif raw_status in {"configured_unverified", "ready"}:
        status, error_code = "configured_unverified", "NO_DIRECT_INTENT"
    else:
        status, error_code = "provider_error", "PROBE_FAILED"
    return {
        "provider": entry["provider"],
        "intent": None,
        "capability": entry["capability"],
        "operation": entry["operation"],
        "direct_probe": True,
        "status": status,
        "error_code": error_code,
        "error_type": "provider_probe_only",
        "row_count": 0,
        "latency_ms": max(0, int(latency_ms)),
        "observed_at": observed_at,
        "fields": [],
        "schema": {"kind": "none", "keys": []},
        "freshness": {"fetched_at": None, "status": "unknown"},
        "provenance": {
            "effective_provider": entry["provider"],
            "fallback_from": None,
            "kind": "provider_probe_only",
            "verified": False,
        },
        "auth": _auth_summary(raw.get("auth") if isinstance(raw, dict) else None),
    }


def _timeout_case(entry: Mapping[str, Any], *, observed_at: str, code: str) -> dict[str, Any]:
    return {
        "provider": entry["provider"],
        "intent": entry["intent"],
        "capability": entry["capability"],
        "operation": entry["operation"],
        "direct_probe": True,
        "status": "timeout",
        "error_code": code,
        "error_type": "timeout",
        "row_count": 0,
        "latency_ms": 0,
        "observed_at": observed_at,
        "fields": [],
        "schema": {"kind": "none", "keys": []},
        "freshness": {"fetched_at": None, "status": "unknown"},
        "provenance": {
            "effective_provider": entry["provider"],
            "fallback_from": None,
            "kind": None,
            "verified": False,
        },
        "auth": {"required": False, "status": "unknown"},
    }


def _exception_case(
    entry: Mapping[str, Any],
    *,
    observed_at: str,
    error: BaseException | None = None,
    error_type: str | None = None,
    latency_ms: int,
) -> dict[str, Any]:
    safe_error_type = _safe_code(
        error_type or (type(error).__name__ if error is not None else None),
        "PROVIDER_EXCEPTION",
    ) or "PROVIDER_EXCEPTION"
    return {
        "provider": entry["provider"],
        "intent": entry["intent"],
        "capability": entry["capability"],
        "operation": entry["operation"],
        "direct_probe": True,
        "status": "provider_error",
        "error_code": "PROVIDER_EXCEPTION",
        "error_type": safe_error_type,
        "row_count": 0,
        "latency_ms": max(0, int(latency_ms)),
        "observed_at": observed_at,
        "fields": [],
        "schema": {"kind": "none", "keys": []},
        "freshness": {"fetched_at": None, "status": "unknown"},
        "provenance": {
            "effective_provider": entry["provider"],
            "fallback_from": None,
            "kind": None,
            "verified": False,
        },
        "auth": {"required": False, "status": "unknown"},
    }


def _provider_case_worker(
    connection: Any,
    entry: Mapping[str, Any],
    provider_loader: Callable[[str], object],
    observed_at: str,
    params: Mapping[str, Any],
) -> None:
    """Run one provider call in an isolated process.

    The worker sends only the already-sanitized case metadata.  It never sends
    raw provider data or exception messages through the parent pipe.
    """

    try:
        provider = provider_loader(entry["provider"])
        started = time.monotonic()
        if entry["operation"] == "direct_provider_probe":
            raw = provider.probe()
            case = _case_from_probe(
                entry,
                raw,
                observed_at=observed_at,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        else:
            outcome = provider.call(entry["intent"], dict(params))
            if not isinstance(outcome, ProviderOutcome):
                raise TypeError("provider did not return ProviderOutcome")
            case = _case_from_outcome(
                entry,
                outcome,
                observed_at=observed_at,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        connection.send({"case": case})
    except BaseException as error:
        connection.send(
            {
                "error_type": _safe_code(type(error).__name__, "PROVIDER_EXCEPTION")
                or "PROVIDER_EXCEPTION"
            }
        )
    finally:
        connection.close()


def _terminate_worker(process: Any) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(timeout=0.02)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(timeout=0.02)
    if process.is_alive():
        process.join()


def _validate_timeout_bounds(case_timeout_sec: float, global_timeout_sec: float) -> None:
    if not isinstance(case_timeout_sec, (int, float)) or isinstance(case_timeout_sec, bool) or case_timeout_sec <= 0:
        raise ValueError("case_timeout_sec must be positive")
    if not isinstance(global_timeout_sec, (int, float)) or isinstance(global_timeout_sec, bool) or global_timeout_sec <= 0:
        raise ValueError("global_timeout_sec must be positive")
    if case_timeout_sec > MAX_CASE_TIMEOUT_SEC:
        raise ValueError("case_timeout_sec must be <= 30 seconds")
    if global_timeout_sec > MAX_GLOBAL_TIMEOUT_SEC:
        raise ValueError("global_timeout_sec must be <= 900 seconds")


def _receipt_template(
    *,
    manifest: Mapping[str, Any],
    started_at: str,
    completed_at: str,
    case_timeout_sec: float,
    global_timeout_sec: float,
    providers: list[dict[str, Any]],
    live: bool,
) -> dict[str, Any]:
    counts = Counter(item["status"] for item in providers)
    case_counts = {"total": len(providers)}
    case_counts.update({status: counts.get(status, 0) for status in sorted(PROVIDER_SMOKE_STATUSES)})
    external_pending = [dict(item) for item in manifest["external_pending"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "live": live,
        "started_at": started_at,
        "completed_at": completed_at,
        "bounds": {
            "case_timeout_sec": float(case_timeout_sec),
            "global_timeout_sec": float(global_timeout_sec),
            "case_count": len(providers),
            "read_only": True,
            "canonical_fallback_used": False,
        },
        "registry": _registry_binding(),
        "source_binding": _source_binding(manifest),
        "case_counts": case_counts,
        "providers": providers,
        "external_pending": external_pending,
    }


def _stamp_case_timing(
    case: Mapping[str, Any],
    *,
    case_started_at: str,
    observed_at: str,
    case_completed_at: str,
    latency_ms: int,
) -> dict[str, Any]:
    stamped = dict(case)
    stamped["case_started_at"] = case_started_at
    stamped["observed_at"] = observed_at
    stamped["case_completed_at"] = case_completed_at
    stamped["latency_ms"] = max(0, int(latency_ms))
    return stamped


def _multiprocessing_context() -> Any:
    methods = multiprocessing.get_all_start_methods()
    if "fork" in methods:
        return multiprocessing.get_context("fork")
    return multiprocessing.get_context()


def _run_isolated_case(
    *,
    entry: Mapping[str, Any],
    provider_loader: Callable[[str], object],
    params: Mapping[str, Any],
    observed_at: str,
    budget_sec: float,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    context = _multiprocessing_context()
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_provider_case_worker,
        args=(sender, entry, provider_loader, observed_at, params),
    )
    try:
        process.start()
    except BaseException as error:
        receiver.close()
        sender.close()
        return None, _safe_code(type(error).__name__, "PROVIDER_EXCEPTION"), False
    finally:
        # The child owns the sending end after fork/spawn.  The parent must
        # close its copy so EOF is observable if the worker exits silently.
        sender.close()

    process.join(timeout=max(0.0, budget_sec))
    timed_out = process.is_alive()
    if timed_out:
        _terminate_worker(process)
    else:
        process.join()

    message: object = None
    try:
        if receiver.poll(0):
            message = receiver.recv()
    except (EOFError, OSError):
        message = None
    finally:
        receiver.close()

    if timed_out:
        return None, None, True
    if isinstance(message, dict) and isinstance(message.get("case"), dict):
        return message["case"], None, False
    if isinstance(message, dict):
        return None, _safe_code(message.get("error_type"), "PROVIDER_EXCEPTION"), False
    return None, "WORKER_EXITED", False


def run_provider_smoke(
    *,
    output_root: Path | str = OUTPUT_ROOT,
    provider_loader: Callable[[str], object] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    case_timeout_sec: float = DEFAULT_CASE_TIMEOUT_SEC,
    global_timeout_sec: float = DEFAULT_GLOBAL_TIMEOUT_SEC,
    _test_only_output_root: bool = False,
) -> dict[str, Any]:
    """Run the direct provider/capability matrix and write one safe receipt."""

    _validate_timeout_bounds(case_timeout_sec, global_timeout_sec)
    requested_root = Path(os.path.abspath(os.fspath(output_root)))
    canonical_root = Path(os.path.abspath(os.fspath(OUTPUT_ROOT)))
    live = (
        provider_loader is None
        and now_fn is None
        and not _test_only_output_root
        and requested_root == canonical_root
    )
    if live and requested_root != canonical_root:
        raise ValueError("live provider-smoke requires the canonical output root")
    if not live and not _test_only_output_root:
        raise ValueError("fixture provider-smoke runs require a private output root")
    if not live and requested_root == canonical_root:
        raise ValueError("fixture provider-smoke runs cannot use the canonical output root")
    loader = provider_loader or _provider_for
    clock = now_fn or _now_datetime
    _validate_output_root(output_root, _test_only_output_root=_test_only_output_root)
    manifest = load_probe_manifest()
    started_at = _safe_timestamp(clock().isoformat(timespec="milliseconds")) or _now_iso()
    started_datetime = _parse_timestamp(started_at, "started_at")
    started = time.monotonic()
    providers: list[dict[str, Any]] = []

    for entry in manifest["providers"]:
        elapsed = time.monotonic() - started
        remaining = global_timeout_sec - elapsed
        case_started_elapsed = max(0.0, time.monotonic() - started)
        case_started_at = _relative_timestamp(started_datetime, case_started_elapsed)
        observed_at = case_started_at
        if remaining <= 0:
            case = _timeout_case(entry, observed_at=observed_at, code="GLOBAL_TIMEOUT")
            providers.append(
                _stamp_case_timing(
                    case,
                    case_started_at=case_started_at,
                    observed_at=observed_at,
                    case_completed_at=case_started_at,
                    latency_ms=0,
                )
            )
            continue
        budget = min(float(case_timeout_sec), float(remaining))
        params = _resolve_manifest_value(entry["params"], now=clock())
        case, worker_error_type, timed_out = _run_isolated_case(
            entry=entry,
            provider_loader=loader,
            params=params,
            observed_at=observed_at,
            budget_sec=budget,
        )
        case_completed_elapsed = max(0.0, time.monotonic() - started)
        case_completed_at = _relative_timestamp(started_datetime, case_completed_elapsed)
        latency_ms = max(0, int((case_completed_elapsed - case_started_elapsed) * 1000))
        if timed_out:
            code = "GLOBAL_TIMEOUT" if remaining <= float(case_timeout_sec) else "TIMEOUT"
            case = _timeout_case(entry, observed_at=observed_at, code=code)
        elif case is None:
            case = _exception_case(
                entry,
                observed_at=observed_at,
                error_type=worker_error_type or "WORKER_EXITED",
                latency_ms=latency_ms,
            )
        providers.append(
            _stamp_case_timing(
                case,
                case_started_at=case_started_at,
                observed_at=observed_at,
                case_completed_at=case_completed_at,
                latency_ms=latency_ms,
            )
        )

    completed_at = _relative_timestamp(started_datetime, time.monotonic() - started)
    receipt = _receipt_template(
        manifest=manifest,
        started_at=started_at,
        completed_at=completed_at,
        case_timeout_sec=case_timeout_sec,
        global_timeout_sec=global_timeout_sec,
        providers=providers,
        live=live,
    )
    validate_provider_receipt(receipt)
    path = write_provider_receipt(
        receipt,
        output_root=output_root,
        _test_only_output_root=_test_only_output_root,
    )
    return {
        "receipt": str(path),
        "receipt_sha256": _sha256_bytes(path.read_bytes()),
        "case_counts": receipt["case_counts"],
        "providers": receipt["providers"],
        "external_pending": receipt["external_pending"],
    }


def _validate_iso(value: object, field: str) -> None:
    _parse_timestamp(value, field)


def _validate_no_sensitive_values(value: Any, *, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or _SENSITIVE_RE.search(key):
                raise ValueError(f"sensitive receipt key: {path}.{key}")
            _validate_no_sensitive_values(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_no_sensitive_values(child, path=f"{path}[{index}]")
    elif isinstance(value, str):
        if _absolute_like(value) or _FORBIDDEN_VALUE_RE.search(value):
            raise ValueError(f"unsafe receipt value: {path}")


def _validate_case(
    case: Mapping[str, Any],
    *,
    entry: Mapping[str, Any],
    registry_names: set[str],
    receipt_started: datetime,
    receipt_completed: datetime,
) -> None:
    required = {
        "provider", "intent", "capability", "operation", "direct_probe", "status",
        "error_code", "error_type", "row_count", "latency_ms", "case_started_at",
        "observed_at", "case_completed_at",
        "fields", "schema", "freshness", "provenance", "auth",
    }
    if set(case) != required:
        raise ValueError("provider conclusion keys drifted")
    if case["provider"] not in registry_names or case["operation"] not in _ALLOWED_OPERATIONS:
        raise ValueError("provider conclusion is not bound to the registry")
    for field in ("provider", "intent", "capability", "operation"):
        if case[field] != entry[field]:
            raise ValueError("provider conclusion is not bound to its canonical manifest entry")
    if case["provider"] != entry["provider"] or case["operation"] != entry["operation"]:
        raise ValueError("provider conclusion provider/operation binding mismatch")
    if case["direct_probe"] is not True:
        raise ValueError("provider conclusion must be a direct probe")
    if case["direct_probe"] is not True or case["status"] not in PROVIDER_SMOKE_STATUSES:
        raise ValueError("invalid provider conclusion status")
    if case["intent"] is not None and not isinstance(case["intent"], str):
        raise ValueError("provider conclusion intent must be string or null")
    if not isinstance(case["capability"], str) or not case["capability"]:
        raise ValueError("provider conclusion capability is required")
    if case["error_code"] is not None and _safe_code(case["error_code"]) is None:
        raise ValueError("provider conclusion error_code is unsafe")
    if case["error_type"] is not None and _safe_code(case["error_type"]) is None:
        raise ValueError("provider conclusion error_type is unsafe")
    if type(case["row_count"]) is not int or case["row_count"] < 0:
        raise ValueError("provider conclusion row_count is invalid")
    if type(case["latency_ms"]) is not int or case["latency_ms"] < 0:
        raise ValueError("provider conclusion latency is invalid")
    case_started = _parse_timestamp(case["case_started_at"], "case_started_at")
    observed = _parse_timestamp(case["observed_at"], "observed_at")
    case_completed = _parse_timestamp(case["case_completed_at"], "case_completed_at")
    if not (receipt_started <= case_started <= observed <= case_completed <= receipt_completed):
        raise ValueError("provider conclusion time interval is outside receipt bounds")
    duration_ms = int((case_completed - case_started).total_seconds() * 1000)
    if case["latency_ms"] > duration_ms + _RECEIPT_TIME_TOLERANCE_MS:
        raise ValueError("provider conclusion latency exceeds case interval")
    if (
        not isinstance(case["fields"], list)
        or len(case["fields"]) > _MAX_METADATA_FIELDS
        or any(not isinstance(field, str) for field in case["fields"])
    ):
        raise ValueError("provider conclusion fields are invalid")
    if len(case["fields"]) != len(set(case["fields"])):
        raise ValueError("provider conclusion fields are invalid")
    for field in case["fields"]:
        if _safe_field(field) is None:
            raise ValueError("provider conclusion field is unsafe")
    schema = case["schema"]
    if not isinstance(schema, dict) or set(schema) != {"kind", "keys"}:
        raise ValueError("provider conclusion schema is invalid")
    if not isinstance(schema["kind"], str) or not isinstance(schema["keys"], list):
        raise ValueError("provider conclusion schema types are invalid")
    if (
        len(schema["keys"]) > _MAX_METADATA_FIELDS
        or any(not isinstance(field, str) for field in schema["keys"])
    ):
        raise ValueError("provider conclusion schema fields are invalid")
    if len(schema["keys"]) != len(set(schema["keys"])):
        raise ValueError("provider conclusion schema fields are duplicated")
    for field in schema["keys"]:
        if _safe_field(field) is None:
            raise ValueError("provider conclusion schema field is unsafe")
    freshness = case["freshness"]
    if not isinstance(freshness, dict) or set(freshness) != {"fetched_at", "status"}:
        raise ValueError("provider conclusion freshness is invalid")
    if freshness["fetched_at"] is not None:
        fetched_at = _parse_timestamp(freshness["fetched_at"], "freshness.fetched_at")
        if fetched_at > case_completed:
            raise ValueError("provider conclusion fetched_at is after case completion")
    if freshness["status"] not in {"observed", "unknown"}:
        raise ValueError("provider conclusion freshness status is invalid")
    provenance = case["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"effective_provider", "fallback_from", "kind", "verified"}:
        raise ValueError("provider conclusion provenance is invalid")
    for key in ("effective_provider", "fallback_from", "kind"):
        if provenance[key] is not None and _safe_code(provenance[key]) is None:
            raise ValueError("provider conclusion provenance is unsafe")
    if not isinstance(provenance["verified"], bool):
        raise ValueError("provider conclusion provenance verified is invalid")
    if provenance["effective_provider"] != case["provider"]:
        if not _valid_degraded_provenance(
            case["provider"], provenance, registry_names=registry_names
        ) or case["status"] != "degraded":
            raise ValueError("provider replacement must be a verified degraded fallback")
    elif not _valid_direct_provenance(
        case["provider"],
        provenance,
        probe_only=entry["operation"] == "direct_provider_probe",
    ):
        raise ValueError("direct provider conclusion cannot fake fallback provenance")
    auth = case["auth"]
    if not isinstance(auth, dict) or set(auth) != {"required", "status"}:
        raise ValueError("provider conclusion auth is invalid")
    if not isinstance(auth["required"], bool) or _safe_code(auth["status"]) is None:
        raise ValueError("provider conclusion auth is unsafe")

    status = case["status"]
    if status in {"success", "degraded"} and case["row_count"] <= 0:
        raise ValueError("data provider conclusion must have positive row_count")
    if status == "empty" and case["row_count"] != 0:
        raise ValueError("empty provider conclusion must have zero row_count")
    if status in _DATA_STATUSES:
        if case["error_code"] is not None or case["error_type"] is not None:
            raise ValueError("data provider conclusion cannot carry error metadata")
    else:
        if case["row_count"] != 0 or case["fields"] or schema != {"kind": "none", "keys": []}:
            raise ValueError("non-data provider conclusion must have empty data metadata")
        if _safe_code(case["error_code"]) is None or _safe_code(case["error_type"]) is None:
            raise ValueError("failed provider conclusion needs safe error metadata")


def validate_provider_receipt(receipt: Mapping[str, Any]) -> None:
    """Validate a V3 receipt against the live registry and current source hashes."""

    required = {
        "schema_version", "runner_version", "live", "started_at", "completed_at", "bounds",
        "registry", "source_binding", "case_counts", "providers", "external_pending",
    }
    if not isinstance(receipt, Mapping) or set(receipt) != required:
        raise ValueError("provider-smoke receipt keys drifted")
    if receipt["schema_version"] != SCHEMA_VERSION or receipt["runner_version"] != RUNNER_VERSION:
        raise ValueError("provider-smoke receipt version mismatch")
    if not isinstance(receipt["live"], bool):
        raise ValueError("provider-smoke receipt live flag is invalid")
    receipt_started = _parse_timestamp(receipt["started_at"], "started_at")
    receipt_completed = _parse_timestamp(receipt["completed_at"], "completed_at")
    if receipt_started > receipt_completed:
        raise ValueError("provider-smoke receipt time interval is reversed")

    bounds = receipt["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != {"case_timeout_sec", "global_timeout_sec", "case_count", "read_only", "canonical_fallback_used"}:
        raise ValueError("provider-smoke bounds drifted")
    _validate_timeout_bounds(bounds["case_timeout_sec"], bounds["global_timeout_sec"])
    receipt_elapsed_ms = int((receipt_completed - receipt_started).total_seconds() * 1000)
    if receipt_elapsed_ms > int(bounds["global_timeout_sec"] * 1000) + _RECEIPT_TIME_TOLERANCE_MS:
        raise ValueError("provider-smoke receipt exceeds global timeout budget")
    if bounds["read_only"] is not True or bounds["canonical_fallback_used"] is not False:
        raise ValueError("provider-smoke read-only boundary failed")

    registry = receipt["registry"]
    current_registry = _registry_binding()
    if registry != current_registry:
        raise ValueError("provider registry binding mismatch")
    source_binding = receipt["source_binding"]
    expected_source_keys = {
        "runner", "runner_sha256", "probe_manifest", "probe_manifest_sha256",
        "baseline_logical_path", "baseline_sha256", "registry_source",
        "adapter_sources", "policy",
    }
    if not isinstance(source_binding, dict) or set(source_binding) != expected_source_keys:
        raise ValueError("provider-smoke source binding drifted")
    manifest = load_probe_manifest()
    expected_source = _source_binding(manifest)
    if source_binding != expected_source:
        raise ValueError("provider-smoke source/probe manifest binding mismatch")

    providers = receipt["providers"]
    if not isinstance(providers, list) or len(providers) != len(manifest["providers"]):
        raise ValueError("provider-smoke provider conclusions are incomplete")
    canonical_entries = {
        (entry["provider"], entry["intent"]): entry for entry in manifest["providers"]
    }
    case_keys = [
        (item.get("provider"), item.get("intent"))
        for item in providers
        if isinstance(item, Mapping)
    ]
    if len(case_keys) != len(set(case_keys)) or set(case_keys) != set(canonical_entries):
        raise ValueError("provider-smoke case universe drifted")
    if bounds["case_count"] != len(canonical_entries):
        raise ValueError("provider-smoke case count is not manifest-bound")
    for case in providers:
        if not isinstance(case, Mapping):
            raise ValueError("provider-smoke provider conclusion must be an object")
        entry = canonical_entries.get((case.get("provider"), case.get("intent")))
        if entry is None:
            raise ValueError("provider-smoke provider conclusion has no canonical entry")
        _validate_case(
            case,
            entry=entry,
            registry_names=set(PROVIDER_REGISTRY),
            receipt_started=receipt_started,
            receipt_completed=receipt_completed,
        )

    case_counts = receipt["case_counts"]
    expected_count_keys = {"total", *PROVIDER_SMOKE_STATUSES}
    if not isinstance(case_counts, dict) or set(case_counts) != expected_count_keys:
        raise ValueError("provider-smoke case counts drifted")
    if case_counts["total"] != len(providers):
        raise ValueError("provider-smoke total count mismatch")
    actual_counts = Counter(case["status"] for case in providers)
    for status in PROVIDER_SMOKE_STATUSES:
        if type(case_counts[status]) is not int or case_counts[status] != actual_counts.get(status, 0):
            raise ValueError("provider-smoke status count mismatch")
    if sum(case_counts[status] for status in PROVIDER_SMOKE_STATUSES) != case_counts["total"]:
        raise ValueError("provider-smoke status counts do not sum to total")

    external_pending = receipt["external_pending"]
    if external_pending != manifest["external_pending"]:
        raise ValueError("external provider channels must remain pending and out of registry")
    _validate_no_sensitive_values(receipt)


def _assert_no_symlink_components(path: Path) -> None:
    path = Path(os.path.abspath(os.fspath(path)))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise ValueError("provider-smoke output path contains a symlink")


def _canonicalize_system_alias(path: Path) -> Path:
    """Normalize macOS system aliases without following user-controlled links."""

    path = Path(os.path.abspath(os.fspath(path)))
    for alias, target in ((Path("/var"), Path("/private/var")),):
        try:
            if path == alias or alias in path.parents:
                if alias.is_symlink() and alias.resolve(strict=False) == target:
                    return target / path.relative_to(alias)
        except (OSError, ValueError):
            raise ValueError("provider-smoke output path has an unsafe system alias")
    return path


def _validate_output_root(
    output_root: Path | str, *, _test_only_output_root: bool = False
) -> Path:
    requested = _canonicalize_system_alias(Path(output_root))
    canonical = _canonicalize_system_alias(Path(OUTPUT_ROOT))
    if not _test_only_output_root and requested != canonical:
        raise ValueError("provider-smoke output root must be the canonical repository root")
    # Check the lexical path before creating or resolving anything.  This
    # rejects both intermediate symlinks and nominal/../escaped paths for the
    # public canonical seam.
    _assert_no_symlink_components(requested)
    if os.path.lexists(requested) and requested.is_symlink():
        raise ValueError("provider-smoke output root must not be a symlink")
    if requested.exists() and not requested.is_dir():
        raise ValueError("provider-smoke output root must be a real directory")
    requested.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_no_symlink_components(requested)
    if requested.is_symlink() or not requested.is_dir():
        raise ValueError("provider-smoke output root must be a real directory")
    os.chmod(requested, 0o700)
    return requested


def write_provider_receipt(
    receipt: Mapping[str, Any],
    *,
    output_root: Path | str = OUTPUT_ROOT,
    _test_only_output_root: bool = False,
) -> Path:
    validate_provider_receipt(receipt)
    if receipt["live"] is True and _test_only_output_root:
        raise ValueError("live provider-smoke receipts cannot use a private output root")
    if receipt["live"] is False and not _test_only_output_root:
        raise ValueError("fixture provider-smoke receipts require a private output root")
    requested_root = _canonicalize_system_alias(Path(output_root))
    canonical_root = _canonicalize_system_alias(Path(OUTPUT_ROOT))
    if receipt["live"] is False and requested_root == canonical_root:
        raise ValueError("fixture provider-smoke receipts cannot use the canonical output root")
    root = _validate_output_root(
        output_root, _test_only_output_root=_test_only_output_root
    )
    completed = datetime.fromisoformat(receipt["completed_at"])
    stamp = completed.strftime("%Y%m%dT%H%M%S%z")
    destination_dir = root / stamp
    _assert_no_symlink_components(destination_dir)
    if os.path.lexists(destination_dir) and destination_dir.is_symlink():
        raise ValueError("provider-smoke timestamp directory must not be a symlink")
    if os.path.lexists(destination_dir):
        raise ValueError("provider-smoke timestamp directory already exists")
    destination_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(destination_dir, 0o700)
    destination = destination_dir / "provider-smoke.v3.json"
    temporary = destination_dir / f".provider-smoke.v3.json.{os.getpid()}.{time.time_ns()}.tmp"
    _assert_no_symlink_components(destination)
    _assert_no_symlink_components(temporary)
    if os.path.lexists(temporary):
        raise ValueError("provider-smoke temporary file already exists")
    payload = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _assert_no_symlink_components(destination)
        if os.path.lexists(destination):
            raise ValueError("provider-smoke receipt destination already exists")
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()
    return destination


def _summary_for_cli(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "complete",
        "receipt": result["receipt"],
        "receipt_sha256": result["receipt_sha256"],
        "case_counts": result["case_counts"],
        "external_pending": len(result["external_pending"]),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--case-timeout", type=float, default=DEFAULT_CASE_TIMEOUT_SEC)
    parser.add_argument("--global-timeout", type=float, default=DEFAULT_GLOBAL_TIMEOUT_SEC)
    args = parser.parse_args(argv)
    try:
        result = run_provider_smoke(
            output_root=args.output_root,
            case_timeout_sec=args.case_timeout,
            global_timeout_sec=args.global_timeout,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "failed", "error": _safe_code(type(error).__name__, "PROVIDER_SMOKE_FAILED")}, ensure_ascii=False))
        return 2
    print(json.dumps(_summary_for_cli(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
