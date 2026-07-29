"""Intent-specific data and quality normalization for the canonical router."""

from __future__ import annotations

from datetime import datetime

from .aggregates import aggregate_review_sentiment
from .contracts import TZ_SHANGHAI, ProviderAttempt
from .quality import assess_quality
from .routing import RouteSpec


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


def _summary(quality: dict) -> dict:
    returned = int(quality.get("returned_count", 0) or 0)
    status = str(quality.get("status", "normal"))
    return {
        "total_queries": 1,
        "nonempty_queries": int(returned > 0),
        "empty_queries": int(status == "empty"),
        "error_queries": int(status == "error"),
        "semantic_degraded_queries": int(status == "semantic_degraded"),
        "partial_queries": int(status == "partial"),
        "normal_queries": int(status == "normal"),
        "batch_status": status,
    }


def _query_meta(
    *,
    data: dict,
    provider: str,
    source_chain: list[str],
    attempts: list[ProviderAttempt],
    quality: dict,
) -> dict:
    raw_meta = data.get("_meta", {}) if isinstance(data.get("_meta"), dict) else {}
    meta = dict(raw_meta)
    meta.update(
        {
            "provider": provider,
            "source": provider,
            "source_chain": source_chain,
            "quality": quality,
            "query_time": datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds"),
        }
    )
    if attempts:
        code = attempts[-1].error_code or ""
        if code.startswith("HTTP_5"):
            meta["fallback_reason"] = "http_5xx"
        elif code in {"HTTP_401", "HTTP_403"}:
            meta["fallback_reason"] = "auth_error"
        elif code == "HTTP_429":
            meta["fallback_reason"] = "rate_limit"
        elif code:
            meta["fallback_reason"] = code.lower()
    return meta


def _review_query(
    params: dict,
    data: dict,
    *,
    provider: str,
    primary: str,
    source_chain: list[str],
    attempts: list[ProviderAttempt],
) -> tuple[dict, dict]:
    rows = data.get("datas", [])
    rows = rows if isinstance(rows, list) else []
    missing = data.get("missing", [])
    missing = missing if isinstance(missing, list) else []
    quality = assess_quality(
        rows,
        expected_row_shape=params.get("expected_row_shape"),
        expected_count=params.get("expected_count"),
        missing=missing,
    )
    quality = _fallback_quality(quality, provider=provider, primary=primary)
    meta = _query_meta(
        data=data,
        provider=provider,
        source_chain=source_chain,
        attempts=attempts,
        quality=quality,
    )
    if params.get("expected_count") is not None:
        meta["coverage"] = {
            "requested_count": quality["requested_count"],
            "returned_count": quality["returned_count"],
            "ratio": quality["coverage"],
        }
    query_item = {
        "query": params["query"],
        "result": {key: value for key, value in data.items() if key != "_meta"},
        "_meta": meta,
    }
    normalized = {key: value for key, value in data.items() if key != "_meta"}
    normalized.update(
        {"queries": [query_item], "query_count": 1, "query_summary": _summary(quality)}
    )
    aggregates = aggregate_review_sentiment([query_item])
    for key in ("涨停收益均值", "红盘率", "炸板率", "最高板"):
        normalized[key] = aggregates.get(key)
    normalized["aggregates"] = {
        key: value
        for key, value in aggregates.items()
        if key not in {"涨停收益均值", "红盘率", "炸板率", "最高板"}
    }
    return normalized, quality


