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

from .api import _provider_for, query
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


def _safe_attempts(value: object) -> list[dict]:
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
        attempts.append(
            {
                "provider": provider,
                "status": status,
                "error_code": error_code,
                "latency_ms": latency_ms,
            }
        )
    return attempts


def summarize_query_result(result: object) -> dict:
    """Project a canonical result to non-business smoke metadata."""

    meta = result.get("_meta") if isinstance(result, dict) else None
    if not isinstance(meta, dict):
        return {
            "status": "error",
            "provider_used": None,
            "attempts": [],
            "row_count": 0,
            "error_code": "INVALID_RESULT",
        }
    status = meta.get("status")
    if status not in RESULT_STATUSES:
        status = "error"
    provider = _safe_enum(meta.get("provider_used"))
    attempts = _safe_attempts(meta.get("attempts"))
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
    }


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
            }
        ],
        "row_count": count,
        "error_code": error_code,
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
    }


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

    def canonical(intent: str, **params):
        return lambda: summarize_query_result(query_fn(intent, **params))

    def tdx_probe():
        state = _provider_state_payload("tdx_mcp", diagnostics_fn)
        if state["status"] not in {"configured_unverified", "ready"}:
            return state
        outcome = provider_loader("tdx_quotes").call(
            "stock_snapshot", {"codes": ["600519"]}
        )
        return _summarize_outcome(outcome)

    def pytdx_screener_probe():
        outcome = provider_loader("pytdx_screener").call(
            "review_sentiment",
            {"query": "沪深A股 非ST 非停牌 最新价>=1", "limit": 3},
        )
        return _summarize_outcome(outcome)

    def wind_probe():
        state = _provider_state_payload("wind_mcp", diagnostics_fn)
        if state["status"] not in {"configured_unverified", "ready"}:
            return state
        return summarize_query_result(
            query_fn(
                "wind_enrichment",
                capability="company_profile",
                code="600519",
            )
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
        "explicit_structured_screener": pytdx_screener_probe,
        "tdx_probe": tdx_probe,
        "wind_probe": wind_probe,
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
                        }
                    ]
                    if spec.direct_provider
                    else [],
                    "row_count": 0,
                    "error_code": "SMOKE_TIMEOUT",
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
                        }
                    ]
                    if spec.direct_provider
                    else [],
                    "row_count": 0,
                    "error_code": "UNHANDLED_EXCEPTION",
                }
        cases.append(
            {
                "case_id": spec.case_id,
                "category": spec.category,
                "intent": spec.intent,
                "params": spec.safe_params(),
                **payload,
                "latency_ms": max(0, int((time.monotonic() - case_started) * 1000)),
            }
        )
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["status"]] = counts.get(case["status"], 0) + 1
    completed_at = now_fn()
    report = {
        "schema_version": CURRENT_SMOKE_SCHEMA_VERSION,
        "baseline": CURRENT_SMOKE_BASELINE,
        "live": True,
        "started_at": started_at.isoformat(timespec="seconds"),
        "completed_at": completed_at.isoformat(timespec="seconds"),
        "summary": {"total": len(cases), "status_counts": counts},
        "cases": cases,
    }
    receipt = _atomic_write(report, Path(output_dir), completed_at)
    return {"receipt": str(receipt), "summary": report["summary"], "cases": cases}
