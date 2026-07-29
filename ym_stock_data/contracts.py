"""Canonical result contract for the unified data channel."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


CONTRACT_VERSION = "1.0"
RESULT_STATUSES = frozenset({"success", "degraded", "empty", "error"})
ATTEMPT_STATUSES = frozenset(
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
FRESHNESS_STATUSES = frozenset({"fresh", "stale"})
TZ_SHANGHAI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ProviderAttempt:
    provider: str
    status: str
    error_code: str | None
    latency_ms: int


def _now_iso() -> str:
    return datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds")


def _freshness(fetched_at: str, max_age_sec: int) -> dict[str, Any]:
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("fetched_at must be an ISO-8601 timestamp") from exc
    if fetched.tzinfo is None:
        raise ValueError("fetched_at must include a timezone")

    age_sec = max(0, int((datetime.now(TZ_SHANGHAI) - fetched).total_seconds()))
    return {
        "status": "fresh" if age_sec <= max_age_sec else "stale",
        "age_sec": age_sec,
        "max_age_sec": max_age_sec,
    }


def build_result(
    *,
    intent: str,
    data: object,
    status: str,
    provider_used: str | None,
    attempts: list[ProviderAttempt],
    data_scope: str,
    trade_usage: str,
    quality: dict,
    max_age_sec: int,
    fetched_at: str | None = None,
    auth: dict | None = None,
) -> dict:
    """Build contract 1.0 without leaking provider secrets."""

    timestamp = fetched_at or _now_iso()
    result = {
        "data": data,
        "_meta": {
            "contract_version": CONTRACT_VERSION,
            "intent": intent,
            "status": status,
            "provider_used": provider_used,
            "source": provider_used,
            "source_chain": [attempt.provider for attempt in attempts],
            "attempts": [asdict(attempt) for attempt in attempts],
            "fetched_at": timestamp,
            "data_scope": data_scope,
            "quality": dict(quality),
            "freshness": _freshness(timestamp, max_age_sec),
            "auth": dict(
                auth
                if auth is not None
                else {"required": False, "status": "not_required"}
            ),
            "trade_usage": trade_usage,
        },
    }
    validate_result(result)
    return result


def validate_result(result: dict) -> None:
    """Raise ValueError when required keys, enums, or provenance invariants fail."""

    if not isinstance(result, dict) or "data" not in result:
        raise ValueError("result must contain data")
    meta = result.get("_meta")
    if not isinstance(meta, dict):
        raise ValueError("result must contain _meta")

    required_meta = {
        "contract_version",
        "intent",
        "status",
        "provider_used",
        "source",
        "source_chain",
        "attempts",
        "fetched_at",
        "data_scope",
        "quality",
        "freshness",
        "auth",
        "trade_usage",
    }
    missing = sorted(required_meta - meta.keys())
    if missing:
        raise ValueError(f"missing metadata keys: {', '.join(missing)}")
    if meta["contract_version"] != CONTRACT_VERSION:
        raise ValueError("unsupported contract_version")
    if not isinstance(meta["intent"], str) or not meta["intent"]:
        raise ValueError("intent must be a non-empty string")
    if meta["status"] not in RESULT_STATUSES:
        raise ValueError("invalid result status")

    attempts = meta["attempts"]
    if not isinstance(attempts, list):
        raise ValueError("attempts must be a list")
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError("each attempt must be a mapping")
        if set(attempt) != {"provider", "status", "error_code", "latency_ms"}:
            raise ValueError("attempt has invalid fields")
        if not isinstance(attempt["provider"], str) or not attempt["provider"]:
            raise ValueError("attempt provider must be a non-empty string")
        if attempt["status"] not in ATTEMPT_STATUSES:
            raise ValueError("invalid attempt status")
        if attempt["error_code"] is not None and not isinstance(
            attempt["error_code"], str
        ):
            raise ValueError("attempt error_code must be a string or null")
        if (
            not isinstance(attempt["latency_ms"], int)
            or isinstance(attempt["latency_ms"], bool)
            or attempt["latency_ms"] < 0
        ):
            raise ValueError("attempt latency_ms must be a non-negative integer")

    source_chain = [attempt["provider"] for attempt in attempts]
    if meta["source_chain"] != source_chain:
        raise ValueError("source_chain must preserve attempt order")
    if meta["source"] != meta["provider_used"]:
        raise ValueError("source must alias provider_used")

    provider_used = meta["provider_used"]
    if meta["status"] == "error" and provider_used is not None:
        raise ValueError("error results cannot name provider_used")
    if meta["status"] in {"success", "degraded"} and provider_used is None:
        raise ValueError("successful results must name provider_used")
    if provider_used is not None and not any(
        attempt["provider"] == provider_used
        and attempt["status"] in {"success", "empty"}
        for attempt in attempts
    ):
        raise ValueError("provider_used must identify an actual successful attempt")

    quality = meta["quality"]
    if not isinstance(quality, dict):
        raise ValueError("quality must be a mapping")
    quality_required = {"status", "returned_count", "reason_codes"}
    if not quality_required.issubset(quality):
        raise ValueError("quality is missing required fields")
    if (
        not isinstance(quality["returned_count"], int)
        or isinstance(quality["returned_count"], bool)
        or quality["returned_count"] < 0
    ):
        raise ValueError("quality.returned_count must be a non-negative integer")
    if not isinstance(quality["reason_codes"], list):
        raise ValueError("quality.reason_codes must be a list")

    freshness = meta["freshness"]
    if not isinstance(freshness, dict):
        raise ValueError("freshness must be a mapping")
    if freshness.get("status") not in FRESHNESS_STATUSES:
        raise ValueError("invalid freshness status")
    for key in ("age_sec", "max_age_sec"):
        if (
            not isinstance(freshness.get(key), int)
            or isinstance(freshness.get(key), bool)
            or freshness[key] < 0
        ):
            raise ValueError(f"freshness.{key} must be a non-negative integer")
    _freshness(meta["fetched_at"], freshness["max_age_sec"])

    if not isinstance(meta["auth"], dict):
        raise ValueError("auth must be a mapping")
    if not isinstance(meta["data_scope"], str) or not meta["data_scope"]:
        raise ValueError("data_scope must be a non-empty string")
    if not isinstance(meta["trade_usage"], str) or not meta["trade_usage"]:
        raise ValueError("trade_usage must be a non-empty string")
