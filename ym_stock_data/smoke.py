"""Explicit, bounded live probes that persist sanitized metadata only."""

from __future__ import annotations

import json
import os
import re
import signal
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

from .api import _provider_for, _query_with, query
from .contracts import ATTEMPT_STATUSES, RESULT_STATUSES, TZ_SHANGHAI
from .doctor import collect_diagnostics
from .providers.base import ProviderOutcome
from .smoke_contract import (
    CASE_SPECS,
    CURRENT_SMOKE_BASELINE,
    CURRENT_SMOKE_SCHEMA_VERSION,
)


SMOKE_DIR = Path.home() / ".ym-stock-data" / "smoke"
DEFAULT_CASE_TIMEOUT_SEC = 45.0
DEFAULT_TOTAL_TIMEOUT_SEC = 360.0
_SAFE_ENUM = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_CONTROLLED_FALLBACK_ROUTE = (
    "iwencai_openapi",
    "pywencai",
    "tdx_screener",
    "wind_screener",
)


class _SmokeDeadline(BaseException):
    """Escape provider code without being swallowed by broad Exception handlers."""


def _safe_enum(value: object, default: str | None = None) -> str | None:
    candidate = str(value or "")
    return candidate if _SAFE_ENUM.fullmatch(candidate) else default


@contextmanager
def _deadline(seconds: float):
    if seconds <= 0:
        raise _SmokeDeadline()
    previous_handler = signal.getsignal(signal.SIGALRM)

    def expire(_signum, _frame):
        raise _SmokeDeadline()

    signal.signal(signal.SIGALRM, expire)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _safe_attempts(
    value: object,
    *,
    injected_providers: frozenset[str] = frozenset(),
    include_origin: bool = False,
) -> list[dict]:
    if not isinstance(value, list):
        return []
    attempts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        provider = _safe_enum(item.get("provider"), "unknown")
        status = item.get("status")
        if status not in ATTEMPT_STATUSES:
            status = "provider_error"
        error_code = _safe_enum(item.get("error_code"))
        latency = item.get("latency_ms")
        latency_ms = latency if isinstance(latency, int) and latency >= 0 else 0
        projected = {
                "provider": provider,
                "status": status,
                "error_code": error_code,
                "latency_ms": latency_ms,
        }
        if include_origin:
            projected["origin"] = (
                "injected" if provider in injected_providers else "live"
            )
        attempts.append(projected)
    return attempts


def summarize_query_result(
    result: object,
    *,
    injected_providers: frozenset[str] = frozenset(),
    include_origin: bool = False,
) -> dict:
    """Project a canonical result to non-business smoke metadata."""

    meta = result.get("_meta") if isinstance(result, dict) else None
    if not isinstance(meta, dict):
        return {
            "status": "error",
            "provider_used": None,
            "attempts": [],
            "row_count": 0,
            "error_code": "INVALID_RESULT",
            "protocol_evidence": None,
        }
    status = meta.get("status")
    if status not in RESULT_STATUSES:
        status = "error"
    provider = _safe_enum(meta.get("provider_used"))
    attempts = _safe_attempts(
        meta.get("attempts"),
        injected_providers=injected_providers,
        include_origin=include_origin,
    )
    quality = meta.get("quality")
    count = quality.get("returned_count") if isinstance(quality, dict) else 0
    row_count = count if isinstance(count, int) and count >= 0 else 0
    error_code = next(
        (
            attempt["error_code"]
            for attempt in reversed(attempts)
            if attempt["error_code"]
        ),
        None,
    )
    return {
        "status": status,
        "provider_used": provider,
        "attempts": attempts,
        "row_count": row_count,
        "error_code": error_code,
        "protocol_evidence": None,
    }


_PROTOCOL_KEYS = frozenset(
    {
        "initialize",
        "tools_list",
        "schema",
        "read_only",
        "tool_call",
        "page_count",
        "session_count",
        "refresh_count",
        "call_count",
    }
)


def _safe_protocol(value: object) -> dict | None:
    if not isinstance(value, dict) or set(value) != _PROTOCOL_KEYS:
        return None
    projected = {}
    for key in ("initialize", "tools_list", "schema", "read_only", "tool_call"):
        if value[key] not in {"pass", "fail"}:
            return None
        projected[key] = value[key]
    for key in ("page_count", "session_count", "refresh_count", "call_count"):
        item = value[key]
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            return None
        projected[key] = item
    return projected


