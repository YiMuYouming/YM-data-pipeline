"""One canonical public query router."""

from __future__ import annotations

import re
import socket
from typing import Callable

from .contracts import ProviderAttempt, build_result
from .provider_state import ProviderState
from .providers.base import ProviderOutcome
from .providers.iwencai import IWenCaiOpenAPIProvider, PyWenCaiProvider
from .providers.local import LOCAL_PROVIDER_NAMES, LocalProvider
from .routing import RouteSpec, route_for
from .sources.stock_events import EVENTS as STOCK_EVENTS


_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_CONTINUE_STATUSES = {
    "auth_error",
    "dependency_missing",
    "timeout",
    "network_error",
    "provider_error",
    "breaker_open",
    "incompatible",
}


class UnavailableProvider:
    """Auditable placeholder for a provider implemented in a later task."""

    def __init__(self, name: str):
        self.name = name

    def probe(self) -> dict:
        return {"provider": self.name, "status": "unavailable"}

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        return ProviderOutcome(
            provider=self.name,
            status="dependency_missing",
            error_code="PROVIDER_NOT_IMPLEMENTED",
        )


def _local_factory(name: str) -> Callable[[], LocalProvider]:
    return lambda: LocalProvider(name)


PROVIDER_REGISTRY: dict[str, object] = {
    **{name: _local_factory(name) for name in LOCAL_PROVIDER_NAMES},
    "iwencai_openapi": IWenCaiOpenAPIProvider,
    "pywencai": PyWenCaiProvider,
}
_STATE: ProviderState | None = None


def _provider_state() -> ProviderState:
    global _STATE
    if _STATE is None:
        _STATE = ProviderState()
    return _STATE


def _provider_for(name: str):
    registered = PROVIDER_REGISTRY.get(name)
    if registered is None:
        return UnavailableProvider(name)
    if callable(registered) and not hasattr(registered, "call"):
        return registered()
    return registered


def _safe_error_code(value: object, default: str) -> str:
    candidate = str(value or "")
    return candidate if _SAFE_CODE.fullmatch(candidate) else default


def _validate_params(intent: str, params: dict) -> None:
    if intent == "stock_snapshot":
        codes = params.get("codes")
        if isinstance(codes, str):
            params["codes"] = [codes]
        elif not isinstance(codes, (list, tuple)) or not codes:
            raise ValueError("stock_snapshot requires non-empty codes")
        if not all(str(code).strip() for code in params["codes"]):
            raise ValueError("stock_snapshot codes cannot contain empty values")
        params["codes"] = [str(code) for code in params["codes"]]
    elif intent == "sector_index":
        for key in ("codes", "names"):
            value = params.get(key)
            if isinstance(value, str):
                params[key] = [value]
            elif value is not None and not isinstance(value, (list, tuple)):
                raise ValueError(f"sector_index {key} must be a list")
        if not params.get("codes") and not params.get("names"):
            raise ValueError("sector_index requires codes or names")
        if any(not str(code).startswith("881") for code in params.get("codes") or []):
            raise ValueError("sector_index codes must use the THS 881 prefix")
    elif intent == "stock_kline":
        if not str(params.get("code") or "").strip():
            raise ValueError("stock_kline requires code")
        period = str(params.get("period", "daily"))
        if period not in {"daily", "weekly", "monthly", "60m", "15m", "5m"}:
            raise ValueError("unsupported stock_kline period")
        params["code"] = str(params["code"])
        params["period"] = period
        if params.get("count") is not None:
            count = int(params["count"])
            if count <= 0:
                raise ValueError("stock_kline count must be positive")
            params["count"] = count
    elif intent == "review_sentiment":
        query_value = params.get("query")
        if query_value is not None and (
            not isinstance(query_value, str) or not query_value.strip()
        ):
            raise ValueError("review_sentiment query must be a non-empty string")
        limit = int(params.get("limit", 50))
        if limit <= 0:
            raise ValueError("review_sentiment limit must be positive")
        params["limit"] = limit
    elif intent == "stock_event":
        if params.get("event") not in STOCK_EVENTS:
            raise ValueError("stock_event requires a supported event")
        if not str(params.get("code") or "").strip():
            raise ValueError("stock_event requires code")
        page_size = int(params.get("page_size", 30))
        if page_size <= 0:
            raise ValueError("stock_event page_size must be positive")
        params["code"] = str(params["code"])
        params["page_size"] = page_size
    elif intent in {"research", "filings"}:
        if not str(params.get("code") or "").strip():
            raise ValueError(f"{intent} requires code")
        params["code"] = str(params["code"])
        for key in ("days", "max_pages"):
            if key in params and int(params[key]) <= 0:
                raise ValueError(f"{intent} {key} must be positive")
            if key in params:
                params[key] = int(params[key])
    elif intent == "news":
        limit = int(params.get("limit", 20))
        if limit <= 0:
            raise ValueError("news limit must be positive")
        params["limit"] = limit


