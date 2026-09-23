"""Evidence-bound, resumable StockToday method audit.

The audit deliberately consumes the existing StockToday provider.  It never
stores rows, upstream messages, credentials, or transport details in a
receipt; rows are used only in memory for deterministic checks and hashing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
from collections import Counter
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Mapping

from .contracts import TZ_SHANGHAI
from .providers.stocktoday import RequestBudget, StockTodayProvider
from .providers.stocktoday_auth import load_token
from .providers.stocktoday_inventory import (
    load_inventory,
    method_content_sha256,
    method_map,
    validate_inventory,
)
from .v3 import PROBE_MANIFEST_PATH


AUDIT_STATUSES = {
    "pass_nonempty",
    "reachable_empty",
    "auth_denied",
    "rate_limited",
    "timeout",
    "invalid_params",
    "schema_error",
    "semantic_fail",
    "provider_error",
    "not_run",
}
PRIMARY_ELIGIBLE_STATUSES = frozenset({"pass_nonempty"})
SCHEMA_VERSION = "3.1"
FIXED_DATE_RE = re.compile(r"^20\d{6}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
QUERY_SECRET_RE = re.compile(
    r"(?:[?&]|\b)(?:token|api[_-]?key|password|passwd|secret|authorization|auth)\s*=",
    re.IGNORECASE,
)
JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
CREDENTIAL_PREFIX_RE = re.compile(
    r"^(?:bearer(?:\s+|$)|sk[-_]|pk[-_]|gh[pousr]_|github_pat_|xox[a-z]*[-_]?|AKIA|AIza)",
    re.IGNORECASE,
)
OPAQUE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+/=-]*$")
HASH_KEY_RE = re.compile(r"(?:hash|sha256)$", re.IGNORECASE)
RECEIPT_BAD_KEY_RE = re.compile(
    r"(?:token|secret|password|credential|keychain|authorization|host.?override|"
    r"^host$|^url$|^uri$|endpoint|raw|message|exception|^rows$|"
    r"^api[_-]?key$|^private[_-]?key$|^access[_-]?token$|^refresh[_-]?token$|"
    r"^client[_-]?secret$|^client[_-]?id$|^bearer$|^auth$|"
    r"^auth[_-]?(?:token|key|header|credential)$)",
    re.IGNORECASE,
)
RECEIPT_SAFE_METADATA_KEY_RE = re.compile(
    r"^(?:auth_(?:required|status|type|mode|source)|token_(?:required|status|source))$",
    re.IGNORECASE,
)

PROVIDER_CODE_CONSISTENCY = {
    "pass_nonempty": "int:0",
    "reachable_empty": "int:0",
    "semantic_fail": "int:0",
    "schema_error": "int:0 or schema error code string",
    "auth_denied": "nonzero int upstream/http code or auth error code string",
    "rate_limited": "nonzero int upstream/http code or RATE_LIMITED",
    "timeout": "TIMEOUT",
    "invalid_params": "INVALID_PARAMS",
    "provider_error": "safe provider code string or int",
    "not_run": "null",
}
SCHEMA_ERROR_PROVIDER_CODES = frozenset(
    {"INVALID_RESPONSE", "INVALID_KLINE", "SYMBOL_MISMATCH", "RESPONSE_CONTAINS_SECRET"}
)
AUTH_PROVIDER_ERROR_CODES = frozenset(
    {"AUTH_MISSING", "AUTH_STORAGE_UNAVAILABLE", "AUTH_DENIED"}
)
NOT_RUN_REASONS = frozenset(
    {
        "auth_threshold",
        "rate_threshold",
        "budget_exhausted",
        "budget_wait_limit",
    }
)
MAX_BUDGET_WAIT_ATTEMPTS = 4
TRUSTED_REPO_ROOT = Path(__file__).resolve().parents[1]
APPROVED_OUTPUT_ROOT = TRUSTED_REPO_ROOT / "outputs"
APPROVED_SHARED_AUDIT_ROOT = Path("/Users/yimu/Projects/YM_Capital/shared/audits")
PACKAGED_PROBE_MANIFEST_SHA256 = "8778ddb7a9ddec43641d3f68a553a415e44c73a7b218e21d95a83d4f40a542a1"

RECEIPT_KEYS = {
    "schema_version",
    "inventory_sha256",
    "probe_manifest_sha256",
    "started",
    "ended",
    "cases",
    "counts",
    "classifications",
    "receipt_sha256",
}
CASE_KEYS = {
    "case_identity",
    "method",
    "probe_id",
    "category",
    "fixed_date",
    "sanitized_params",
    "requested_fields",
    "duplicate_key_fields",
    "expected_fields",
    "primary_declared",
    "primary_eligible",
    "declared_filter_checks",
    "pagination",
    "start",
    "end",
    "latency_ms",
    "row_count",
    "columns",
    "provider_code",
    "classification",
    "filter_checks",
    "duplicate_key_checks",
    "pagination_truncation_checks",
    "payload_hash",
    "payload_sha256",
}
_DUPLICATE_CHECK_KEYS = frozenset(
    {"status", "key_fields", "duplicate_count", "reason"}
)
_PAGINATION_CHECK_KEYS = frozenset(
    {
        "status",
        "total",
        "total_present",
        "limit",
        "offset",
        "reason",
        "truncation_policy",
    }
)
_FILTER_CHECK_STATUSES = frozenset({"pass", "fail", "not_observable_empty"})
_PAGINATION_POLICIES = frozenset(
    {"fail_if_total_exceeds_rows", "report_if_total_exceeds_rows"}
)
_PAGINATION_DECLARATION_KEYS = frozenset(
    {
        "total_field",
        "limit_field",
        "offset_field",
        "total_required",
        "truncation_policy",
    }
)
_FILTER_KINDS = frozenset(
    {
        "set_equal",
        "pattern",
        "scalar_equal",
        "date_equal",
        "date_range",
        "month_range",
    }
)
RESPONSE_SCALAR_FILTERS = frozenset(
    {
        "exchange",
        "idx_type",
        "topic",
        "name",
        "con_code",
        "theme_code",
        "fut_type",
        "call_put",
        "is_new",
        "is_open",
        "content_type",
        "market_type",
        "exchange_id",
        "bank",
        "country",
        "currency",
        "event",
        "src",
        "org",
        "ptype",
        "report_date",
        "level",
        "publisher",
        "category",
        "symbol",
        "index_code",
        "l1_code",
        "l2_code",
        "l3_code",
        "report_type",
        "comp_type",
    }
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _manifest_hash(payload: Mapping[str, Any]) -> str:
    content = dict(payload)
    content.pop("manifest_sha256", None)
    return _sha256(content)


def _is_safe_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _safe_tree(
    value: Any,
    *,
    context: str = "value",
    depth: int = 0,
    key: str | None = None,
) -> None:
    """Reject credentials, URLs, and unsafe object shapes recursively."""

    if depth > 8:
        raise ValueError("safe-value depth exceeded")
    if isinstance(value, dict):
        for field, child in value.items():
            if not isinstance(field, str):
                raise ValueError(f"{context} has a non-string key")
            if RECEIPT_BAD_KEY_RE.search(field) and not RECEIPT_SAFE_METADATA_KEY_RE.fullmatch(field):
                raise ValueError(f"{context} has a restricted key")
            _safe_tree(child, context=f"{context}.field", depth=depth + 1, key=field)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _safe_tree(child, context=f"{context}.item", depth=depth + 1, key=key)
        return
    if not _is_safe_scalar(value):
        raise ValueError(f"{context} has an unsafe value type")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{context} has a non-finite number")
    if isinstance(value, str):
        if URL_RE.search(value) or QUERY_SECRET_RE.search(value):
            raise ValueError(f"{context} contains a URL")
        if any(ord(char) < 32 for char in value):
            raise ValueError(f"{context} contains a control character")
        if len(value) > 4096:
            raise ValueError(f"{context} is too long")
        if (
            key is None
            or (
                not HASH_KEY_RE.search(key)
                and key != "case_identity"
            )
        ) and (
            CREDENTIAL_PREFIX_RE.match(value)
            or JWT_RE.fullmatch(value)
            or (
                len(value) >= 32
                and OPAQUE_VALUE_RE.fullmatch(value) is not None
                and re.search(r"[A-Za-z]", value) is not None
                and re.search(r"[0-9]", value) is not None
            )
        ):
            raise ValueError(f"{context} contains a credential-shaped value")


def validate_receipt_safety(receipt: Any) -> None:
    """Fail closed if a receipt tree contains secrets, URLs, or raw errors."""

    _safe_tree(receipt, context="receipt")


def _safe_code(value: Any, default: str = "PROVIDER_ERROR") -> str:
    candidate = str(value or "")
    return candidate if SAFE_CODE_RE.fullmatch(candidate) else default


def _provider_code_consistent(classification: str, provider_code: Any) -> bool:
    if classification in {"pass_nonempty", "reachable_empty", "semantic_fail"}:
        return type(provider_code) is int and provider_code == 0
    if classification == "schema_error":
        return (type(provider_code) is int and provider_code == 0) or (
            isinstance(provider_code, str) and provider_code in SCHEMA_ERROR_PROVIDER_CODES
        )
    if classification == "auth_denied":
        return (
            type(provider_code) is int and provider_code != 0
        ) or (isinstance(provider_code, str) and provider_code in AUTH_PROVIDER_ERROR_CODES)
    if classification == "rate_limited":
        return (
            type(provider_code) is int and provider_code != 0
        ) or provider_code == "RATE_LIMITED"
    if classification == "timeout":
        return provider_code == "TIMEOUT"
    if classification == "invalid_params":
        return provider_code == "INVALID_PARAMS"
    if classification == "provider_error":
        return (type(provider_code) is int) or (
            isinstance(provider_code, str) and SAFE_CODE_RE.fullmatch(provider_code) is not None
        )
    if classification == "not_run":
        return provider_code is None
    return False


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load StockToday audit manifest: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("StockToday audit manifest must be an object")
    return value


def _field_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError("requested fields must be a string or list")
    if not all(isinstance(item, str) and item.strip() for item in values):
        raise ValueError("requested fields must contain non-empty strings")
    result = [item.strip() for item in values]
    if len(result) != len(set(result)):
        raise ValueError("requested fields must be unique")
    return result


def _request_from_probe(probe: Mapping[str, Any]) -> dict:
    params = probe.get("params")
    if not isinstance(params, dict):
        raise ValueError("probe params must be an object")
    result = copy.deepcopy(params)
    _safe_tree(result, context="probe params")
    return result


def validate_probe_manifest(
    payload: dict,
    *,
    source_path: Path | None = None,
    inventory_payload: Mapping[str, Any] | None = None,
) -> dict:
    """Validate the canonical, credential-free probe manifest."""

    if not isinstance(payload, dict):
        raise ValueError("StockToday probe manifest must be an object")
    expected_keys = {
        "schema_version",
        "inventory_sha256",
        "manifest_sha256",
        "methods",
        "account_diagnostic",
    }
    if set(payload) != expected_keys:
        raise ValueError("StockToday probe manifest has missing or unknown fields")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported StockToday probe manifest schema")
    if not isinstance(payload.get("inventory_sha256"), str) or not SHA256_RE.fullmatch(payload["inventory_sha256"]):
        raise ValueError("invalid StockToday probe inventory hash")
    if not isinstance(payload.get("manifest_sha256"), str) or not SHA256_RE.fullmatch(payload["manifest_sha256"]):
        raise ValueError("invalid StockToday probe manifest hash")
    if payload["manifest_sha256"] != _manifest_hash(payload):
        raise ValueError("StockToday probe manifest hash mismatch")
    if source_path is None or Path(source_path).resolve() == PROBE_MANIFEST_PATH.resolve():
        if payload["manifest_sha256"] != PACKAGED_PROBE_MANIFEST_SHA256:
            raise ValueError("packaged StockToday probe manifest anchor mismatch")

    inventory = (
        validate_inventory(dict(inventory_payload))
        if inventory_payload is not None
        else load_inventory()
    )
    inventory_methods = {
        item["name"]: item for item in inventory["methods"]
    }
    methods = payload.get("methods")
    if not isinstance(methods, dict) or set(methods) != set(inventory_methods):
        raise ValueError("StockToday probe methods do not match frozen inventory")
    if payload["inventory_sha256"] != inventory["payload_sha256"]:
        raise ValueError("StockToday probe inventory hash mismatch")

    for method_name, inventory_item in inventory_methods.items():
        entries = methods.get(method_name)
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"StockToday method {method_name} has no probe")
        probe_ids: set[str] = set()
        for index, probe in enumerate(entries):
            if not isinstance(probe, dict):
                raise ValueError(f"StockToday method {method_name} probe is not an object")
            expected_probe_keys = {
                "probe_id",
                "source",
                "params",
                "fixed_date",
                "duplicate_key_fields",
                "pagination",
                "expected_fields",
                "filter_checks",
                "primary_declared",
            }
            if set(probe) != expected_probe_keys:
                raise ValueError(f"StockToday method {method_name} probe fields are invalid")
            probe_id = probe.get("probe_id")
            if not isinstance(probe_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", probe_id):
                raise ValueError(f"StockToday method {method_name} probe_id is invalid")
            if probe_id in probe_ids:
                raise ValueError(f"StockToday method {method_name} has duplicate probe_id")
            probe_ids.add(probe_id)
            if index == 0:
                expected_source = "vendor_example" if inventory_item["has_example"] else "fixed_parameters"
                if probe.get("source") != expected_source:
                    raise ValueError(f"StockToday method {method_name} first probe source is invalid")
            if not isinstance(probe.get("fixed_date"), str) or not FIXED_DATE_RE.fullmatch(probe["fixed_date"]):
                raise ValueError(f"StockToday method {method_name} fixed_date is invalid")
            request = _request_from_probe(probe)
            if request.get("api_name") != method_name or not isinstance(request.get("params"), dict):
                raise ValueError(f"StockToday method {method_name} request is invalid")
            requested_fields = _field_list(request.get("fields", ""))
            expected_fields = probe.get("expected_fields")
            if (
                not isinstance(expected_fields, list)
                or len(expected_fields) != len(set(expected_fields))
                or not all(isinstance(field, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", field) for field in expected_fields)
            ):
                raise ValueError(f"StockToday method {method_name} expected fields are invalid")
            if requested_fields and not set(requested_fields).issubset(expected_fields):
                raise ValueError(f"StockToday method {method_name} expected fields omit requested fields")
            filter_checks = probe.get("filter_checks")
            if not isinstance(filter_checks, list):
                raise ValueError(f"StockToday method {method_name} filter checks are invalid")
            for check in filter_checks:
                if not isinstance(check, dict) or set(check) - {"field", "kind", "response_fields"}:
                    raise ValueError(f"StockToday method {method_name} filter check is invalid")
                if not isinstance(check.get("field"), str) or check.get("kind") not in {
                    "set_equal",
                    "pattern",
                    "scalar_equal",
                    "date_equal",
                    "date_range",
                    "month_range",
                }:
                    raise ValueError(f"StockToday method {method_name} filter check is invalid")
                if "response_fields" in check and (
                    not isinstance(check["response_fields"], list)
                    or not check["response_fields"]
                    or not all(isinstance(field, str) for field in check["response_fields"])
                ):
                    raise ValueError(f"StockToday method {method_name} filter response fields are invalid")
            if inventory_item["has_example"] and index == 0:
                expected = dict(inventory_item["example"])
                example_fields = expected.pop("fields", "")
                if request["params"] != expected or request.get("fields", "") != example_fields:
                    raise ValueError(f"StockToday method {method_name} example probe drifted")
            if not inventory_item["has_example"] and index == 0:
                if (
                    not request.get("params")
                    and not request.get("fields")
                    and not (
                        expected_fields == []
                        and probe.get("filter_checks") == []
                        and probe.get("duplicate_key_fields") == []
                    )
                ):
                    raise ValueError(f"StockToday method {method_name} fixed probe is empty")
            keys = probe.get("duplicate_key_fields")
            if not isinstance(keys, list) or not all(isinstance(key, str) and key for key in keys):
                raise ValueError(f"StockToday method {method_name} duplicate key declaration is invalid")
            if len(keys) != len(set(keys)):
                raise ValueError(f"StockToday method {method_name} duplicate key declaration repeats fields")
            if not set(keys).issubset(expected_fields):
                raise ValueError(f"StockToday method {method_name} duplicate key declaration is outside expected fields")
            pagination = probe.get("pagination")
            if not _pagination_declaration_valid(pagination):
                raise ValueError(f"StockToday method {method_name} pagination declaration is invalid")
            primary = probe.get("primary_declared")
            if type(primary) is not bool:
                raise ValueError(f"StockToday method {method_name} primary declaration is invalid")
            if primary and (
                not expected_fields
                or not filter_checks
                or pagination["total_required"] is not True
            ):
                raise ValueError(f"StockToday method {method_name} primary declaration lacks complete evidence")

    diagnostic = payload.get("account_diagnostic")
    if not isinstance(diagnostic, dict) or set(diagnostic) != {"name", "kind", "primary_declared"}:
        raise ValueError("StockToday account diagnostic declaration is invalid")
    if diagnostic != {"name": "token_info", "kind": "account_diagnostic", "primary_declared": False}:
        raise ValueError("StockToday account diagnostic declaration drifted")
    return payload


def load_probes(path: Path = PROBE_MANIFEST_PATH) -> dict:
    path = Path(path)
    return validate_probe_manifest(_load_json(path), source_path=path)


def probe_manifest_sha256(path: Path = PROBE_MANIFEST_PATH) -> str:
    return load_probes(path)["manifest_sha256"]


def _now_iso(clock: Any) -> str:
    return datetime.fromtimestamp(clock.time(), TZ_SHANGHAI).isoformat(timespec="seconds")


def _parse_receipt_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip() or "T" not in value.upper():
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _requested_fields(params: Mapping[str, Any]) -> list[str]:
    return _field_list(params.get("fields", ""))


def _normalise_date(value: Any) -> str:
    text = str(value)
    return re.sub(r"[^0-9]", "", text)[:8]


def _normalise_month(value: Any) -> str:
    text = str(value)
    return re.sub(r"[^0-9]", "", text)[:6]


def _inferred_filter_checks(nested: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if nested.get("ts_code"):
        ts_code = str(nested["ts_code"])
        kind = "pattern" if any(marker in ts_code for marker in ("*", "?")) else "set_equal"
        checks.append({"field": "ts_code", "kind": kind})
    for field in ("trade_date", "date", "ann_date", "pub_date", "imp_date"):
        if nested.get(field):
            checks.append({"field": field, "kind": "date_equal"})
    if nested.get("start_date") or nested.get("end_date"):
        checks.append(
            {
                "field": "date_range",
                "kind": "date_range",
                "response_fields": ["trade_date", "date", "end_date", "ann_date", "pub_date"],
            }
        )
    if nested.get("m") or nested.get("start_m") or nested.get("end_m"):
        checks.append({"field": "month_range", "kind": "month_range", "response_fields": ["month"]})
    for field in (
        "limit_type",
        "type",
        "list_status",
        "classify",
        "market",
        "status",
        "tag",
        "hot_type",
        "year",
        "quarter",
        "period",
    ):
        if field in nested and nested[field] not in (None, ""):
            checks.append({"field": field, "kind": "scalar_equal"})
    existing = {check["field"] for check in checks}
    for field in sorted(RESPONSE_SCALAR_FILTERS & set(nested) - existing):
        if nested[field] not in (None, ""):
            checks.append({"field": field, "kind": "scalar_equal"})
    return checks


def _filter_checks(
    params: Mapping[str, Any], rows: list[dict], declared: Any = None
) -> dict:
    nested = params.get("params", {})
    if not isinstance(nested, dict):
        return {"status": "fail", "checks": {"params": "invalid"}}
    specs = declared if isinstance(declared, list) else _inferred_filter_checks(nested)
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, status: str, **details: Any) -> None:
        checks[name] = {"status": status, **details}

    if not rows:
        for spec in specs:
            field = spec.get("field")
            if isinstance(field, str) and (
                field in nested
                or field in {"date_range", "month_range"}
            ):
                record(field, "not_observable_empty")
        return {"status": "pass", "checks": checks}

    for spec in specs:
        field = spec.get("field")
        kind = spec.get("kind")
        if not isinstance(field, str) or not isinstance(kind, str):
            record(str(field), "fail", reason="invalid_declaration")
            continue
        if kind == "set_equal":
            expected = {item.strip() for item in str(nested.get(field, "")).split(",") if item.strip()}
            values = [row.get(field) for row in rows]
            if not expected or any(value is None or value not in expected for value in values):
                record(field, "fail", expected=sorted(expected), reason="response_field_missing_or_mismatch")
            else:
                record(field, "pass", expected=sorted(expected))
            continue
        if kind == "pattern":
            expected = [item.strip() for item in str(nested.get(field, "")).split(",") if item.strip()]
            values = [row.get(field) for row in rows]
            if not expected or any(
                value is None
                or not any(fnmatchcase(str(value), pattern) for pattern in expected)
                for value in values
            ):
                record(field, "fail", expected=expected, reason="response_field_missing_or_mismatch")
            else:
                record(field, "pass", expected=expected)
            continue
        if kind == "date_equal":
            expected = _normalise_date(nested.get(field))
            values = [_normalise_date(row.get(field)) for row in rows]
            if not expected or any(not value or value != expected for value in values):
                record(field, "fail", expected=expected, reason="response_field_missing_or_mismatch")
            else:
                record(field, "pass", expected=expected)
            continue
        if kind == "scalar_equal":
            expected = nested.get(field)
            values = [row.get(field) for row in rows]
            if any(value is None or str(value) != str(expected) for value in values):
                record(field, "fail", expected=expected, reason="response_field_missing_or_mismatch")
            else:
                record(field, "pass", expected=expected)
            continue
        if kind == "date_range":
            bounds = {key: nested.get(key) for key in ("start_date", "end_date") if nested.get(key)}
            response_fields = spec.get("response_fields") or ["trade_date", "date", "end_date", "ann_date", "pub_date"]
            values = []
            for row in rows:
                value = next((row.get(candidate) for candidate in response_fields if row.get(candidate) is not None), None)
                values.append(_normalise_date(value))
            if any(not value for value in values):
                record(field, "fail", reason="response_date_field_missing")
            elif any(bounds.get("start_date") and value < _normalise_date(bounds["start_date"]) for value in values) or any(bounds.get("end_date") and value > _normalise_date(bounds["end_date"]) for value in values):
                record(field, "fail", expected={key: _normalise_date(value) for key, value in bounds.items()})
            else:
                record(field, "pass", expected={key: _normalise_date(value) for key, value in bounds.items()})
            continue
        if kind == "month_range":
            bounds = {key: nested.get(key) for key in ("start_m", "end_m") if nested.get(key)}
            if nested.get("m"):
                bounds = {"start_m": nested["m"], "end_m": nested["m"]}
            response_fields = spec.get("response_fields") or ["month"]
            values = []
            for row in rows:
                value = next((row.get(candidate) for candidate in response_fields if row.get(candidate) is not None), None)
                values.append(_normalise_month(value))
            if any(not value for value in values):
                record(field, "fail", reason="response_month_field_missing")
            elif any(bounds.get("start_m") and value < _normalise_month(bounds["start_m"]) for value in values) or any(bounds.get("end_m") and value > _normalise_month(bounds["end_m"]) for value in values):
                record(field, "fail", expected={key: _normalise_month(value) for key, value in bounds.items()})
            else:
                record(field, "pass", expected={key: _normalise_month(value) for key, value in bounds.items()})

    status = "fail" if any(item["status"] == "fail" for item in checks.values()) else "pass"
    return {"status": status, "checks": checks}


def _duplicate_checks(rows: list[dict], key_fields: Any) -> dict:
    if not key_fields:
        return {
            "status": "not_applicable",
            "key_fields": [],
            "duplicate_count": 0,
            "reason": None,
        }
    if not isinstance(key_fields, list) or not all(isinstance(key, str) and key for key in key_fields):
        return {
            "status": "fail",
            "key_fields": [],
            "duplicate_count": 0,
            "reason": "invalid_key_fields",
        }
    seen: Counter[tuple[Any, ...]] = Counter()
    for row in rows:
        if any(field not in row for field in key_fields):
            return {
                "status": "fail",
                "key_fields": key_fields,
                "duplicate_count": 0,
                "reason": "key_field_missing",
            }
        try:
            seen[tuple(row[field] for field in key_fields)] += 1
        except TypeError:
            return {
                "status": "fail",
                "key_fields": key_fields,
                "duplicate_count": 0,
                "reason": "key_value_unhashable",
            }
    duplicate_count = sum(count - 1 for count in seen.values() if count > 1)
    return {
        "status": "fail" if duplicate_count else "pass",
        "key_fields": key_fields,
        "duplicate_count": duplicate_count,
        "reason": "duplicate_keys" if duplicate_count else None,
    }


def _pagination_declaration_valid(
    declaration: Any, *, require_total: bool = False
) -> bool:
    if not isinstance(declaration, Mapping) or set(declaration) != _PAGINATION_DECLARATION_KEYS:
        return False
    if (
        declaration.get("total_field") != "total"
        or declaration.get("limit_field") != "limit"
        or declaration.get("offset_field") != "offset"
        or type(declaration.get("total_required")) is not bool
        or declaration.get("truncation_policy") not in _PAGINATION_POLICIES
    ):
        return False
    if not declaration["total_required"] and declaration["truncation_policy"] != "fail_if_total_exceeds_rows":
        return False
    return not require_total or declaration["total_required"] is True


def _pagination_policy(declaration: Any) -> str:
    if isinstance(declaration, Mapping):
        value = declaration.get("truncation_policy")
        if value in _PAGINATION_POLICIES:
            return value
    return "fail_if_total_exceeds_rows"


def _pagination_checks(
    params: Mapping[str, Any], data: Mapping[str, Any], row_count: int, declaration: Any = None
) -> dict:
    nested = params.get("params", {})
    declaration = declaration if isinstance(declaration, dict) else {}
    truncation_policy = _pagination_policy(declaration)
    limit = nested.get("limit")
    offset = nested.get("offset", 0)
    total_present = data.get("total_present") if isinstance(data.get("total_present"), bool) else data.get("total") is not None
    total = data.get("total") if total_present else None
    if not total_present:
        if row_count == 0 or declaration.get("total_required", True):
            return {
                "status": "unknown",
                "total": None,
                "total_present": False,
                "limit": limit,
                "offset": offset,
                "reason": "total_missing",
                "truncation_policy": truncation_policy,
            }
        return {
            "status": "not_applicable",
            "total": None,
            "total_present": False,
            "limit": limit,
            "offset": offset,
            "reason": None,
            "truncation_policy": truncation_policy,
        }
    if type(total) is not int or total < row_count:
        return {
            "status": "fail",
            "total": total,
            "total_present": total_present,
            "limit": limit,
            "offset": offset,
            "reason": "total_less_than_rows" if type(total) is int else "invalid_total",
            "truncation_policy": truncation_policy,
        }
    if type(offset) is not int or offset < 0:
        return {
            "status": "fail",
            "total": total,
            "total_present": total_present,
            "limit": limit,
            "offset": offset,
            "reason": "invalid_offset",
            "truncation_policy": truncation_policy,
        }
    if limit is not None and (type(limit) is not int or limit < 0):
        return {
            "status": "fail",
            "total": total,
            "total_present": total_present,
            "limit": limit,
            "offset": offset,
            "reason": "invalid_limit",
            "truncation_policy": truncation_policy,
        }
    truncated = total > row_count
    if limit is not None and row_count > limit:
        truncated = True
    if offset and row_count and offset >= total:
        return {
            "status": "fail",
            "total": total,
            "total_present": total_present,
            "limit": limit,
            "offset": offset,
            "reason": "offset_out_of_range",
            "truncation_policy": truncation_policy,
        }
    return {
        "status": "truncated" if truncated else "pass",
        "total": total,
        "total_present": total_present,
        "limit": limit,
        "offset": offset,
        "reason": "total_exceeds_rows" if truncated else None,
        "truncation_policy": truncation_policy,
    }


def classify(observation: Mapping[str, Any]) -> str:
    """Classify a sanitized in-memory observation into the frozen set."""

    error_code = str(observation.get("error_code") or "")
    provenance = observation.get("provenance") if isinstance(observation.get("provenance"), Mapping) else {}
    http_status = provenance.get("http_status")
    upstream_code = provenance.get("upstream_code")
    if http_status in {401, 403}:
        return "auth_denied"
    if http_status == 429:
        return "rate_limited"
    if upstream_code in {401, 403, 40203, -2001, -2002}:
        return "auth_denied"
    if upstream_code in {429, -429}:
        return "rate_limited"
    if error_code in {"AUTH_MISSING", "AUTH_STORAGE_UNAVAILABLE", "AUTH_DENIED"}:
        return "auth_denied"
    if error_code in {"RATE_LIMITED"}:
        return "rate_limited"
    if error_code in {"TIMEOUT"}:
        return "timeout"
    if error_code in {"INVALID_PARAMS"}:
        return "invalid_params"
    if error_code in {"INVALID_RESPONSE", "INVALID_KLINE", "SYMBOL_MISMATCH", "RESPONSE_CONTAINS_SECRET"}:
        return "schema_error"
    if error_code:
        return "provider_error"
    if observation.get("schema_status") == "fail":
        return "schema_error"
    filters = observation.get("filter_checks") or {}
    duplicates = observation.get("duplicate_key_checks") or {}
    pagination = observation.get("pagination_truncation_checks") or {}
    if filters.get("status") == "fail" or duplicates.get("status") == "fail":
        return "semantic_fail"
    rows = observation.get("rows")
    row_count = observation.get("row_count", len(rows) if isinstance(rows, list) else 0)
    if row_count == 0 or rows == []:
        return "reachable_empty"
    if pagination.get("status") in {"fail", "truncated", "unknown"}:
        return "semantic_fail"
    return "pass_nonempty"


def _declared_filter_fields(declared: Any) -> list[str] | None:
    if not isinstance(declared, list):
        return None
    fields: list[str] = []
    for item in declared:
        if (
            not isinstance(item, Mapping)
            or set(item) - {"field", "kind", "response_fields"}
            or not isinstance(item.get("field"), str)
            or item.get("kind") not in _FILTER_KINDS
        ):
            return None
        field = item["field"]
        if not field or field in fields:
            return None
        fields.append(field)
    return fields


def _filter_checks_match_declaration(
    declared: Any, checks: Any, *, require_pass: bool = False
) -> bool:
    fields = _declared_filter_fields(declared)
    if fields is None or not isinstance(checks, Mapping):
        return False
    if set(checks) != {"status", "checks"} or not isinstance(checks.get("checks"), Mapping):
        return False
    status = checks.get("status")
    if status not in {"pass", "fail", "not_applicable"}:
        return False
    actual = checks["checks"]
    if status == "not_applicable":
        return not actual and not require_pass
    if set(actual) != set(fields):
        return False
    statuses: list[str] = []
    for field in fields:
        item = actual.get(field)
        if not isinstance(item, Mapping) or set(item) - {"status", "expected", "reason"}:
            return False
        item_status = item.get("status")
        if item_status not in _FILTER_CHECK_STATUSES:
            return False
        statuses.append(item_status)
    if require_pass and any(item_status != "pass" for item_status in statuses):
        return False
    if status == "pass" and any(item_status == "fail" for item_status in statuses):
        return False
    if status == "fail" and not any(item_status == "fail" for item_status in statuses):
        return False
    return True


def _duplicate_checks_valid(
    checks: Any,
    declared_fields: Any = None,
    *,
    columns: Any = None,
    expected_fields: Any = None,
    require_primary: bool = False,
) -> bool:
    if not isinstance(checks, Mapping) or set(checks) != _DUPLICATE_CHECK_KEYS:
        return False
    status = checks.get("status")
    if status not in {"pass", "fail", "not_applicable"}:
        return False
    fields = checks.get("key_fields")
    if not isinstance(fields, list) or not all(
        isinstance(field, str) and field for field in fields
    ):
        return False
    if len(fields) != len(set(fields)):
        return False
    if declared_fields is not None:
        if status == "not_applicable":
            if require_primary and declared_fields != []:
                return False
        elif fields != declared_fields:
            return False
    if columns is not None and (
        not isinstance(columns, list) or not set(fields).issubset(columns)
    ):
        return False
    if expected_fields is not None and (
        not isinstance(expected_fields, list)
        or not set(fields).issubset(expected_fields)
    ):
        return False
    count = checks.get("duplicate_count")
    if type(count) is not int or count < 0:
        return False
    reason = checks.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason):
        return False
    if status == "not_applicable":
        return fields == [] and count == 0 and reason is None
    if status == "pass":
        return count == 0 and reason is None
    return bool(reason)


def _pagination_checks_valid(
    checks: Any, declaration: Any = None, *, require_primary: bool = False
) -> bool:
    if not _pagination_declaration_valid(declaration, require_total=require_primary):
        return False
    if not isinstance(checks, Mapping) or set(checks) != _PAGINATION_CHECK_KEYS:
        return False
    status = checks.get("status")
    if status not in {"pass", "fail", "truncated", "unknown", "not_applicable"}:
        return False
    total_present = checks.get("total_present")
    if type(total_present) is not bool:
        return False
    total = checks.get("total")
    if total is not None and (type(total) is not int or total < 0):
        return False
    for field in ("limit", "offset"):
        value = checks.get(field)
        if value is not None and (type(value) is not int or value < 0):
            return False
    reason = checks.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason):
        return False
    expected_policy = _pagination_policy(declaration)
    if checks.get("truncation_policy") != expected_policy:
        return False
    if status == "pass":
        if not total_present or total is None or reason is not None:
            return False
    elif status == "truncated":
        if not total_present or total is None or reason is None:
            return False
    elif status == "unknown":
        if total_present or total is not None or reason is None:
            return False
    elif status == "not_applicable":
        if total_present or total is not None or reason is not None:
            return False
    elif reason is None:
        return False
    return not require_primary or status == "pass"


def primary_eligible(
    classification: str,
    *,
    method: str | None = None,
    category: str | None = None,
    primary_declared: bool = False,
    expected_fields: Any = None,
    columns: Any = None,
    declared_filter_checks: Any = None,
    filter_checks: Any = None,
    duplicate_key_fields: Any = None,
    duplicate_key_checks: Any = None,
    pagination_truncation_checks: Any = None,
    pagination: Any = None,
) -> bool:
    if method == "token_info" or category == "account_diagnostic":
        return False
    if classification not in PRIMARY_ELIGIBLE_STATUSES or primary_declared is not True:
        return False
    if not isinstance(expected_fields, list) or not expected_fields:
        return False
    if not all(isinstance(field, str) and field for field in expected_fields):
        return False
    if len(expected_fields) != len(set(expected_fields)):
        return False
    if not isinstance(columns, list) or not all(
        isinstance(column, str) for column in columns
    ):
        return False
    if len(columns) != len(set(columns)) or not set(expected_fields).issubset(columns):
        return False
    if not _filter_checks_match_declaration(
        declared_filter_checks, filter_checks, require_pass=True
    ):
        return False
    if not _pagination_declaration_valid(pagination, require_total=True):
        return False
    if not isinstance(duplicate_key_fields, list) or not all(
        isinstance(field, str) and field for field in duplicate_key_fields
    ):
        return False
    if len(duplicate_key_fields) != len(set(duplicate_key_fields)):
        return False
    if (
        not set(duplicate_key_fields).issubset(expected_fields)
        or not set(duplicate_key_fields).issubset(columns)
    ):
        return False
    if not isinstance(declared_filter_checks, list) or not declared_filter_checks:
        return False
    if not _duplicate_checks_valid(
        duplicate_key_checks,
        duplicate_key_fields,
        columns=columns,
        expected_fields=expected_fields,
        require_primary=True,
    ):
        return False
    if not _pagination_checks_valid(
        pagination_truncation_checks, pagination, require_primary=True
    ):
        return False
    return True


def _extract_data(
    outcome: Any, *, requested_fields: list[str] | None = None, expected_fields: list[str] | None = None
) -> tuple[list[dict], list[str], dict, Any]:
    data = outcome.data if isinstance(getattr(outcome, "data", None), dict) else {}
    raw_rows = data.get("items", [])
    fields = data.get("fields", [])
    if not isinstance(raw_rows, list) or not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        return [], [], data, "schema"
    rows: list[dict] = []
    for row in raw_rows:
        if not isinstance(row, dict) or any(not isinstance(key, str) for key in row):
            return [], [], data, "schema"
        rows.append(row)
    if len(fields) != len(set(fields)) or any(field not in row for row in rows for field in fields):
        return [], [], data, "schema"
    required = requested_fields or expected_fields or []
    if any(field not in fields for field in required):
        return [], [], data, "schema"

    def finite_tree(value: Any) -> bool:
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, dict):
            return all(isinstance(key, str) and finite_tree(child) for key, child in value.items())
        if isinstance(value, list):
            return all(finite_tree(child) for child in value)
        return isinstance(value, (str, int, bool)) or value is None

    if not finite_tree(rows):
        return [], [], data, "schema"
    return rows, fields, data, None


def _payload_hash(provider_code: Any, columns: list[str], rows: list[dict], total: Any) -> str:
    shape = {
        "provider_code": provider_code,
        "columns": columns,
        "row_count": len(rows),
        "total": total,
        "rows": rows,
    }
    return _sha256(shape)


def _case_identity(case: Mapping[str, Any]) -> str:
    return _sha256(
        {
            "method": case.get("method"),
            "probe_id": case.get("probe_id"),
            "category": case.get("category"),
            "fixed_date": case.get("fixed_date"),
            "params": case.get("params"),
            "duplicate_key_fields": case.get("duplicate_key_fields", []),
            "expected_fields": case.get("expected_fields", []),
            "filter_checks": case.get("filter_checks", []),
            "pagination": case.get("pagination", {}),
            "primary_declared": case.get("primary_declared", False),
        }
    )


def _default_pagination_declaration(*, total_required: bool) -> dict[str, Any]:
    return {
        "total_field": "total",
        "limit_field": "limit",
        "offset_field": "offset",
        "total_required": total_required,
        "truncation_policy": "fail_if_total_exceeds_rows",
    }


def _normalise_case(case: Mapping[str, Any]) -> dict:
    if not isinstance(case, Mapping):
        raise ValueError("audit case must be an object")
    if set(case) - {
        "method",
        "probe_id",
        "category",
        "params",
        "fixed_date",
        "duplicate_key_fields",
        "filter_checks",
        "expected_fields",
        "pagination",
        "case_identity",
        "primary_declared",
    }:
        raise ValueError("audit case has unknown fields")
    method = case.get("method")
    probe_id = case.get("probe_id")
    params = case.get("params")
    if not isinstance(method, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", method):
        raise ValueError("audit case method is invalid")
    if not isinstance(probe_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", probe_id):
        raise ValueError("audit case probe_id is invalid")
    if not isinstance(params, dict):
        raise ValueError("audit case params are invalid")
    _safe_tree(params, context="audit case params")
    result = copy.deepcopy(dict(case))
    if "filter_checks" not in result:
        nested = params.get("params", {})
        result["filter_checks"] = _inferred_filter_checks(nested) if isinstance(nested, dict) else []
    result.setdefault("category", "unknown")
    if method == "token_info":
        result["category"] = "account_diagnostic"
        if params.get("api_name") != "token_info" or params.get("params") not in ({}, None):
            raise ValueError("token_info diagnostic params are invalid")
        if set(params) - {"api_name", "params", "fields"}:
            raise ValueError("token_info diagnostic params are invalid")
        result.setdefault("pagination", _default_pagination_declaration(total_required=False))
    result.setdefault("fixed_date", "20250930")
    if not isinstance(result["fixed_date"], str) or not FIXED_DATE_RE.fullmatch(result["fixed_date"]):
        raise ValueError("audit case fixed_date is invalid")
    result.setdefault("duplicate_key_fields", [])
    result.setdefault("filter_checks", [])
    result.setdefault("expected_fields", [])
    result.setdefault("pagination", _default_pagination_declaration(total_required=True))
    result.setdefault("primary_declared", False)
    if (
        not isinstance(result["filter_checks"], list)
        or not isinstance(result["expected_fields"], list)
        or not _pagination_declaration_valid(result["pagination"])
    ):
        raise ValueError("audit case expectations are invalid")
    if type(result["primary_declared"]) is not bool:
        raise ValueError("audit case primary declaration is invalid")
    result["case_identity"] = _case_identity(result)
    return result


def default_cases() -> list[dict]:
    inventory = method_map()
    probes = load_probes()
    cases = []
    for method_name in sorted(probes["methods"]):
        for probe in probes["methods"][method_name]:
            case = {
                "method": method_name,
                "probe_id": probe["probe_id"],
                "category": inventory[method_name]["category"],
                "params": copy.deepcopy(probe["params"]),
                "fixed_date": probe["fixed_date"],
                "duplicate_key_fields": copy.deepcopy(probe["duplicate_key_fields"]),
                "filter_checks": copy.deepcopy(probe["filter_checks"]),
                "expected_fields": copy.deepcopy(probe["expected_fields"]),
                "pagination": copy.deepcopy(probe["pagination"]),
                "primary_declared": probe["primary_declared"],
            }
            cases.append(_normalise_case(case))
    diagnostic = probes["account_diagnostic"]
    cases.append(
        _normalise_case(
            {
                "method": diagnostic["name"],
                "probe_id": "token-info",
                "category": diagnostic["kind"],
                "params": {"api_name": "token_info", "params": {}, "fields": ""},
                "fixed_date": "20250930",
                "duplicate_key_fields": [],
                "filter_checks": [],
                "expected_fields": ["account_status"],
                "primary_declared": diagnostic["primary_declared"],
            }
        )
    )
    return cases


def _empty_checks(declaration: Any = None) -> tuple[dict, dict, dict]:
    return (
        {"status": "not_applicable", "checks": {}},
        {
            "status": "not_applicable",
            "key_fields": [],
            "duplicate_count": 0,
            "reason": None,
        },
        {
            "status": "not_applicable",
            "total": None,
            "total_present": False,
            "limit": None,
            "offset": None,
            "reason": None,
            "truncation_policy": _pagination_policy(declaration),
        },
    )


def _receipt_case(
    case: Mapping[str, Any],
    *,
    start: str,
    end: str,
    latency_ms: int,
    provider_code: Any,
    classification: str,
    columns: list[str],
    rows: list[dict],
    total: Any,
    filter_checks: dict,
    duplicate_key_checks: dict,
    pagination_checks: dict,
    error_code: str | None = None,
    not_run_reason: str | None = None,
) -> dict:
    params = copy.deepcopy(case["params"])
    _safe_tree(params, context="receipt params")
    result = {
        "case_identity": case["case_identity"],
        "method": case["method"],
        "probe_id": case["probe_id"],
        "category": str(case.get("category", "unknown")),
        "fixed_date": case["fixed_date"],
        "sanitized_params": params,
        "requested_fields": _requested_fields(params),
        "duplicate_key_fields": copy.deepcopy(case.get("duplicate_key_fields", [])),
        "expected_fields": copy.deepcopy(case.get("expected_fields", [])),
        "primary_declared": bool(case.get("primary_declared", False)),
        "primary_eligible": primary_eligible(
            classification,
            method=case.get("method"),
            category=case.get("category"),
            primary_declared=case.get("primary_declared", False),
            expected_fields=case.get("expected_fields", []),
            columns=columns,
            declared_filter_checks=case.get("filter_checks", []),
            filter_checks=filter_checks,
            duplicate_key_fields=case.get("duplicate_key_fields", []),
            duplicate_key_checks=duplicate_key_checks,
            pagination_truncation_checks=pagination_checks,
            pagination=case.get("pagination", {}),
        ),
        "declared_filter_checks": copy.deepcopy(case.get("filter_checks", [])),
        "pagination": copy.deepcopy(case.get("pagination", {})),
        "start": start,
        "end": end,
        "latency_ms": max(0, int(latency_ms)),
        "row_count": len(rows),
        "columns": list(columns),
        "provider_code": provider_code if provider_code is None or (type(provider_code) is int) or isinstance(provider_code, str) else "PROVIDER_ERROR",
        "classification": classification,
        "filter_checks": filter_checks,
        "duplicate_key_checks": duplicate_key_checks,
        "pagination_truncation_checks": pagination_checks,
        "payload_hash": _payload_hash(provider_code, columns, rows, total),
    }
    result["payload_sha256"] = result["payload_hash"]
    if error_code and not (type(provider_code) is int):
        result["provider_code"] = _safe_code(error_code)
    if not_run_reason:
        if not_run_reason not in NOT_RUN_REASONS:
            raise ValueError("unstable not_run reason")
        result["not_run_reason"] = not_run_reason
    validate_receipt_safety(result)
    return result


def _outcome_case(case: Mapping[str, Any], outcome: Any, *, start: str, end: str, latency_ms: int) -> dict:
    error_code = getattr(outcome, "error_code", None)
    if error_code == "LOCAL_BUDGET_EXHAUSTED":
        raise RuntimeError("budget exhausted")
    provenance = getattr(outcome, "provenance", None)
    provenance = provenance if isinstance(provenance, Mapping) else {}
    upstream_code = provenance.get("upstream_code") if type(provenance.get("upstream_code")) is int else 0
    http_status = provenance.get("http_status") if type(provenance.get("http_status")) is int else 0
    if getattr(outcome, "status", "") in {"success", "empty"}:
        provider_code: Any = upstream_code
    elif upstream_code:
        provider_code = upstream_code
    elif http_status:
        provider_code = http_status
    else:
        provider_code = _safe_code(error_code)
    if error_code and error_code not in {"INVALID_RESPONSE", "INVALID_KLINE", "SYMBOL_MISMATCH"}:
        rows, columns, data, schema_error = [], [], {}, None
    else:
        rows, columns, data, schema_error = _extract_data(
            outcome,
            requested_fields=_requested_fields(case["params"]),
            expected_fields=case.get("expected_fields") or None,
        )
    if schema_error or error_code in {"INVALID_RESPONSE", "INVALID_KLINE", "SYMBOL_MISMATCH"}:
        filters, duplicates, pagination = _empty_checks(case.get("pagination"))
        observation = {
            "error_code": "INVALID_RESPONSE",
            "provider_code": provider_code,
            "rows": [],
            "row_count": 0,
            "provenance": provenance,
        }
        classification = "schema_error"
        total = None
    elif error_code:
        filters, duplicates, pagination = _empty_checks(case.get("pagination"))
        observation = {
            "error_code": error_code,
            "provider_code": provider_code,
            "rows": [],
            "row_count": 0,
            "provenance": provenance,
        }
        classification = classify(observation)
        total = None
        rows = []
        columns = []
    else:
        filters = _filter_checks(case["params"], rows, case.get("filter_checks"))
        duplicates = _duplicate_checks(rows, case.get("duplicate_key_fields", []))
        pagination = _pagination_checks(case["params"], data, len(rows), case.get("pagination"))
        observation = {
            "provider_code": provider_code,
            "rows": rows,
            "row_count": len(rows),
            "columns": columns,
            "total": data.get("total"),
            "filter_checks": filters,
            "duplicate_key_checks": duplicates,
            "pagination_truncation_checks": pagination,
            "provenance": provenance,
        }
        classification = classify(observation)
        total = data.get("total")
    return _receipt_case(
        case,
        start=start,
        end=end,
        latency_ms=latency_ms,
        provider_code=provider_code,
        classification=classification,
        columns=columns,
        rows=rows,
        total=total,
        filter_checks=filters,
        duplicate_key_checks=duplicates,
        pagination_checks=pagination,
        error_code=error_code,
    )


def _not_run_case(case: Mapping[str, Any], *, clock: Any, reason: str) -> dict:
    start = _now_iso(clock)
    return _receipt_case(
        case,
        start=start,
        end=start,
        latency_ms=0,
        provider_code=None,
        classification="not_run",
        columns=[],
        rows=[],
        total=None,
        filter_checks={"status": "not_applicable", "checks": {}},
        duplicate_key_checks={
            "status": "not_applicable",
            "key_fields": [],
            "duplicate_count": 0,
            "reason": None,
        },
        pagination_checks={
            "status": "not_applicable",
            "total": None,
            "total_present": False,
            "limit": None,
            "offset": None,
            "reason": None,
            "truncation_policy": _pagination_policy(case.get("pagination")),
        },
        not_run_reason=reason,
    )


def _counts(cases: list[dict]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(AUDIT_STATUSES)}
    for case in cases:
        classification = case.get("classification")
        if classification not in AUDIT_STATUSES:
            raise ValueError("receipt has an unknown classification")
        counts[classification] += 1
    return counts


def _canonical_probe_index(receipt: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Return current canonical probes, rejecting any manifest drift."""

    manifest = load_probes()
    if receipt.get("probe_manifest_sha256") != manifest.get("manifest_sha256"):
        raise ValueError("receipt probe manifest hash does not bind current canonical manifest")
    methods = manifest.get("methods")
    if not isinstance(methods, Mapping):
        raise ValueError("current canonical probe manifest methods are invalid")
    index: dict[tuple[str, str], Mapping[str, Any]] = {}
    for method, probes in methods.items():
        if not isinstance(method, str) or not isinstance(probes, list):
            raise ValueError("current canonical probe manifest index is invalid")
        for probe in probes:
            if not isinstance(probe, Mapping) or not isinstance(probe.get("probe_id"), str):
                raise ValueError("current canonical probe manifest entry is invalid")
            index[(method, probe["probe_id"])] = probe
    return index