def _summarize_outcome(outcome: ProviderOutcome) -> dict:
    status = outcome.status if outcome.status in ATTEMPT_STATUSES else "provider_error"
    provider = _safe_enum(outcome.provider, "tdx_mcp")
    error_code = _safe_enum(outcome.error_code)
    count = 0
    if isinstance(outcome.quality, dict):
        value = outcome.quality.get("returned_count")
        count = value if isinstance(value, int) and value >= 0 else 0
    return {
        "status": status,
        "provider_used": provider if status in {"success", "empty"} else None,
        "attempts": [
            {
                "provider": provider,
                "status": status,
                "error_code": error_code,
                "latency_ms": max(0, int(outcome.latency_ms or 0)),
                "origin": "live",
            }
        ],
        "row_count": count,
        "error_code": error_code,
        "protocol_evidence": _safe_protocol(
            (outcome.provenance or {}).get("smoke_protocol")
        ),
    }


def _provider_state_payload(name: str, diagnostics_fn: Callable[[], dict]) -> dict:
    try:
        diagnostics = diagnostics_fn()
    except Exception:
        state = "unavailable"
    else:
        providers = diagnostics.get("providers") if isinstance(diagnostics, dict) else None
        item = providers.get(name) if isinstance(providers, dict) else None
        state = item.get("status") if isinstance(item, dict) else "unavailable"
    state = _safe_enum(state, "unavailable")
    return {
        "status": state,
        "provider_used": None,
        "attempts": [],
        "row_count": 0,
        "error_code": None,
        "protocol_evidence": None,
    }


class _NoBreakers:
    def active_breaker(self, _provider_name: str):
        return None


class _InjectedProvider:
    def __init__(self, outcome: ProviderOutcome):
        self.outcome = outcome

    def call(self, _intent: str, _params: dict) -> ProviderOutcome:
        return self.outcome


def _compute_smoke_gate(cases: list[dict]) -> tuple[dict, str, str]:
    """Recompute the four managed sources and TDX fallback gate."""

    by_id = {case["case_id"]: case for case in cases}

    def passed(case_id: str, provider: str, *, protocol: bool = False) -> bool:
        case = by_id[case_id]
        valid = (
            case["status"] == "success"
            and case["provider_used"] == provider
            and case["row_count"] > 0
            and any(
                attempt["provider"] == provider
                and attempt["status"] == "success"
                and attempt["origin"] == "live"
                for attempt in case["attempts"]
            )
        )
        if not protocol:
            return valid
        evidence = case["protocol_evidence"]
        return valid and isinstance(evidence, dict) and all(
            evidence[key] == "pass"
            for key in ("initialize", "tools_list", "schema", "read_only", "tool_call")
        ) and all(
            evidence[key] >= 1
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
    }
    fallback = by_id["canonical_tdx_fallback"]
    attempts = fallback["attempts"]
    chain_status = "pass" if (
        fallback["status"] == "degraded"
        and fallback["provider_used"] == "tdx_screener"
        and fallback["row_count"] > 0
        and [item["provider"] for item in attempts]
        == ["iwencai_openapi", "pywencai", "tdx_screener"]
        and [item["status"] for item in attempts]
        == ["auth_error", "provider_error", "success"]
        and [item["origin"] for item in attempts]
        == ["injected", "injected", "live"]
    ) else "fail"
    gate_status = "pass" if (
        chain_status == "pass"
        and all(status == "pass" for status in source_status.values())
    ) else "fail"
    return source_status, chain_status, gate_status