def _analyze_data(intent: str, params: dict, data: object) -> tuple[bool, bool, int]:
    if not isinstance(data, dict) or data.get("error"):
        return False, False, 0
    if intent == "review_sentiment":
        if params.get("query") is not None:
            rows = data.get("datas")
            return (isinstance(rows, list), isinstance(rows, list) and not rows, len(rows) if isinstance(rows, list) else 0)
        if "_total" in data:
            try:
                count = max(0, int(data.get("_total") or 0))
            except (TypeError, ValueError):
                return False, False, 0
            return count > 0, False, count
        required = {"zt_count", "zb_count", "dt_count", "pools"}
        if required.issubset(data):
            count = sum(int(data.get(key, 0) or 0) for key in ("zt_count", "zb_count", "dt_count"))
            return True, count == 0, count
        return False, False, 0
    if intent == "realtime_market":
        count = int(any(key not in {"_meta", "_source"} for key in data))
        return bool(count), False, count
    if intent == "sector_index":
        rows = data.get("items")
        count = len(rows) if isinstance(rows, list) else 0
        return count > 0, False, count
    if intent == "stock_snapshot":
        codes = params["codes"]
        count = sum(
            1
            for code in codes
            if isinstance(data.get(code), dict) and not data[code].get("error")
        )
        return count > 0, False, count
    if intent == "stock_kline":
        rows = data.get("bars")
        count = len(rows) if isinstance(rows, list) else 0
        return count > 0, False, count
    if intent == "market_limit_state":
        required = {"zt_count", "zb_count", "dt_count", "break_rate", "max_board", "pools"}
        if not required.issubset(data):
            return False, False, 0
        count = sum(int(data.get(key, 0) or 0) for key in ("zt_count", "zb_count", "dt_count"))
        return True, count == 0, count
    container = {
        "stock_event": "items",
        "research": "reports",
        "filings": "filings",
        "news": "items",
    }.get(intent)
    if container:
        rows = data.get(container)
        count = len(rows) if isinstance(rows, list) else 0
        return isinstance(rows, list), isinstance(rows, list) and not rows, count
    count = int(bool(data))
    return bool(count), False, count


def _quality(outcome: ProviderOutcome | None, *, status: str, count: int) -> dict:
    quality = dict(outcome.quality or {}) if outcome is not None else {}
    quality.setdefault("status", "normal" if status in {"success", "degraded"} else status)
    quality["returned_count"] = count
    quality.setdefault("reason_codes", [])
    return quality


def query(intent: str, **params) -> dict:
    """Resolve one canonical intent through semantically compatible providers."""

    call_params = dict(params)
    spec: RouteSpec = route_for(intent, call_params)
    _validate_params(intent, call_params)

    attempts: list[ProviderAttempt] = []
    provider_used: str | None = None
    data: object = None
    final_status = "error"
    final_count = 0
    final_outcome: ProviderOutcome | None = None
    fetched_at: str | None = None
    auth: dict | None = None

    for provider_name in spec.providers:
        breaker = _provider_state().active_breaker(provider_name)
        if breaker:
            attempts.append(
                ProviderAttempt(
                    provider_name,
                    "breaker_open",
                    breaker["error_code"],
                    0,
                )
            )
            continue
        try:
            provider = _provider_for(provider_name)
            outcome = provider.call(intent, dict(call_params))
        except (TimeoutError, socket.timeout):
            outcome = ProviderOutcome(provider_name, "timeout", error_code="TIMEOUT")
        except ImportError:
            outcome = ProviderOutcome(
                provider_name,
                "dependency_missing",
                error_code="DEPENDENCY_MISSING",
            )
        except Exception as error:
            outcome = ProviderOutcome(
                provider_name,
                "provider_error",
                error_code=_safe_error_code(type(error).__name__, "PROVIDER_ERROR"),
            )

        outcome_status = outcome.status if outcome.status in _CONTINUE_STATUSES | {"success", "empty"} else "provider_error"
        error_code = outcome.error_code
        actual_provider = outcome.provider or provider_name
        if actual_provider != provider_name and outcome_status in {"success", "empty"}:
            attempts.append(
                ProviderAttempt(provider_name, "provider_error", "INTERNAL_FALLBACK", 0)
            )

        if outcome_status in {"success", "empty"}:
            valid, is_empty, count = _analyze_data(intent, call_params, outcome.data)
            if not valid:
                attempts.append(
                    ProviderAttempt(
                        actual_provider,
                        "provider_error",
                        "INVALID_EMPTY" if outcome_status == "empty" else "INVALID_RESPONSE",
                        max(0, int(outcome.latency_ms)),
                    )
                )
                continue
            if outcome_status == "empty" and not is_empty:
                attempts.append(
                    ProviderAttempt(
                        actual_provider,
                        "provider_error",
                        "STATUS_DATA_MISMATCH",
                        max(0, int(outcome.latency_ms)),
                    )
                )
                continue
            terminal_status = "empty" if is_empty else "success"
            attempts.append(
                ProviderAttempt(
                    actual_provider,
                    terminal_status,
                    None,
                    max(0, int(outcome.latency_ms)),
                )
            )
            provider_used = actual_provider
            data = outcome.data
            final_count = count
            final_outcome = outcome
            fetched_at = outcome.fetched_at
            auth = outcome.auth
            final_status = (
                "degraded"
                if terminal_status == "success" and any(
                    attempt.status != "success" for attempt in attempts[:-1]
                )
                else terminal_status
            )
            break

        attempts.append(
            ProviderAttempt(
                provider_name,
                outcome_status,
                _safe_error_code(error_code, "PROVIDER_ERROR"),
                max(0, int(outcome.latency_ms)),
            )
        )
        final_outcome = outcome
        auth = outcome.auth or auth

    quality = _quality(final_outcome, status=final_status, count=final_count)
    return build_result(
        intent=intent,
        data=data,
        status=final_status,
        provider_used=provider_used,
        attempts=attempts,
        data_scope=spec.data_scope,
        trade_usage=spec.trade_usage,
        quality=quality,
        max_age_sec=spec.max_age_sec,
        fetched_at=fetched_at,
        auth=auth,
    )
