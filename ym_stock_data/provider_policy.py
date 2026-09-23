"""Fail-closed V3 provider policy loading and evidence binding.

The packaged policy is a declarative routing proposal.  It can only become
active when its referenced StockToday audit receipt is a valid schema 3.1
receipt, is tied to the current local inventory and probe manifest, and
contains capability-specific primary evidence.  Receipt hashes are local
self-consistency checks; they are not signatures.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .routing import (
    EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
    EMPTY_POLICY_STOP,
    RouteSpec,
    capability_for,
    route_for,
)


POLICY_VERSION = "3.0"
PIPELINE_VERSION = "3.0"
ROUTE_POLICY_VERSION = "3.0"
POLICY_PATH = Path(__file__).resolve().parent / "v3" / "provider-policy.v3.json"
# Update this code-side approval anchor together with the canonical policy.
PACKAGED_POLICY_SHA256 = "c2c8362d1914a97a499c6fd0c71553fe5bf090346183e7bfb7812ddcf759ca88"
_DEFAULT_AUDIT_RECEIPT_PATH = (
    Path(__file__).resolve().parent / "v3" / "stocktoday-audit-receipt.v3.json"
)
AUDIT_RECEIPT_PATH = _DEFAULT_AUDIT_RECEIPT_PATH
# Descriptive alias for callers reviewing the policy/runtime boundary.
REVIEWED_RECEIPT_PATH = AUDIT_RECEIPT_PATH

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROVIDER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
CLASSIFICATIONS = frozenset(
    {
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
)
PRIMARY_CLASSIFICATION = "pass_nonempty"
EMPTY_POLICIES = frozenset(
    {EMPTY_POLICY_STOP, EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED}
)
CAPABILITIES = frozenset(
    {
        "stocktoday_data",
        "stock_snapshot",
        "stock_kline_daily",
        "stock_kline_weekly",
        "stock_kline_monthly",
        "stock_kline_60m",
        "stock_kline_15m",
        "stock_kline_5m",
        "sector_index",
        "review_sentiment",
        "market_limit_state",
    }
)

# These are the only providers whose runtime adapters implement the
# corresponding canonical route.  This is deliberately code-side: a policy
# file cannot introduce a new provider or borrow a semantically unrelated
# adapter merely by naming it in provider_order.
_CAPABILITY_PROVIDER_ALLOWLIST = {
    "stocktoday_data": frozenset({"stocktoday"}),
    "stock_snapshot": frozenset(
        {"stocktoday", "pytdx", "tencent", "tdx_quotes"}
    ),
    "stock_kline_daily": frozenset(
        {"stocktoday", "eastmoney_stock", "pytdx", "tencent", "tdx_kline"}
    ),
    "stock_kline_weekly": frozenset(
        {"stocktoday", "eastmoney_stock", "pytdx", "tencent", "tdx_kline"}
    ),
    "stock_kline_monthly": frozenset(
        {"stocktoday", "eastmoney_stock", "pytdx", "tencent", "tdx_kline"}
    ),
    "stock_kline_60m": frozenset({"stocktoday", "pytdx", "sina", "tdx_kline"}),
    "stock_kline_15m": frozenset({"stocktoday", "pytdx", "sina", "tdx_kline"}),
    "stock_kline_5m": frozenset({"stocktoday", "pytdx", "sina", "tdx_kline"}),
    "sector_index": frozenset({"ths_industry"}),
    # This is the default breadth route.  A non-empty natural-language query
    # is handled by route_for() independently and never inherits this order.
    "review_sentiment": frozenset(
        {"pytdx_breadth", "eastmoney_breadth", "eastmoney_limit_pool"}
    ),
    "market_limit_state": frozenset({"eastmoney_limit_pool"}),
}
_BASE_MAX_AGE_SEC = {
    "stocktoday_data": 60,
    "stock_snapshot": 60,
    "stock_kline_daily": 86400,
    "stock_kline_weekly": 86400,
    "stock_kline_monthly": 86400,
    "stock_kline_60m": 300,
    "stock_kline_15m": 300,
    "stock_kline_5m": 300,
    "sector_index": 300,
    "review_sentiment": 300,
    "market_limit_state": 300,
}

_POLICY_KEYS = frozenset(
    {
        "policy_version",
        "pipeline_version",
        "route_policy_version",
        "active",
        "expires_at",
        "inventory_sha256",
        "probe_manifest_sha256",
        "audit_receipt_sha256",
        "capabilities",
    }
)
_CAPABILITY_KEYS = frozenset(
    {
        "provider_order",
        "empty_policy",
        "max_age_sec",
        "minimum_audit_classification",
        "audit_evidence",
    }
)
_EVIDENCE_KEYS = frozenset(
    {
        "case_identity",
        "method",
        "classification",
        "primary_declared",
        "primary_eligible",
    }
)


class PolicyError(ValueError):
    """Base class for malformed or unusable provider policy documents."""


class PolicySchemaError(PolicyError):
    """Raised when a policy document violates the strict V3 schema."""


class PolicyEvidenceError(PolicyError):
    """Raised when active policy evidence cannot be bound to a receipt."""


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


def _policy_logical_sha256(policy: Mapping[str, Any]) -> str:
    """Hash the complete schema-normalised policy, independent of JSON layout."""

    return _sha256(_validate_document(policy))


def _require_sha(value: Any, field: str, *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PolicySchemaError(f"{field} must be a lowercase SHA-256 hash")


def _parse_expiry(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PolicySchemaError("expires_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicySchemaError("expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PolicySchemaError("expires_at must include a timezone")
    return parsed


def _validate_evidence_shape(value: Any, *, capability: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PolicySchemaError(f"{capability}.audit_evidence must be a list")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _EVIDENCE_KEYS:
            raise PolicySchemaError(
                f"{capability}.audit_evidence has invalid fields"
            )
        if not isinstance(item["case_identity"], str) or not SHA256_RE.fullmatch(
            item["case_identity"]
        ):
            raise PolicySchemaError(f"{capability}.audit_evidence case_identity is invalid")
        if not isinstance(item["method"], str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{1,63}", item["method"]
        ):
            raise PolicySchemaError(f"{capability}.audit_evidence method is invalid")
        if item["classification"] not in CLASSIFICATIONS:
            raise PolicySchemaError(
                f"{capability}.audit_evidence classification is invalid"
            )
        if type(item["primary_declared"]) is not bool or type(
            item["primary_eligible"]
        ) is not bool:
            raise PolicySchemaError(
                f"{capability}.audit_evidence primary flags are invalid"
            )
        result.append(copy.deepcopy(item))
    return result


def _validate_document(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict) or set(policy) != _POLICY_KEYS:
        raise PolicySchemaError("provider policy has missing or unknown fields")
    for field, expected in (
        ("policy_version", POLICY_VERSION),
        ("pipeline_version", PIPELINE_VERSION),
        ("route_policy_version", ROUTE_POLICY_VERSION),
    ):
        if policy.get(field) != expected:
            raise PolicySchemaError(f"unsupported {field}")
    if type(policy.get("active")) is not bool:
        raise PolicySchemaError("active must be a boolean")
    _parse_expiry(policy.get("expires_at"))
    _require_sha(policy.get("inventory_sha256"), "inventory_sha256")
    _require_sha(policy.get("probe_manifest_sha256"), "probe_manifest_sha256")
    _require_sha(policy.get("audit_receipt_sha256"), "audit_receipt_sha256")

    capabilities = policy.get("capabilities")
    if not isinstance(capabilities, dict) or not capabilities:
        raise PolicySchemaError("capabilities must be a non-empty object")
    unknown_capabilities = set(capabilities) - CAPABILITIES
    if unknown_capabilities:
        raise PolicySchemaError(
            "unknown capabilities: " + ", ".join(sorted(unknown_capabilities))
        )
    normalised_capabilities: dict[str, dict[str, Any]] = {}
    for capability, config in capabilities.items():
        if not isinstance(config, dict) or set(config) != _CAPABILITY_KEYS:
            raise PolicySchemaError(f"{capability} has missing or unknown fields")
        order = config.get("provider_order")
        if (
            not isinstance(order, list)
            or not order
            or any(
                not isinstance(provider, str) or not PROVIDER_NAME_RE.fullmatch(provider)
                for provider in order
            )
            or len(order) != len(set(order))
        ):
            raise PolicySchemaError(f"{capability}.provider_order is invalid")
        allowed_providers = _CAPABILITY_PROVIDER_ALLOWLIST.get(capability)
        if allowed_providers is None or any(
            provider not in allowed_providers for provider in order
        ):
            raise PolicySchemaError(
                f"{capability}.provider_order contains an incompatible provider"
            )
        if config.get("empty_policy") not in EMPTY_POLICIES:
            raise PolicySchemaError(f"{capability}.empty_policy is invalid")
        max_age = config.get("max_age_sec")
        if (
            type(max_age) is not int
            or max_age < 1
            or max_age > _BASE_MAX_AGE_SEC[capability]
        ):
            raise PolicySchemaError(f"{capability}.max_age_sec is invalid")
        if config.get("minimum_audit_classification") not in CLASSIFICATIONS:
            raise PolicySchemaError(
                f"{capability}.minimum_audit_classification is invalid"
            )
        normalised_capabilities[capability] = {
            "provider_order": list(order),
            "empty_policy": config["empty_policy"],
            "max_age_sec": max_age,
            "minimum_audit_classification": config[
                "minimum_audit_classification"
            ],
            "audit_evidence": _validate_evidence_shape(
                config["audit_evidence"], capability=capability
            ),
        }
    normalised = copy.deepcopy(policy)
    normalised["capabilities"] = normalised_capabilities
    return normalised


def _inactive(
    policy: Mapping[str, Any] | None = None, *, reason: str | None = None
) -> "CompiledPolicy":
    return CompiledPolicy(
        document=copy.deepcopy(dict(policy)) if isinstance(policy, Mapping) else None,
        policy_status="inactive",
        policy_evidence_sha256=None,
        inactive_reason=reason,
    )


_CAPABILITY_METHODS = {
    "stock_snapshot": frozenset({"rt_k"}),
    "stock_kline_daily": frozenset({"daily"}),
    "stock_kline_weekly": frozenset({"weekly"}),
    "stock_kline_monthly": frozenset({"monthly"}),
    "stock_kline_60m": frozenset({"stk_mins"}),
    "stock_kline_15m": frozenset({"stk_mins"}),
    "stock_kline_5m": frozenset({"stk_mins"}),
    "sector_index": frozenset(
        {"ths_index", "ci_index_member", "index_classify", "index_daily", "index_dailybasic", "sw_daily"}
    ),
    "review_sentiment": frozenset(
        {"limit_list_d", "limit_list_ths", "limit_cpt_list", "ths_hot", "dc_hot"}
    ),
    "market_limit_state": frozenset(
        {"limit_list_d", "limit_list_ths", "limit_cpt_list"}
    ),
    # Generic stocktoday_data is intentionally not an evidence-promotion
    # capability.  Its explicit route remains available through V2.
    "stocktoday_data": frozenset(),
}
_MINUTE_CAPABILITY_FREQ = {
    "stock_kline_60m": "60MIN",
    "stock_kline_15m": "15MIN",
    "stock_kline_5m": "5MIN",
}
_RUNTIME_COLUMNS = {
    "stock_snapshot": (
        frozenset({"ts_code", "close"}),
        frozenset({"updated_at", "trade_time"}),
    ),
    "stock_kline_daily": (
        frozenset({"ts_code", "trade_date", "open", "high", "low", "close"}),
        frozenset(),
    ),
    "stock_kline_weekly": (
        frozenset({"ts_code", "trade_date", "open", "high", "low", "close"}),
        frozenset(),
    ),
    "stock_kline_monthly": (
        frozenset({"ts_code", "trade_date", "open", "high", "low", "close"}),
        frozenset(),
    ),
    "stock_kline_60m": (
        frozenset({"ts_code", "open", "high", "low", "close"}),
        frozenset({"trade_time", "trade_date"}),
    ),
    "stock_kline_15m": (
        frozenset({"ts_code", "open", "high", "low", "close"}),
        frozenset({"trade_time", "trade_date"}),
    ),
    "stock_kline_5m": (
        frozenset({"ts_code", "open", "high", "low", "close"}),
        frozenset({"trade_time", "trade_date"}),
    ),
}


def _normalise_frequency(value: Any) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    if text.endswith("MIN"):
        return text
    if text.endswith("MINUTE"):
        return text[:-6] + "MIN"
    if text.endswith("M"):
        return text[:-1] + "MIN"
    return text


def _runtime_columns_match(capability: str, columns: Any) -> bool:
    """Check the minimum runtime columns for an automatic StockToday route."""

    spec = _RUNTIME_COLUMNS.get(capability)
    if spec is None or not isinstance(columns, list):
        return False
    if not all(isinstance(column, str) for column in columns):
        return False
    available = set(columns)
    required, one_of = spec
    return required.issubset(available) and (
        not one_of or bool(one_of.intersection(available))
    )


def _capability_case_matches(
    capability: str,
    case: Mapping[str, Any],
    *,
    inventory: Mapping[str, Any] | None = None,
    probe_manifest: Mapping[str, Any] | None = None,
) -> bool:
    """Match receipt evidence using code-side method and parameter rules.

    Receipt category is only a cross-check against the frozen inventory.  It
    is never allowed to turn a daily or one-minute method into another
    capability.  Unknown capabilities are denied by default.
    """

    methods = _CAPABILITY_METHODS.get(capability)
    if not methods:
        return False
    method = str(case.get("method") or "").strip()
    if method not in methods:
        return False

    if inventory is None:
        from .providers.stocktoday_inventory import method_map

        inventory_items = method_map()
    else:
        inventory_items = {
            item["name"]: item
            for item in inventory.get("methods", [])
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
    inventory_item = inventory_items.get(method)
    if not isinstance(inventory_item, Mapping):
        return False
    if case.get("category") != inventory_item.get("category"):
        return False

    # Bind the receipt case to the currently packaged probe declaration.  A
    # receipt cannot relabel a one-minute probe as a 5/15/60-minute probe by
    # changing only sanitized parameters while keeping the old manifest hash.
    from . import stocktoday_audit as audit

    try:
        loaded_manifest = probe_manifest if probe_manifest is not None else audit.load_probes()
        probes = loaded_manifest["methods"].get(method, [])
    except Exception:
        return False
    probe_id = case.get("probe_id")
    probe = next(
        (item for item in probes if item.get("probe_id") == probe_id), None
    )
    if not isinstance(probe, Mapping):
        return False
    for case_field, probe_field in (
        ("sanitized_params", "params"),
        ("fixed_date", "fixed_date"),
        ("duplicate_key_fields", "duplicate_key_fields"),
        ("expected_fields", "expected_fields"),
        ("declared_filter_checks", "filter_checks"),
        ("pagination", "pagination"),
        ("primary_declared", "primary_declared"),
    ):
        if case.get(case_field) != probe.get(probe_field):
            return False

    expected_fields = probe.get("expected_fields")
    columns = case.get("columns")
    if (
        not isinstance(expected_fields, list)
        or not isinstance(columns, list)
        or not set(expected_fields).issubset(columns)
    ):
        return False

    sanitized_params = case.get("sanitized_params")
    nested_params = (
        sanitized_params.get("params")
        if isinstance(sanitized_params, Mapping)
        else None
    )
    if not isinstance(nested_params, Mapping):
        return False

    if capability in _MINUTE_CAPABILITY_FREQ:
        return _normalise_frequency(nested_params.get("freq")) == _MINUTE_CAPABILITY_FREQ[capability]
    if capability == "stock_snapshot":
        return "freq" not in nested_params
    if capability in {
        "stock_kline_daily",
        "stock_kline_weekly",
        "stock_kline_monthly",
    }:
        return "freq" not in nested_params
    return True


def _classification_meets_minimum(actual: str, minimum: str) -> bool:
    # A primary promotion is deliberately the only supported positive tier.
    if minimum == PRIMARY_CLASSIFICATION:
        return actual == PRIMARY_CLASSIFICATION
    return actual == minimum


def _validate_active_evidence(
    policy: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
    *,
    now: datetime,
    inventory: Mapping[str, Any] | None = None,
    probe_manifest: Mapping[str, Any] | None = None,
) -> str:
    if receipt is None:
        raise PolicyEvidenceError("active provider policy requires an audit receipt")

    from . import stocktoday_audit as audit

    try:
        audit.validate_receipt(receipt)
    except Exception as exc:
        raise PolicyEvidenceError("audit receipt is invalid") from exc
    if receipt.get("schema_version") != "3.1":
        raise PolicyEvidenceError("audit receipt schema 3.1 is required")

    try:
        current_inventory = inventory if inventory is not None else audit.load_inventory()
        current_probe = (
            probe_manifest if probe_manifest is not None else audit.load_probes()
        )
        current_inventory_sha = current_inventory["payload_sha256"]
        current_probe_sha = current_probe["manifest_sha256"]
    except Exception as exc:
        raise PolicyEvidenceError("current audit inventory or probe is unavailable") from exc
    for field, expected in (
        ("inventory_sha256", current_inventory_sha),
        ("probe_manifest_sha256", current_probe_sha),
    ):
        if receipt.get(field) != expected or policy.get(field) != receipt.get(field):
            raise PolicyEvidenceError(f"{field} does not bind current evidence")
    if policy.get("audit_receipt_sha256") != receipt.get("receipt_sha256"):
        raise PolicyEvidenceError("audit_receipt_sha256 does not bind receipt")

    expiry = _parse_expiry(policy["expires_at"])
    if now.tzinfo is None:
        raise PolicyEvidenceError("policy evaluation time must include a timezone")
    if now >= expiry:
        raise PolicyEvidenceError("provider policy has expired")

    cases = receipt.get("cases", [])
    case_by_identity = {case["case_identity"]: case for case in cases}
    for capability, config in policy["capabilities"].items():
        order = config["provider_order"]
        evidence_items = config["audit_evidence"]
        seen: set[str] = set()
        for evidence in evidence_items:
            identity = evidence["case_identity"]
            if identity in seen:
                raise PolicyEvidenceError(
                    f"{capability} repeats audit evidence {identity}"
                )
            seen.add(identity)
            case = case_by_identity.get(identity)
            if case is None:
                raise PolicyEvidenceError(
                    f"{capability} references an unknown case identity"
                )
            for field in (
                "method",
                "classification",
                "primary_declared",
                "primary_eligible",
            ):
                if evidence[field] != case.get(field):
                    raise PolicyEvidenceError(
                        f"{capability} evidence does not bind case {field}"
                    )
            if not _capability_case_matches(
                capability,
                case,
                inventory=inventory,
                probe_manifest=probe_manifest,
            ):
                raise PolicyEvidenceError(
                    f"{capability} evidence belongs to another capability"
                )

        automatic_stocktoday = (
            capability != "stocktoday_data" and "stocktoday" in order
        )
        if automatic_stocktoday:
            if config["minimum_audit_classification"] != PRIMARY_CLASSIFICATION:
                raise PolicyEvidenceError(f"{capability} StockToday requires pass_nonempty")
            if not evidence_items:
                raise PolicyEvidenceError(f"{capability} StockToday requires audit evidence")
            for evidence in evidence_items:
                if not evidence["primary_declared"] or not evidence["primary_eligible"]:
                    raise PolicyEvidenceError(
                        f"{capability} StockToday evidence is not primary eligible"
                    )
                if not _classification_meets_minimum(
                    evidence["classification"], config["minimum_audit_classification"]
                ):
                    raise PolicyEvidenceError(
                        f"{capability} evidence does not meet minimum classification"
                    )
                case = case_by_identity[evidence["case_identity"]]
                if not _runtime_columns_match(capability, case.get("columns")):
                    raise PolicyEvidenceError(
                        f"{capability} evidence lacks required runtime columns"
                    )

    evidence_payload = {
        "policy_version": policy["policy_version"],
        "pipeline_version": policy["pipeline_version"],
        "route_policy_version": policy["route_policy_version"],
        "inventory_sha256": policy["inventory_sha256"],
        "probe_manifest_sha256": policy["probe_manifest_sha256"],
        "audit_receipt_sha256": policy["audit_receipt_sha256"],
        "capabilities": {
            name: config["audit_evidence"]
            for name, config in sorted(policy["capabilities"].items())
        },
    }
    return _sha256(evidence_payload)


def _reviewed_receipt_target() -> Path:
    # Keep both public names usable for test/runtime inspection while still
    # resolving to one fixed package-relative location in normal operation.
    target_value = (
        REVIEWED_RECEIPT_PATH
        if REVIEWED_RECEIPT_PATH != _DEFAULT_AUDIT_RECEIPT_PATH
        else AUDIT_RECEIPT_PATH
    )
    return Path(target_value)


def _load_reviewed_receipt_from(target: Path) -> dict[str, Any]:
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyEvidenceError(
            f"unable to load canonical reviewed receipt: {target}"
        ) from exc
    return _validate_reviewed_receipt_value(value)


def _validate_reviewed_receipt_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyEvidenceError("canonical reviewed receipt must be an object")
    from . import stocktoday_audit as audit

    try:
        return audit.validate_receipt(value)
    except Exception as exc:
        raise PolicyEvidenceError("canonical reviewed receipt is invalid") from exc


def load_reviewed_receipt() -> dict[str, Any]:
    """Load the one canonical reviewed receipt used by production routing."""

    return _load_reviewed_receipt_from(_reviewed_receipt_target())


@dataclass(frozen=True)
class CompiledPolicy:
    """Validated policy state used by the API router."""

    document: dict[str, Any] | None
    policy_status: str
    policy_evidence_sha256: str | None
    inactive_reason: str | None = None

    @property
    def pipeline_version(self) -> str:
        return PIPELINE_VERSION

    @property
    def route_policy_version(self) -> str:
        return ROUTE_POLICY_VERSION

    @property
    def active(self) -> bool:
        return self.policy_status == "active"

    def route(self, intent: str, params: Mapping[str, Any] | None = None) -> RouteSpec:
        params_dict = dict(params or {})
        if intent == "stocktoday_data":
            return route_for(intent, params_dict)
        # High-frequency dashboard polling is an explicit pipeline-owned
        # profile.  It must keep its PyTDX-first route even when a reviewed
        # capability policy supplies the default Agent/research order.
        if params_dict.get("use_case") == "realtime_poll":
            return route_for(intent, params_dict)
        # An explicit StockToday request remains explicit and keeps the V2
        # source-specific route, regardless of policy activation.
        if (
            intent in {"stock_snapshot", "stock_kline"}
            and params_dict.get("source") == "stocktoday"
        ):
            return route_for(intent, params_dict)
        # A query-bearing review is a distinct natural-language route.  The
        # active policy governs only the default breadth route and must not
        # replace this independently audited V2 route.
        if (
            intent == "review_sentiment"
            and "query" in params_dict
            and params_dict["query"] not in (None, "", [])
        ):
            return route_for(intent, params_dict)
        if not self.active or self.document is None:
            return route_for(intent, params_dict)
        capability = capability_for(intent, params_dict)
        config = self.document["capabilities"].get(capability)
        if config is None:
            return route_for(intent, params_dict)
        base = route_for(intent, params_dict)
        return RouteSpec(
            intent=base.intent,
            providers=tuple(config["provider_order"]),
            data_scope=base.data_scope,
            trade_usage=base.trade_usage,
            max_age_sec=config["max_age_sec"],
            empty_policy=config["empty_policy"],
        )


_POLICY_CACHE_LOCK = threading.RLock()
_POLICY_CACHE: dict[tuple[Any, ...], CompiledPolicy] = {}


def _file_identity(path: Path) -> tuple[int, int, int, int] | None:
    """Return a cheap stable identity for an atomically replaced file."""

    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    data: bytes | None
    identity_before: tuple[int, int, int, int] | None
    identity_after: tuple[int, int, int, int] | None
    raw_sha256: str | None
    logical_sha256: str | None
    stable: bool


def _logical_content_sha256(data: bytes, *, kind: str) -> str | None:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    try:
        if kind == "policy":
            return _policy_logical_sha256(value)
        if kind == "receipt":
            return _sha256(value)
        if kind == "inventory" and isinstance(value, Mapping):
            return _sha256(value.get("methods"))
        if kind == "probe" and isinstance(value, Mapping):
            content = dict(value)
            content.pop("manifest_sha256", None)
            return _sha256(content)
    except Exception:
        return None
    return None


def _read_file_snapshot(path: Path, *, kind: str) -> _FileSnapshot:
    target = Path(path)
    before = _file_identity(target)
    try:
        data = target.read_bytes()
    except OSError:
        data = None
    after = _file_identity(target)
    stable = data is not None and before == after
    raw_sha256 = hashlib.sha256(data).hexdigest() if data is not None else None
    return _FileSnapshot(
        path=target,
        data=data,
        identity_before=before,
        identity_after=after,
        raw_sha256=raw_sha256,
        logical_sha256=(
            _logical_content_sha256(data, kind=kind) if data is not None else None
        ),
        stable=stable,
    )


def _evidence_paths() -> tuple[Path, Path]:
    from .providers.stocktoday_inventory import INVENTORY_PATH
    from .v3 import PROBE_MANIFEST_PATH

    return Path(INVENTORY_PATH), Path(PROBE_MANIFEST_PATH)


def _snapshot_component(snapshot: _FileSnapshot) -> tuple[Any, ...]:
    return (
        snapshot.identity_before,
        snapshot.identity_after,
        snapshot.raw_sha256,
        snapshot.logical_sha256,
        snapshot.stable,
    )


def _snapshot_key(
    policy_path: Path,
    receipt_path: Path,
    *,
    snapshots: Mapping[str, _FileSnapshot] | None = None,
) -> tuple[Any, ...]:
    if snapshots is None:
        inventory_path, probe_path = _evidence_paths()
        snapshots = {
            "policy": _read_file_snapshot(policy_path, kind="policy"),
            "receipt": _read_file_snapshot(receipt_path, kind="receipt"),
            "inventory": _read_file_snapshot(inventory_path, kind="inventory"),
            "probe": _read_file_snapshot(probe_path, kind="probe"),
        }
    return (
        str(policy_path),
        _snapshot_component(snapshots["policy"]),
        str(receipt_path),
        _snapshot_component(snapshots["receipt"]),
        str(snapshots["inventory"].path),
        _snapshot_component(snapshots["inventory"]),
        str(snapshots["probe"].path),
        _snapshot_component(snapshots["probe"]),
        PACKAGED_POLICY_SHA256,
    )


def _clone_compiled_policy(compiled: CompiledPolicy) -> CompiledPolicy:
    """Prevent callers from mutating the cached document snapshot."""

    return CompiledPolicy(
        document=copy.deepcopy(compiled.document),
        policy_status=compiled.policy_status,
        policy_evidence_sha256=compiled.policy_evidence_sha256,
        inactive_reason=compiled.inactive_reason,
    )


def _policy_from_value(value: Any, *, enforce_packaged_anchor: bool) -> dict[str, Any]:
    document = _validate_document(value)
    if enforce_packaged_anchor and _policy_logical_sha256(document) != PACKAGED_POLICY_SHA256:
        raise PolicySchemaError("packaged provider policy anchor mismatch")
    return document


def _json_from_snapshot(snapshot: _FileSnapshot, *, label: str) -> Any:
    if not snapshot.stable or snapshot.data is None:
        raise PolicyError(f"{label} changed or is unavailable during snapshot")
    try:
        return json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"{label} is not valid JSON") from exc


def _snapshot_identities_unchanged(
    snapshots: Mapping[str, _FileSnapshot]
) -> bool:
    return all(
        _file_identity(snapshot.path) == snapshot.identity_after
        for snapshot in snapshots.values()
    )


def _validated_evidence_snapshots(
    inventory_snapshot: _FileSnapshot, probe_snapshot: _FileSnapshot
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .providers.stocktoday_inventory import validate_inventory
    from . import stocktoday_audit as audit

    inventory = validate_inventory(
        _json_from_snapshot(inventory_snapshot, label="StockToday inventory")
    )
    probes = audit.validate_probe_manifest(
        _json_from_snapshot(probe_snapshot, label="StockToday probe manifest"),
        source_path=probe_snapshot.path,
        inventory_payload=inventory,
    )
    return inventory, probes


def _load_default_compiled_policy(*, now: datetime | None = None) -> CompiledPolicy:
    """Load one stable canonical policy/receipt snapshot, then cache it."""

    evaluation_time = now or datetime.now().astimezone()
    policy_path = Path(POLICY_PATH)
    receipt_path = _reviewed_receipt_target()
    inventory_path, probe_path = _evidence_paths()
    with _POLICY_CACHE_LOCK:
        snapshots = {
            "policy": _read_file_snapshot(policy_path, kind="policy"),
            "receipt": _read_file_snapshot(receipt_path, kind="receipt"),
            "inventory": _read_file_snapshot(inventory_path, kind="inventory"),
            "probe": _read_file_snapshot(probe_path, kind="probe"),
        }
        before = _snapshot_key(policy_path, receipt_path, snapshots=snapshots)
        cached = _POLICY_CACHE.get(before)
        if cached is not None:
            if cached.active:
                try:
                    expiry = _parse_expiry(cached.document["expires_at"])
                    if evaluation_time.tzinfo is None or evaluation_time >= expiry:
                        cached = _inactive(
                            cached.document, reason="provider policy has expired"
                        )
                        _POLICY_CACHE[before] = cached
                except Exception:
                    cached = _inactive(
                        cached.document, reason="cached provider policy is invalid"
                    )
                    _POLICY_CACHE[before] = cached
            return _clone_compiled_policy(cached)

        candidate: Mapping[str, Any] | None = None
        try:
            candidate = _policy_from_value(
                _json_from_snapshot(snapshots["policy"], label="provider policy"),
                enforce_packaged_anchor=True,
            )
            receipt: Mapping[str, Any] | None = None
            if candidate.get("active") is True:
                receipt = _validate_reviewed_receipt_value(
                    _json_from_snapshot(snapshots["receipt"], label="audit receipt")
                )
            inventory, probes = _validated_evidence_snapshots(
                snapshots["inventory"], snapshots["probe"]
            )
            if not _snapshot_identities_unchanged(snapshots):
                raise PolicyError("canonical policy/evidence changed during load")
            compiled = compile_policy(
                candidate,
                audit_receipt=receipt,
                now=evaluation_time,
                _inventory=inventory,
                _probe_manifest=probes,
            )
            if not _snapshot_identities_unchanged(snapshots):
                raise PolicyError("canonical policy/evidence changed during compile")
        except PolicyError as exc:
            compiled = _inactive(
                candidate if isinstance(candidate, Mapping) else None,
                reason=str(exc),
            )
        except Exception as exc:
            compiled = _inactive(
                candidate if isinstance(candidate, Mapping) else None,
                reason=str(exc),
            )
        _POLICY_CACHE[before] = _clone_compiled_policy(compiled)
        return _clone_compiled_policy(compiled)


def compile_policy(
    policy: Mapping[str, Any],
    *,
    audit_receipt: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    _inventory: Mapping[str, Any] | None = None,
    _probe_manifest: Mapping[str, Any] | None = None,
) -> CompiledPolicy:
    """Strictly compile a policy; active evidence failures raise."""

    document = _validate_document(policy)
    if not document["active"]:
        return _inactive(document, reason="policy_declared_inactive")
    evaluation_time = now or datetime.now().astimezone()
    evidence_sha = _validate_active_evidence(
        document,
        audit_receipt,
        now=evaluation_time,
        inventory=_inventory,
        probe_manifest=_probe_manifest,
    )
    return CompiledPolicy(
        document=document,
        policy_status="active",
        policy_evidence_sha256=evidence_sha,
    )


def load_policy(path: Path | None = None) -> dict[str, Any]:
    """Load and strictly validate a policy document from the code-side path."""

    target = POLICY_PATH if path is None else Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicySchemaError(f"unable to load provider policy: {target}") from exc
    return _policy_from_value(value, enforce_packaged_anchor=path is None)


_MISSING = object()
_MISSING_RECEIPT = object()


def load_compiled_policy(
    path: Path | None = None,
    *,
    policy: Mapping[str, Any] | object = _MISSING,
    audit_receipt: Mapping[str, Any] | None | object = _MISSING_RECEIPT,
    now: datetime | None = None,
) -> CompiledPolicy:
    """Load a policy fail-closed; malformed or stale evidence is inactive."""

    # Only the production default path is cached.  Explicit policy/receipt
    # injection is test and tooling input and must always compile independently.
    if path is None and policy is _MISSING and audit_receipt is _MISSING_RECEIPT:
        return _load_default_compiled_policy(now=now)

    candidate: Mapping[str, Any] | None = None
    try:
        candidate = load_policy(path) if policy is _MISSING else policy  # type: ignore[assignment]
        receipt = audit_receipt
        if (
            receipt is _MISSING_RECEIPT
            and isinstance(candidate, Mapping)
            and candidate.get("active") is True
        ):
            receipt = load_reviewed_receipt()
        return compile_policy(candidate, audit_receipt=receipt, now=now)  # type: ignore[arg-type]
    except PolicyError as exc:
        return _inactive(candidate if isinstance(candidate, Mapping) else None, reason=str(exc))
    except Exception as exc:
        return _inactive(candidate if isinstance(candidate, Mapping) else None, reason=str(exc))


__all__ = [
    "CAPABILITIES",
    "CompiledPolicy",
    "AUDIT_RECEIPT_PATH",
    "PACKAGED_POLICY_SHA256",
    "PIPELINE_VERSION",
    "POLICY_PATH",
    "POLICY_VERSION",
    "PolicyError",
    "PolicyEvidenceError",
    "PolicySchemaError",
    "ROUTE_POLICY_VERSION",
    "REVIEWED_RECEIPT_PATH",
    "compile_policy",
    "load_compiled_policy",
    "load_policy",
    "load_reviewed_receipt",
]