def _atomic_write(report: dict, output_dir: Path, now: datetime) -> Path:
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    filename = now.strftime("%Y-%m-%dT%H%M%S%z.json")
    destination = output_dir / filename
    temporary = output_dir / f".{filename}.{os.getpid()}.tmp"
    payload = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def run_live_smoke(
    *,
    output_dir: Path = SMOKE_DIR,
    query_fn: Callable = query,
    diagnostics_fn: Callable[[], dict] = collect_diagnostics,
    provider_loader: Callable[[str], object] = _provider_for,
    now_fn: Callable[[], datetime] = lambda: datetime.now(TZ_SHANGHAI),
    case_timeout_sec: float = DEFAULT_CASE_TIMEOUT_SEC,
    total_timeout_sec: float = DEFAULT_TOTAL_TIMEOUT_SEC,
) -> dict:
    """Run the explicit read-only matrix; never persist business rows."""

    if case_timeout_sec <= 0 or total_timeout_sec <= 0:
        raise ValueError("smoke timeouts must be positive")
    started_at = now_fn()
    started = time.monotonic()
    diagnostics_budget = min(case_timeout_sec, total_timeout_sec)
    try:
        with _deadline(diagnostics_budget):
            diagnostics = diagnostics_fn()
    except _SmokeDeadline:
        diagnostics = {"providers": {}}
    except Exception:
        diagnostics = {"providers": {}}

    def canonical(intent: str, **params):
        return lambda: summarize_query_result(
            query_fn(intent, **params), include_origin=True
        )

    def direct_probe(
        provider_name: str,
        intent: str,
        params: dict,
        *,
        diagnostic_name: str | None = None,
    ):
        state = _provider_state_payload(
            diagnostic_name or provider_name, lambda: diagnostics
        )
        if state["status"] not in {"configured_unverified", "ready"}:
            return state
        outcome = provider_loader(provider_name).call(intent, dict(params))
        return _summarize_outcome(outcome)

    def controlled_fallback():
        from . import api as api_module

        tdx_state = _provider_state_payload("tdx_mcp", lambda: diagnostics)
        if tdx_state["status"] not in {"configured_unverified", "ready"}:
            return tdx_state
        params = {"query": "沪深A股 非ST 非停牌 最新价>=1", "limit": 3}
        spec = api_module.route_for("review_sentiment", dict(params))
        if tuple(spec.providers) != _CONTROLLED_FALLBACK_ROUTE:
            return {
                "status": "error",
                "provider_used": None,
                "attempts": [],
                "row_count": 0,
                "error_code": "CONTROLLED_ROUTE_DRIFT",
                "protocol_evidence": None,
            }
        injected = {
            "iwencai_openapi": ProviderOutcome(
                "iwencai_openapi", "auth_error", error_code="HTTP_401",
                auth={"required": True, "status": "expired"},
            ),
            "pywencai": ProviderOutcome(
                "pywencai", "provider_error", error_code="PYWENCAI_PROVIDER_ERROR"
            ),
        }
        controlled_origins = set(injected)
        next_provider = 0
        route_drifted = False

        def controlled_loader(name: str):
            nonlocal next_provider, route_drifted
            expected = (
                _CONTROLLED_FALLBACK_ROUTE[next_provider]
                if next_provider < len(_CONTROLLED_FALLBACK_ROUTE)
                else None
            )
            if route_drifted or name != expected:
                route_drifted = True
                controlled_origins.add(name)
                return _InjectedProvider(
                    ProviderOutcome(
                        name,
                        "provider_error",
                        error_code="CONTROLLED_ROUTE_DRIFT",
                    )
                )
            next_provider += 1
            if name in injected:
                return _InjectedProvider(injected[name])
            if name == "tdx_screener":
                return provider_loader(name)
            route_drifted = True
            controlled_origins.add(name)
            return _InjectedProvider(
                ProviderOutcome(
                    name,
                    "provider_error",
                    error_code="CONTROLLED_ROUTE_DRIFT",
                )
            )

        result = _query_with(
            "review_sentiment",
            params,
            provider_loader=controlled_loader,
            state_loader=_NoBreakers,
        )
        return summarize_query_result(
            result,
            injected_providers=frozenset(controlled_origins),
            include_origin=True,
        )

    callbacks = {
        "zero_realtime_market": canonical("realtime_market"),
        "zero_sector_index": canonical("sector_index", names=["半导体"]),
        "zero_stock_snapshot": canonical("stock_snapshot", codes=["600519"]),
        "zero_stock_kline": canonical(
            "stock_kline", code="600519", period="daily", count=3
        ),
        "zero_review_sentiment": canonical("review_sentiment"),
        "zero_market_limit_state": canonical("market_limit_state"),
        "zero_stock_event": canonical(
            "stock_event", code="600519", event="lockup", page_size=3
        ),
        "explicit_wencai": canonical(
            "review_sentiment", query="A股 非ST 涨停", limit=3
        ),
        "optional_pytdx_screener_state": lambda: _provider_state_payload(
            "pytdx_screener", lambda: diagnostics
        ),
        "tdx_probe": lambda: direct_probe(
            "tdx_quotes", "stock_snapshot", {"codes": ["600519"]},
            diagnostic_name="tdx_mcp",
        ),
        "wind_probe": lambda: direct_probe(
            "wind_mcp", "wind_enrichment",
            {"capability": "company_profile", "code": "600519"},
            diagnostic_name="wind_mcp",
        ),
        "direct_openapi_screener": lambda: direct_probe(
            "iwencai_openapi", "review_sentiment",
            {"query": "沪深A股 非ST 非停牌 最新价>=1", "limit": 3},
        ),
        "direct_pywencai_screener": lambda: direct_probe(
            "pywencai", "review_sentiment",
            {"query": "沪深A股 非ST 非停牌 最新价>=1", "limit": 3},
        ),
        "tdx_screener_probe": lambda: direct_probe(
            "tdx_screener", "review_sentiment",
            {"query": "沪深A股 非ST 非停牌 最新价>=1", "limit": 3},
            diagnostic_name="tdx_mcp",
        ),
        "tdx_kline_probe": lambda: direct_probe(
            "tdx_kline", "stock_kline",
            {"code": "600519", "period": "daily", "count": 3},
            diagnostic_name="tdx_mcp",
        ),
        "tdx_report_probe": lambda: direct_probe(
            "tdx_report", "research", {"code": "600519", "days": 365},
            diagnostic_name="tdx_mcp",
        ),
        "tdx_notice_probe": lambda: direct_probe(
            "tdx_notice", "filings", {"code": "600519", "days": 365},
            diagnostic_name="tdx_mcp",
        ),
        "tdx_news_probe": lambda: direct_probe(
            "tdx_news", "news", {"limit": 3}, diagnostic_name="tdx_mcp",
        ),
        "wind_screener_probe": lambda: direct_probe(
            "wind_screener", "review_sentiment",
            {"query": "沪深A股 非ST 非停牌 最新价>=1", "limit": 3},
            diagnostic_name="wind_mcp",
        ),
        "wind_filings_probe": lambda: direct_probe(
            "wind_documents", "filings",
            {"code": "600519", "days": 365, "max_pages": 1},
            diagnostic_name="wind_mcp",
        ),
        "canonical_tdx_fallback": controlled_fallback,
    }
    if set(callbacks) != {spec.case_id for spec in CASE_SPECS}:
        raise RuntimeError("smoke case contract drift")
    cases = []
    for spec in CASE_SPECS:
        callback = callbacks[spec.case_id]
        elapsed = time.monotonic() - started
        remaining = total_timeout_sec - elapsed
        case_started = time.monotonic()
        if remaining <= 0:
            payload = {
                "status": "timeout",
                "provider_used": None,
                "attempts": [],
                "row_count": 0,
                    "error_code": "TOTAL_TIMEOUT",
                    "protocol_evidence": None,
            }
        else:
            try:
                with _deadline(min(case_timeout_sec, remaining)):
                    payload = callback()
            except _SmokeDeadline:
                payload = {
                    "status": "timeout",
                    "provider_used": None,
                    "attempts": [
                        {
                            "provider": spec.direct_provider,
                            "status": "timeout",
                            "error_code": "SMOKE_TIMEOUT",
                            "latency_ms": 0,
                            "origin": "live",
                        }
                    ]
                    if spec.direct_provider
                    else [],
                    "row_count": 0,
                    "error_code": "SMOKE_TIMEOUT",
                    "protocol_evidence": None,
                }
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                payload = {
                    "status": "error",
                    "provider_used": None,
                    "attempts": [
                        {
                            "provider": spec.direct_provider,
                            "status": "provider_error",
                            "error_code": "UNHANDLED_EXCEPTION",
                            "latency_ms": 0,
                            "origin": "live",
                        }
                    ]
                    if spec.direct_provider
                    else [],
                    "row_count": 0,
                    "error_code": "UNHANDLED_EXCEPTION",
                    "protocol_evidence": None,
                }
        cases.append(
            {
                "case_id": spec.case_id,
                "category": spec.category,
                "intent": spec.intent,
                "params": spec.safe_params(),
                "direct_provider": spec.direct_provider,
                "evidence_kind": spec.evidence_kind,
                "capability": spec.capability,
                **payload,
                "latency_ms": max(0, int((time.monotonic() - case_started) * 1000)),
            }
        )
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["status"]] = counts.get(case["status"], 0) + 1
    completed_at = now_fn()
    source_status, chain_status, gate_status = _compute_smoke_gate(cases)
    report = {
        "schema_version": CURRENT_SMOKE_SCHEMA_VERSION,
        "baseline": CURRENT_SMOKE_BASELINE,
        "live": True,
        "started_at": started_at.isoformat(timespec="seconds"),
        "completed_at": completed_at.isoformat(timespec="seconds"),
        "summary": {"total": len(cases), "status_counts": counts},
        "source_status": source_status,
        "chain_status": chain_status,
        "gate_status": gate_status,
        "cases": cases,
    }
    receipt = _atomic_write(report, Path(output_dir), completed_at)
    return {
        "receipt": str(receipt),
        "summary": report["summary"],
        "source_status": source_status,
        "chain_status": chain_status,
        "gate_status": gate_status,
        "cases": cases,
    }