def _validate_receipt_case(
    item: Any,
    *,
    receipt_started: datetime | None = None,
    receipt_ended: datetime | None = None,
    canonical_probe: Mapping[str, Any] | None = None,
) -> None:
    if not isinstance(item, dict):
        raise ValueError("receipt case must be an object")
    allowed = CASE_KEYS | {"not_run_reason"}
    if set(item) - allowed or not CASE_KEYS.issubset(item):
        raise ValueError("receipt case has missing or unknown fields")
    if not isinstance(item.get("case_identity"), str) or not SHA256_RE.fullmatch(item["case_identity"]):
        raise ValueError("receipt case identity is invalid")
    for field in ("method", "probe_id"):
        if not isinstance(item.get(field), str):
            raise ValueError("receipt case identity fields are invalid")
    if not isinstance(item.get("category"), str) or not isinstance(item.get("fixed_date"), str) or not FIXED_DATE_RE.fullmatch(item["fixed_date"]):
        raise ValueError("receipt case metadata is invalid")
    params = item.get("sanitized_params")
    if not isinstance(params, dict):
        raise ValueError("receipt case parameters are invalid")
    if item.get("requested_fields") != _requested_fields(params):
        raise ValueError("receipt case requested fields drifted")
    duplicate_fields = item.get("duplicate_key_fields")
    if not isinstance(duplicate_fields, list) or not all(
        isinstance(field, str) and field for field in duplicate_fields
    ):
        raise ValueError("receipt case duplicate key declaration is invalid")
    if len(duplicate_fields) != len(set(duplicate_fields)):
        raise ValueError("receipt case duplicate key declaration is invalid")
    expected_fields = item.get("expected_fields")
    declared_filters = item.get("declared_filter_checks")
    declared_pagination = item.get("pagination")
    if (
        not isinstance(expected_fields, list)
        or not all(isinstance(field, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", field) for field in expected_fields)
        or not isinstance(declared_filters, list)
        or not _pagination_declaration_valid(declared_pagination)
    ):
        raise ValueError("receipt case declarations are invalid")
    if len(expected_fields) != len(set(expected_fields)):
        raise ValueError("receipt case declarations are invalid")
    if type(item.get("primary_declared")) is not bool or type(item.get("primary_eligible")) is not bool:
        raise ValueError("receipt case primary declaration or eligibility is invalid")
    if canonical_probe is not None:
        for case_field, probe_field in (
            ("fixed_date", "fixed_date"),
            ("duplicate_key_fields", "duplicate_key_fields"),
            ("expected_fields", "expected_fields"),
            ("declared_filter_checks", "filter_checks"),
            ("pagination", "pagination"),
            ("primary_declared", "primary_declared"),
        ):
            if item.get(case_field) != canonical_probe.get(probe_field):
                raise ValueError("receipt case declaration drifted from canonical probe")
    identity_case = {
        "method": item["method"],
        "probe_id": item["probe_id"],
        "category": item["category"],
        "params": params,
        "fixed_date": item["fixed_date"],
        "duplicate_key_fields": duplicate_fields,
        "expected_fields": item["expected_fields"],
        "filter_checks": item["declared_filter_checks"],
        "pagination": item["pagination"],
        "primary_declared": item["primary_declared"],
    }
    if item["case_identity"] != _case_identity(identity_case):
        raise ValueError("receipt case identity hash mismatch")
    case_started = _parse_receipt_timestamp(item.get("start"), field="receipt case start")
    case_ended = _parse_receipt_timestamp(item.get("end"), field="receipt case end")
    if case_started > case_ended:
        raise ValueError("receipt case timestamps are reversed")
    if receipt_started is not None and case_started < receipt_started:
        raise ValueError("receipt case starts before receipt")
    if receipt_ended is not None and case_ended > receipt_ended:
        raise ValueError("receipt case ends after receipt")
    if type(item.get("latency_ms")) is not int or item["latency_ms"] < 0 or type(item.get("row_count")) is not int or item["row_count"] < 0:
        raise ValueError("receipt case counts are invalid")
    columns = item.get("columns")
    if not isinstance(columns, list) or not all(isinstance(column, str) for column in columns):
        raise ValueError("receipt case columns are invalid")
    if len(columns) != len(set(columns)):
        raise ValueError("receipt case columns are invalid")
    classification = item.get("classification")
    provider_code = item.get("provider_code")
    if not set(duplicate_fields).issubset(expected_fields):
        raise ValueError("receipt case duplicate key declaration is outside declared columns")
    schema_unverifiable = classification == "schema_error"
    if not schema_unverifiable and not set(duplicate_fields).issubset(columns):
        raise ValueError("receipt case duplicate key declaration is outside declared columns")
    if schema_unverifiable:
        duplicate_checks = item.get("duplicate_key_checks")
        if not isinstance(duplicate_checks, Mapping) or duplicate_checks.get("status") != "not_applicable":
            raise ValueError("schema-error receipt duplicate checks must be not_applicable")
    if provider_code is not None and type(provider_code) is not int and not isinstance(provider_code, str):
        raise ValueError("receipt case provider code is invalid")
    if item.get("classification") not in AUDIT_STATUSES:
        raise ValueError("receipt case classification is invalid")
    if not _provider_code_consistent(classification, provider_code):
        raise ValueError(
            f"receipt provider code {provider_code!r} is inconsistent with {classification}"
        )
    if not isinstance(item.get("filter_checks"), dict) or not isinstance(item.get("duplicate_key_checks"), dict) or not isinstance(item.get("pagination_truncation_checks"), dict):
        raise ValueError("receipt case checks are invalid")
    filter_status = item["filter_checks"].get("status")
    duplicate_status = item["duplicate_key_checks"].get("status")
    pagination = item["pagination_truncation_checks"]
    if not _filter_checks_match_declaration(
        declared_filters, item["filter_checks"]
    ):
        raise ValueError("receipt case filter checks do not match declaration")
    if not _duplicate_checks_valid(
        item["duplicate_key_checks"],
        duplicate_fields,
        columns=columns,
        expected_fields=expected_fields,
    ):
        raise ValueError("receipt case duplicate check shape is invalid")
    if not _pagination_checks_valid(pagination, declared_pagination):
        raise ValueError("receipt case pagination check shape is invalid")
    if not isinstance(item.get("payload_hash"), str) or not SHA256_RE.fullmatch(item["payload_hash"]):
        raise ValueError("receipt case payload hash is invalid")
    if item.get("payload_sha256") != item.get("payload_hash"):
        raise ValueError("receipt case payload hash aliases differ")
    computed_primary = primary_eligible(
        item["classification"],
        method=item["method"],
        category=item["category"],
        primary_declared=item["primary_declared"],
        expected_fields=item["expected_fields"],
        columns=item["columns"],
        declared_filter_checks=item["declared_filter_checks"],
        filter_checks=item["filter_checks"],
        duplicate_key_fields=item["duplicate_key_fields"],
        duplicate_key_checks=item["duplicate_key_checks"],
        pagination_truncation_checks=item["pagination_truncation_checks"],
        pagination=item["pagination"],
    )
    if item["primary_eligible"] != computed_primary:
        raise ValueError("receipt case primary declaration is not supported by its evidence")
    classification = item["classification"]
    if classification == "pass_nonempty":
        pagination_ok = pagination.get("status") == "pass" or (
            pagination.get("status") == "not_applicable"
            and (
                item.get("category") == "account_diagnostic"
                or item.get("pagination", {}).get("total_required") is False
            )
        )
        if (
            item["row_count"] <= 0
            or not _filter_checks_match_declaration(
                declared_filters, item["filter_checks"], require_pass=True
            )
            or duplicate_status == "fail"
            or not pagination_ok
        ):
            raise ValueError("receipt pass classification is not supported by its checks")
        if item["provider_code"] in {401, 403, 429, 40203, -2001, -2002}:
            raise ValueError("receipt pass classification conflicts with provider code")
    elif classification == "reachable_empty" and item["row_count"] != 0:
        raise ValueError("receipt empty classification has rows")
    elif classification == "semantic_fail":
        if not (
            filter_status == "fail"
            or duplicate_status == "fail"
            or pagination.get("status") in {"fail", "truncated", "unknown"}
        ):
            raise ValueError("receipt semantic classification has no failed check")
    elif classification == "not_run" and item["row_count"] != 0:
        raise ValueError("receipt not_run classification has rows")
    if classification == "not_run":
        if item.get("not_run_reason") not in NOT_RUN_REASONS:
            raise ValueError("receipt case not_run reason is invalid")
    elif "not_run_reason" in item:
        raise ValueError("non-not_run receipt case has a not_run reason")


def validate_receipt(receipt: Any) -> dict:
    """Validate receipt shape, case identities, counts and self-excluding hash."""

    validate_receipt_safety(receipt)
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise ValueError("audit receipt has missing or unknown fields")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported audit receipt schema")
    for field in ("inventory_sha256", "probe_manifest_sha256", "receipt_sha256"):
        if not isinstance(receipt.get(field), str) or not SHA256_RE.fullmatch(receipt[field]):
            raise ValueError(f"audit receipt {field} is invalid")
    receipt_started = _parse_receipt_timestamp(
        receipt.get("started"), field="audit receipt started"
    )
    receipt_ended = _parse_receipt_timestamp(
        receipt.get("ended"), field="audit receipt ended"
    )
    if receipt_started > receipt_ended:
        raise ValueError("audit receipt timestamps are reversed")
    cases = receipt.get("cases")
    if not isinstance(cases, list):
        raise ValueError("audit receipt cases are invalid")
    canonical_probes = _canonical_probe_index(receipt)
    identities = set()
    for item in cases:
        canonical_probe = None
        if isinstance(item, Mapping) and canonical_probes is not None:
            canonical_probe = canonical_probes.get(
                (item.get("method"), item.get("probe_id"))
            )
        _validate_receipt_case(
            item,
            receipt_started=receipt_started,
            receipt_ended=receipt_ended,
            canonical_probe=canonical_probe,
        )
        if item["case_identity"] in identities:
            raise ValueError("audit receipt contains duplicate case identity")
        identities.add(item["case_identity"])
    counts = _counts(cases)
    if receipt.get("counts") != counts:
        raise ValueError("audit receipt counts drifted")
    expected_classifications = sorted(status for status, count in counts.items() if count)
    if receipt.get("classifications") != expected_classifications:
        raise ValueError("audit receipt classifications drifted")
    unsigned = copy.deepcopy(receipt)
    unsigned.pop("receipt_sha256")
    if receipt["receipt_sha256"] != _sha256(unsigned):
        raise ValueError("audit receipt hash mismatch")
    return receipt


def _build_receipt(cases: list[dict], *, inventory_sha: str, manifest_sha: str, started: str, ended: str) -> dict:
    counts = _counts(cases)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "inventory_sha256": inventory_sha,
        "probe_manifest_sha256": manifest_sha,
        "started": started,
        "ended": ended,
        "cases": cases,
        "counts": counts,
        "classifications": sorted(status for status, count in counts.items() if count),
    }
    receipt["receipt_sha256"] = _sha256(receipt)
    validate_receipt(receipt)
    return receipt


def atomic_write_json(path: Path, value: dict) -> None:
    """Write a 0600 JSON checkpoint with fsync and atomic replace."""

    target = validate_output_path(Path(path))
    allowed_root = None
    for candidate_root in (APPROVED_OUTPUT_ROOT, APPROVED_SHARED_AUDIT_ROOT):
        try:
            target.relative_to(Path(candidate_root))
        except ValueError:
            continue
        allowed_root = Path(candidate_root)
        break
    if allowed_root is None:
        raise ValueError("audit output is outside the approved audit directories")

    # The root is a trusted constant; all user-controlled descendants are
    # created and opened relative to its directory fd below.
    allowed_root.mkdir(parents=True, exist_ok=True)
    if _path_has_symlink_below(allowed_root, target.parent):
        raise ValueError("audit output cannot use symlink components")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    root_fd = os.open(allowed_root, directory_flags)
    open_fds = [root_fd]
    directory_fd = root_fd
    temporary_name = None
    fd = None
    try:
        relative_parent = target.parent.relative_to(allowed_root)
        for part in relative_parent.parts:
            try:
                os.mkdir(part, 0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            open_fds.append(child_fd)
            directory_fd = child_fd
        for _ in range(8):
            candidate_name = f".{target.name}.tmp-{secrets.token_hex(8)}"
            try:
                fd = os.open(
                    candidate_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                    0o600,
                    dir_fd=directory_fd,
                )
                temporary_name = candidate_name
                break
            except FileExistsError:
                continue
        if fd is None or temporary_name is None:
            raise OSError("unable to create audit checkpoint temporary file")
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if _path_has_symlink_below(target.parent.parent, target.parent) or (target.exists() and target.is_symlink()):
            raise ValueError("audit output path changed during atomic write")
        os.replace(
            temporary_name,
            target.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        final_fd = os.open(target.name, os.O_RDONLY | nofollow, dir_fd=directory_fd)
        try:
            final_stat = os.fstat(final_fd)
            if not stat.S_ISREG(final_stat.st_mode):
                raise ValueError("audit output is not a regular file")
            os.fchmod(final_fd, stat.S_IRUSR | stat.S_IWUSR)
            os.fsync(final_fd)
        finally:
            os.close(final_fd)
        os.fsync(directory_fd)
    except Exception:
        if fd is not None:
            os.close(fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        for opened_fd in reversed(open_fds):
            try:
                os.close(opened_fd)
            except OSError:
                pass


def _path_has_symlink_below(base: Path, path: Path) -> bool:
    """Check user-controlled components without rejecting system aliases such as /var."""

    base = Path(base)
    path = Path(path)
    try:
        relative = path.relative_to(base)
    except ValueError:
        return False
    current = base
    if current.is_symlink():
        return True
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def validate_output_path(path: Path, *, repo_root: Path | None = None) -> Path:
    # ``repo_root`` is retained for CLI compatibility but is deliberately not
    # trusted: a caller cannot expand the approved write surface by supplying
    # an arbitrary repository root.
    root = TRUSTED_REPO_ROOT
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.absolute()
    if candidate.suffix.lower() != ".json":
        raise ValueError("audit output must be a JSON file")
    allowed_roots = (APPROVED_OUTPUT_ROOT, APPROVED_SHARED_AUDIT_ROOT)
    resolved = candidate.resolve(strict=False)
    if candidate.exists() and candidate.is_dir():
        raise ValueError("audit output must be a file")
    for allowed in allowed_roots:
        allowed_resolved = allowed.resolve(strict=False)
        try:
            resolved.relative_to(allowed_resolved)
        except ValueError:
            continue
        if _path_has_symlink_below(allowed, candidate):
            raise ValueError("audit output cannot use symlink components")
        return candidate
    raise ValueError("audit output is outside the approved audit directories")


def _load_resume(resume: Mapping[str, Any] | Path) -> dict:
    if isinstance(resume, (str, Path)):
        value = _load_json(Path(resume))
    elif isinstance(resume, Mapping):
        value = copy.deepcopy(dict(resume))
    else:
        raise ValueError("resume must be a receipt object or path")
    return validate_receipt(value)


def _validate_resume(value: dict, cases: list[dict], inventory_sha: str, manifest_sha: str) -> dict[str, dict]:
    if value.get("inventory_sha256") != inventory_sha:
        raise ValueError("resume inventory hash mismatch")
    if value.get("probe_manifest_sha256") != manifest_sha:
        raise ValueError("resume probe manifest hash mismatch")
    previous = value.get("cases")
    if not isinstance(previous, list):
        raise ValueError("resume receipt cases are invalid")
    previous_map: dict[str, dict] = {}
    for item in previous:
        if not isinstance(item, dict) or not isinstance(item.get("case_identity"), str):
            raise ValueError("resume case identity is missing")
        if item["case_identity"] in previous_map:
            raise ValueError("resume receipt contains duplicate case identity")
        if item.get("classification") not in AUDIT_STATUSES:
            raise ValueError("resume receipt contains unknown classification")
        previous_map[item["case_identity"]] = item
    current_ids = [_case["case_identity"] for _case in cases]
    if len(current_ids) != len(set(current_ids)):
        raise ValueError("audit cases contain duplicate identity")
    if not set(previous_map).issubset(set(current_ids)):
        raise ValueError("resume receipt case identity does not match requested cases")
    current_by_id = {item["case_identity"]: item for item in cases}
    for identity, item in previous_map.items():
        current = current_by_id[identity]
        if (
            item.get("method") != current.get("method")
            or item.get("probe_id") != current.get("probe_id")
            or item.get("category") != current.get("category")
            or item.get("fixed_date") != current.get("fixed_date")
            or item.get("sanitized_params") != current.get("params")
        ):
            raise ValueError("resume receipt case identity does not match requested case")
    return previous_map


def _wait_for_budget(budget: Any, clock: Any, sleeper: Any, waits: int) -> bool:
    if waits >= MAX_BUDGET_WAIT_ATTEMPTS:
        return False
    wait_seconds = None
    method = getattr(budget, "wait_seconds", None)
    if callable(method):
        wait_seconds = method(now=clock.time())
    if wait_seconds is None:
        return False
    try:
        wait_seconds = float(wait_seconds)
    except (TypeError, ValueError):
        return False
    if wait_seconds <= 0:
        wait_seconds = 1.0
    sleeper(wait_seconds)
    return True


def run_audit(
    *,
    cases: list[dict] | None = None,
    transport: Any = None,
    token_loader=load_token,
    budget: Any = None,
    clock: Any = time,
    sleeper=None,
    resume: Mapping[str, Any] | Path | None = None,
    output: Path | None = None,
    repo_root: Path | None = None,
    stop_threshold: int = 2,
) -> dict:
    """Run cases sequentially, checkpointing after every case."""

    if type(stop_threshold) is not int or stop_threshold < 1:
        raise ValueError("stop_threshold must be a positive integer")
    requested_cases = default_cases() if cases is None else [_normalise_case(case) for case in cases]
    inventory = load_inventory()
    inventory_sha = inventory["payload_sha256"]
    manifest_sha = probe_manifest_sha256()
    if output is not None:
        output = validate_output_path(Path(output), repo_root=repo_root)
    resume_value = _load_resume(resume) if resume is not None else None
    previous = _validate_resume(resume_value, requested_cases, inventory_sha, manifest_sha) if resume_value else {}
    sleeper = sleeper or time.sleep
    budget = budget or RequestBudget()
    provider = StockTodayProvider(
        token_loader=token_loader,
        post=transport,
        budget=budget,
        clock=clock,
    )
    started = resume_value.get("started") if resume_value else _now_iso(clock)
    results: list[dict] = []
    auth_consecutive = 0
    rate_consecutive = 0
    stop_reason: str | None = None

    for case in requested_cases:
        prior = previous.get(case["case_identity"])
        if prior is not None and prior.get("classification") != "not_run":
            result = copy.deepcopy(prior)
        elif stop_reason:
            result = _not_run_case(case, clock=clock, reason=stop_reason)
        else:
            start_mono = clock.monotonic()
            start = _now_iso(clock)
            waits = 0
            while True:
                try:
                    if case["method"] == "token_info":
                        outcome = provider.call_account_diagnostic()
                    else:
                        outcome = provider.call("stocktoday_data", case["params"])
                except ValueError:
                    outcome = None
                    result = _receipt_case(
                        case,
                        start=start,
                        end=_now_iso(clock),
                        latency_ms=int((clock.monotonic() - start_mono) * 1000),
                        provider_code="INVALID_PARAMS",
                        classification="invalid_params",
                        columns=[],
                        rows=[],
                        total=None,
                        filter_checks={"status": "not_applicable", "checks": {}},
                        duplicate_key_checks={
                            "status": "not_applicable",
                            "key_fields": [],
                            "duplicate_count": 0,
                            "reason": None,
                        },
                        pagination_checks={
                            "status": "not_applicable",
                            "total": None,
                            "total_present": False,
                            "limit": None,
                            "offset": None,
                            "reason": None,
                            "truncation_policy": _pagination_policy(case.get("pagination")),
                        },
                        error_code="INVALID_PARAMS",
                    )
                    break
                except Exception:
                    result = _receipt_case(
                        case,
                        start=start,
                        end=_now_iso(clock),
                        latency_ms=int((clock.monotonic() - start_mono) * 1000),
                        provider_code="PROVIDER_ERROR",
                        classification="provider_error",
                        columns=[],
                        rows=[],
                        total=None,
                        filter_checks={"status": "not_applicable", "checks": {}},
                        duplicate_key_checks={
                            "status": "not_applicable",
                            "key_fields": [],
                            "duplicate_count": 0,
                            "reason": None,
                        },
                        pagination_checks={
                            "status": "not_applicable",
                            "total": None,
                            "total_present": False,
                            "limit": None,
                            "offset": None,
                            "reason": None,
                            "truncation_policy": _pagination_policy(case.get("pagination")),
                        },
                        error_code="PROVIDER_ERROR",
                    )
                    break
                if getattr(outcome, "error_code", None) == "LOCAL_BUDGET_EXHAUSTED":
                    if _wait_for_budget(budget, clock, sleeper, waits):
                        waits += 1
                        continue
                    reason = "budget_exhausted" if waits == 0 else "budget_wait_limit"
                    result = _not_run_case(case, clock=clock, reason=reason)
                    break
                result = _outcome_case(
                    case,
                    outcome,
                    start=start,
                    end=_now_iso(clock),
                    latency_ms=int((clock.monotonic() - start_mono) * 1000),
                )
                break

            classification = result["classification"]
            if classification == "auth_denied":
                auth_consecutive += 1
                rate_consecutive = 0
                if auth_consecutive >= stop_threshold:
                    stop_reason = "auth_threshold"
            elif classification == "rate_limited":
                rate_consecutive += 1
                auth_consecutive = 0
                if rate_consecutive >= stop_threshold:
                    stop_reason = "rate_threshold"
            else:
                auth_consecutive = 0
                rate_consecutive = 0
            if classification == "not_run" and result.get("not_run_reason") in {"budget_exhausted", "budget_wait_limit"}:
                stop_reason = result["not_run_reason"]
        results.append(result)
        checkpoint = _build_receipt(
            results,
            inventory_sha=inventory_sha,
            manifest_sha=manifest_sha,
            started=started,
            ended=_now_iso(clock),
        )
        if output is not None:
            atomic_write_json(output, checkpoint)

    return _build_receipt(
        results,
        inventory_sha=inventory_sha,
        manifest_sha=manifest_sha,
        started=started,
        ended=_now_iso(clock),
    )


def inventory_summary() -> dict:
    inventory = load_inventory()
    methods = inventory["methods"]
    return {
        "status": "complete",
        "method_count": len(methods),
        "methods_with_examples": sum(item["has_example"] for item in methods),
        "methods_without_examples": sum(not item["has_example"] for item in methods),
        "inventory_sha256": inventory["payload_sha256"],
    }


def audit_status(path: Path) -> dict:
    value = _load_resume(Path(path))
    cases = value.get("cases")
    if not isinstance(cases, list):
        raise ValueError("audit receipt cases are invalid")
    counts = _counts(cases)
    return {
        "counts": counts,
        "classifications": sorted(status for status, count in counts.items() if count),
    }


__all__ = [
    "AUDIT_STATUSES",
    "PRIMARY_ELIGIBLE_STATUSES",
    "PROBE_MANIFEST_PATH",
    "audit_status",
    "atomic_write_json",
    "classify",
    "default_cases",
    "inventory_summary",
    "load_probes",
    "primary_eligible",
    "probe_manifest_sha256",
    "run_audit",
    "validate_output_path",
    "validate_probe_manifest",
    "validate_receipt",
    "validate_receipt_safety",
]