def _breadth_count(data: dict, keys: tuple[str, ...]) -> int:
    total = 0
    for key in keys:
        try:
            total += int(float(data.get(key, 0) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _review_breadth(
    data: dict,
    *,
    provider: str,
    primary: str,
    source_chain: list[str],
) -> tuple[dict, dict]:
    up = _breadth_count(data, ("涨停", ">7%", "5~7%", "3~5%", "0~3%"))
    down = _breadth_count(data, ("-0~-3%", "-3~-5%", "-5~-7%", "<-7%", "跌停"))
    red_rate = round(up / (up + down) * 100, 2) if up + down else None
    exact = provider == "pytdx_breadth"
    limit_up = _breadth_count(data, ("涨停",)) if exact else None
    limit_down = _breadth_count(data, ("跌停",)) if exact else None
    row = {
        "上涨家数": up,
        "下跌家数": down,
        "涨停家数": limit_up,
        "跌停家数": limit_down,
        "红盘率": red_rate,
    }
    missing = ["涨停收益均值", "炸板率", "最高板"]
    if not exact:
        missing.extend(["涨停家数", "跌停家数"])
    quality = assess_quality([row], missing=missing)
    quality = _fallback_quality(quality, provider=provider, primary=primary)
    item = {
        "query": "全市场涨跌分布",
        "result": {"datas": [row], "row_count": 1, "breadth": dict(data)},
        "_meta": {
            "provider": provider,
            "source": provider,
            "source_chain": source_chain,
            "quality": quality,
        },
    }
    normalized = {
        "queries": [item],
        "query_count": 1,
        "query_summary": _summary(quality),
        "涨停收益均值": None,
        "红盘率": red_rate,
        "炸板率": None,
        "最高板": None,
        "涨停家数": limit_up,
        "跌停家数": limit_down,
        "上涨家数": up,
        "下跌家数": down,
        "aggregates": {
            "red_rate": red_rate,
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "up_count": up,
            "down_count": down,
            "breadth": dict(data),
        },
    }
    return normalized, quality


def _review_limit(
    data: dict,
    *,
    provider: str,
    primary: str,
    source_chain: list[str],
) -> tuple[dict, dict]:
    row = {
        "涨停家数": data.get("zt_count"),
        "跌停家数": data.get("dt_count"),
        "炸板率": data.get("break_rate"),
        "最高板": data.get("max_board"),
    }
    quality = assess_quality(
        [row], missing=["上涨家数", "下跌家数", "红盘率", "涨停收益均值"]
    )
    quality = _fallback_quality(quality, provider=provider, primary=primary)
    item = {
        "query": "涨跌停池聚合",
        "result": dict(data),
        "_meta": {
            "provider": provider,
            "source": provider,
            "source_chain": source_chain,
            "quality": quality,
        },
    }
    normalized = dict(data)
    normalized.update(
        {
            "queries": [item],
            "query_count": 1,
            "query_summary": _summary(quality),
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


def normalize_success(
    intent: str,
    params: dict,
    data: dict,
    *,
    provider: str,
    spec: RouteSpec,
    attempts: list[ProviderAttempt],
) -> tuple[dict, dict]:
    """Return normalized business data and canonical semantic quality."""

    primary = spec.providers[0]
    source_chain = [attempt.provider for attempt in attempts] + [provider]
    if intent == "review_sentiment":
        if params.get("query") is not None:
            return _review_query(
                params,
                data,
                provider=provider,
                primary=primary,
                source_chain=source_chain,
                attempts=attempts,
            )
        if "_total" in data:
            return _review_breadth(
                data, provider=provider, primary=primary, source_chain=source_chain
            )
        return _review_limit(
            data, provider=provider, primary=primary, source_chain=source_chain
        )
    if intent == "stock_snapshot":
        rows, missing = [], []
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
    elif intent == "sector_index":
        rows = data.get("items", [])
        missing = data.get("missing", [])
        quality = assess_quality(
            rows if isinstance(rows, list) else [],
            expected_row_shape="sector_rows",
            expected_count=len(params.get("codes") or []) + len(params.get("names") or []),
            missing=missing if isinstance(missing, list) else [],
        )
    elif intent == "stock_kline":
        rows = data.get("bars", [])
        quality = assess_quality(
            rows if isinstance(rows, list) else [], expected_count=params.get("count")
        )
        missing_fields = []
        if provider == "tencent" and any(
            isinstance(row, dict) and row.get("amount") is None
            for row in rows if isinstance(rows, list)
        ):
            missing_fields.append("amount")
        return data, _fallback_quality(
            quality,
            provider=provider,
            primary=primary,
            missing_fields=missing_fields,
        )
    elif intent == "market_limit_state":
        count = sum(int(data.get(key, 0) or 0) for key in ("zt_count", "zb_count", "dt_count"))
        quality = assess_quality([data] if count else [])
    else:
        container = {
            "stock_event": "items",
            "research": "reports",
            "filings": "filings",
            "news": "items",
            "wind_enrichment": "items",
        }.get(intent)
        rows = data.get(container, []) if container else [data]
        quality = assess_quality(rows if isinstance(rows, list) else [])
    return data, _fallback_quality(quality, provider=provider, primary=primary)
