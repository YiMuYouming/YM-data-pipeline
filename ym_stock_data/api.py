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
_ALLOWED_PARAMS = {
    "realtime_market": frozenset(),
    "sector_index": frozenset({"codes", "names"}),
    "stock_snapshot": frozenset({"codes"}),
    "stock_kline": frozenset({"code", "period", "count"}),
    "review_sentiment": frozenset(
        {"query", "limit", "date", "expected_row_shape", "expected_count"}
    ),
    "market_limit_state": frozenset({"date"}),
    "stock_event": frozenset({"event", "code", "page_size"}),
    "research": frozenset({"code", "days", "max_pages"}),
    "filings": frozenset({"code", "days", "max_pages"}),
    "news": frozenset({"limit"}),
    "wind_enrichment": frozenset({"capability", "code", "codes", "fields", "params"}),
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
    unknown = set(params) - _ALLOWED_PARAMS[intent]
    if unknown:
        raise ValueError(f"unsupported {intent} params: {', '.join(sorted(unknown))}")
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
        expected_row_shape = params.get("expected_row_shape")
        if expected_row_shape not in {None, "stock_rows", "sector_rows"}:
            raise ValueError("unsupported review_sentiment expected_row_shape")
        if params.get("expected_count") is not None:
            expected_count = int(params["expected_count"])
            if expected_count <= 0:
                raise ValueError("review_sentiment expected_count must be positive")
            params["expected_count"] = expected_count
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
        return isinstance(rows, list), isinstance(rows, list) and not rows, count
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


def _fallback_quality(
    quality: dict,
    *,
    provider: str,
    primary: str,
    missing_fields: list[str] | None = None,
) -> dict:
    if provider == primary:
        return quality
    result = dict(quality)
    if result.get("status") == "normal":
        result["status"] = "partial"
    result["semantic_equivalence"] = "unknown"
    reasons = list(result.get("reason_codes", []))
    if "fallback_source" not in reasons:
        reasons.append("fallback_source")
    result["reason_codes"] = reasons
    missing = list(result.get("missing", []))
    for field in missing_fields or []:
        if field not in missing:
            missing.append(field)
    result["missing"] = missing
    result["missing_count"] = len(missing)
    return result


def _query_summary(quality: dict) -> dict:
    returned_count = int(quality.get("returned_count", 0) or 0)
    status = str(quality.get("status", "normal"))
    return {
        "total_queries": 1,
        "nonempty_queries": int(returned_count > 0),
        "empty_queries": int(status == "empty"),
        "error_queries": int(status == "error"),
        "semantic_degraded_queries": int(status == "semantic_degraded"),
        "partial_queries": int(status == "partial"),
        "normal_queries": int(status == "normal"),
        "batch_status": status,
    }


def _normalize_review_sentiment(
    params: dict,
    data: dict,
    *,
    provider: str,
    primary: str,
    source_chain: list[str],
) -> tuple[dict, dict]:
    from .v2.aggregates import aggregate_review_sentiment
    from .v2.quality import assess_quality

    query_value = params.get("query")
    if query_value is not None:
        rows = data.get("datas", [])
        if not isinstance(rows, list):
            rows = []
        missing = data.get("missing", [])
        if not isinstance(missing, list):
            missing = []
        quality = assess_quality(
            rows,
            expected_row_shape=params.get("expected_row_shape"),
            expected_count=params.get("expected_count"),
            missing=missing,
        )
        quality = _fallback_quality(
            quality,
            provider=provider,
            primary=primary,
        )
        raw_meta = data.get("_meta", {}) if isinstance(data.get("_meta"), dict) else {}
        query_meta = dict(raw_meta)
        query_meta.update(
            {
                "provider": provider,
                "source": provider,
                "source_chain": list(source_chain),
                "quality": quality,
            }
        )
        if params.get("expected_count") is not None:
            query_meta["coverage"] = {
                "requested_count": quality["requested_count"],
                "returned_count": quality["returned_count"],
                "ratio": quality["coverage"],
            }
        query_item = {
            "query": query_value,
            "result": {key: value for key, value in data.items() if key != "_meta"},
            "_meta": query_meta,
        }
        normalized = {key: value for key, value in data.items() if key != "_meta"}
        normalized["queries"] = [query_item]
        normalized["query_count"] = 1
        normalized["query_summary"] = _query_summary(quality)
        aggregates = aggregate_review_sentiment([query_item])
        for key in ("涨停收益均值", "红盘率", "炸板率", "最高板"):
            normalized[key] = aggregates.get(key)
        normalized["aggregates"] = {
            key: value
            for key, value in aggregates.items()
            if key not in {"涨停收益均值", "红盘率", "炸板率", "最高板"}
        }
        return normalized, quality

    if "_total" in data:
        def count(keys: tuple[str, ...]) -> int:
            total = 0
            for key in keys:
                try:
                    total += int(float(data.get(key, 0) or 0))
                except (TypeError, ValueError):
                    continue
            return total

        up_count = count(("涨停", ">7%", "5~7%", "3~5%", "0~3%"))
        down_count = count(("-0~-3%", "-3~-5%", "-5~-7%", "<-7%", "跌停"))
        directional_total = up_count + down_count
        red_rate = round(up_count / directional_total * 100, 2) if directional_total else None
        exact_limits = provider == "pytdx_breadth"
        limit_up = count(("涨停",)) if exact_limits else None
        limit_down = count(("跌停",)) if exact_limits else None
        row = {
            "上涨家数": up_count,
            "下跌家数": down_count,
            "涨停家数": limit_up,
            "跌停家数": limit_down,
            "红盘率": red_rate,
        }
        missing = ["涨停收益均值", "炸板率", "最高板"]
        if not exact_limits:
            missing.extend(["涨停家数", "跌停家数"])
        quality = assess_quality([row], missing=missing)
        quality = _fallback_quality(quality, provider=provider, primary=primary)
        query_item = {
            "query": "全市场涨跌分布",
            "result": {"datas": [row], "row_count": 1, "breadth": dict(data)},
            "_meta": {
                "provider": provider,
                "source": provider,
                "source_chain": list(source_chain),
                "quality": quality,
            },
        }
        normalized = {
            "queries": [query_item],
            "query_count": 1,
            "query_summary": _query_summary(quality),
            "涨停收益均值": None,
            "红盘率": red_rate,
            "炸板率": None,
            "最高板": None,
            "涨停家数": limit_up,
            "跌停家数": limit_down,
            "上涨家数": up_count,
            "下跌家数": down_count,
            "aggregates": {
                "red_rate": red_rate,
                "limit_up_count": limit_up,
                "limit_down_count": limit_down,
                "up_count": up_count,
                "down_count": down_count,
                "breadth": dict(data),
            },
        }
        return normalized, quality

    row = {
        "涨停家数": data.get("zt_count"),
        "跌停家数": data.get("dt_count"),
        "炸板率": data.get("break_rate"),
        "最高板": data.get("max_board"),
    }
    quality = assess_quality(
        [row],
        missing=["上涨家数", "下跌家数", "红盘率", "涨停收益均值"],
    )
    quality = _fallback_quality(quality, provider=provider, primary=primary)
    normalized = dict(data)
    normalized.update(
        {
            "queries": [{
                "query": "涨跌停池聚合",
                "result": dict(data),
                "_meta": {
                    "provider": provider,
                    "source": provider,
                    "source_chain": list(source_chain),
                    "quality": quality,
                },
            }],
            "query_count": 1,
            "query_summary": _query_summary(quality),
            "涨停收益均值": None,
            "红盘率": None,
            "炸板率": data.get("break_rate"),
            "最高板": data.get("max_board"),
            "涨停家数": data.get("zt_count"),
            "跌停家数": data.get("dt_count"),
            "上涨家数": None,
            "下跌家数": None,
            "aggregates": {
                "limit_up_count": data.get("zt_count"),
                "limit_down_count": data.get("dt_count"),
                "failed_limit_rate": data.get("break_rate"),
                "highest_board": data.get("max_board"),
            },
        }
    )
    return normalized, quality


def _normalize_success(
    intent: str,
    params: dict,
    data: dict,
    *,
    provider: str,
    spec: RouteSpec,
    attempts: list[ProviderAttempt],
) -> tuple[dict, dict]:
    from .v2.quality import assess_quality

    source_chain = [attempt.provider for attempt in attempts] + [provider]
    primary = spec.providers[0]
    if intent == "review_sentiment":
        return _normalize_review_sentiment(
            params,
            data,
            provider=provider,
            primary=primary,
            source_chain=source_chain,
        )
    if intent == "stock_snapshot":
        rows = []
        missing = []
        for code in params["codes"]:
            row = data.get(code)
            if isinstance(row, dict) and not row.get("error"):
                rows.append({"股票代码": str(code), **row})
            else:
                missing.append(str(code))
        quality = assess_quality(
            rows,
            expected_row_shape="stock_rows",
            expected_count=len(params["codes"]),
            missing=missing,
        )
        return data, _fallback_quality(quality, provider=provider, primary=primary)
    if intent == "sector_index":
        rows = data.get("items", [])
        missing = data.get("missing", [])
        quality = assess_quality(
            rows if isinstance(rows, list) else [],
            expected_row_shape="sector_rows",
            expected_count=len(params.get("codes") or []) + len(params.get("names") or []),
            missing=missing if isinstance(missing, list) else [],
        )
        return data, _fallback_quality(quality, provider=provider, primary=primary)
    if intent == "stock_kline":
        rows = data.get("bars", [])
        missing_fields = []
        if provider == "tencent" and any(
            isinstance(row, dict) and row.get("amount") is None
            for row in rows if isinstance(rows, list)
        ):
            missing_fields.append("amount")
        quality = assess_quality(
            rows if isinstance(rows, list) else [],
            expected_count=params.get("count"),
        )
        return data, _fallback_quality(
            quality,
            provider=provider,
            primary=primary,
            missing_fields=missing_fields,
        )
    container = {
        "stock_event": "items",
        "research": "reports",
        "filings": "filings",
        "news": "items",
    }.get(intent)
    if container:
        rows = data.get(container, [])
        quality = assess_quality(rows if isinstance(rows, list) else [])
    else:
        quality = assess_quality([data])
    return data, _fallback_quality(quality, provider=provider, primary=primary)


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
    final_quality: dict | None = None

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
            provenance = outcome.provenance or {}
            verified_fallback = (
                provenance.get("verified") is True
                and provenance.get("fallback_from") == provider_name
                and provenance.get("kind") == "source_internal"
            )
            if actual_provider not in spec.providers or not verified_fallback:
                attempts.append(
                    ProviderAttempt(
                        provider_name,
                        "provider_error",
                        "INCOMPATIBLE_PROVIDER",
                        max(0, int(outcome.latency_ms)),
                    )
                )
                continue
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
            normalized_data, semantic_quality = _normalize_success(
                intent,
                call_params,
                outcome.data,
                provider=actual_provider,
                spec=spec,
                attempts=attempts,
            )
            attempts.append(
                ProviderAttempt(
                    actual_provider,
                    terminal_status,
                    None,
                    max(0, int(outcome.latency_ms)),
                )
            )
            provider_used = actual_provider
            data = normalized_data
            final_count = count
            final_outcome = outcome
            fetched_at = outcome.fetched_at
            auth = outcome.auth
            final_quality = semantic_quality
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

    quality = final_quality or _quality(
        final_outcome, status=final_status, count=final_count
    )
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
