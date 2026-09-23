"""Thin compatibility projection over :func:`ym_stock_data.query`."""

from __future__ import annotations

from datetime import datetime

import ym_stock_data.api as public_api

from ..aggregates import aggregate_review_sentiment
from ..quality import rollup_qualities
from .normalize import TZ_SHANGHAI, parse_timestamp


DEFAULT_REVIEW_SENTIMENT_QUERY = "昨日涨停 今日涨跌幅 非st"
SUPPORTED_INTENTS = [
    "realtime_market",
    "sector_index",
    "review_sentiment",
    "stock_snapshot",
    "stock_kline",
    "market_limit_state",
    "stock_event",
]
SUPPORTED_KLINE_PERIODS = {"daily", "weekly", "monthly", "60m", "15m", "5m"}
_COMPAT_STALENESS = {
    "realtime_market": 60,
    "sector_index": 300,
    "review_sentiment": 1800,
    "stock_snapshot": 60,
    "stock_kline": 300,
    "market_limit_state": 300,
    "stock_event": 86400,
}


def _queries(value: list[str] | tuple[str, ...]) -> list[str]:
    result = []
    seen = set()
    for item in value:
        query_value = str(item).strip()
        if query_value and query_value not in seen:
            result.append(query_value)
            seen.add(query_value)
    if not result:
        raise ValueError("review_sentiment query 列表不能为空")
    return result


def _confidence(meta: dict, *, intent: str, now: datetime | None) -> dict:
    result = dict(meta)
    fetched_at = parse_timestamp(result.get("fetched_at"))
    now_dt = now or datetime.now(TZ_SHANGHAI)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=TZ_SHANGHAI)
    now_dt = now_dt.astimezone(TZ_SHANGHAI)
    max_age = _COMPAT_STALENESS[intent]
    age_sec = max(0, int((now_dt - fetched_at).total_seconds())) if fetched_at else None
    status = result.get("status")
    if status == "error":
        confidence = "error"
        warn = "数据源返回错误"
    elif status == "degraded":
        confidence = "degraded"
        warn = "主数据源不可用，当前使用降级数据源"
    elif fetched_at is None:
        confidence = "unknown"
        warn = "无法取得 fetched_at，无法判断数据新鲜度"
    elif age_sec is not None and age_sec > max_age:
        confidence = "stale"
        warn = f"数据距今 {age_sec}s，超过阈值 {max_age}s"
    else:
        confidence = "normal"
        warn = None
    result.update(
        {
            "age_sec": age_sec,
            "staleness_sec": max_age,
            "confidence": confidence,
            "error": status == "error",
        }
    )
    if warn:
        result["warn"] = warn
    return result


def _project(
    result: dict,
    *,
    intent: str,
    now: datetime | None,
    queries: list[str] | None = None,
) -> dict:
    meta = _confidence(result.get("_meta", {}), intent=intent, now=now)
    data = result.get("data")
    if queries is None and intent == "review_sentiment" and isinstance(data, dict):
        items = data.get("queries")
        if isinstance(items, list):
            inferred = [item.get("query") for item in items if isinstance(item, dict)]
            queries = [str(item) for item in inferred if item]
    if queries is not None:
        meta["queries"] = queries
    return {"data": data, "_meta": meta}


def _batch_review(
    queries: list[str],
    *,
    now: datetime | None,
    params: dict,
) -> dict:
    results = [
        public_api.query("review_sentiment", query=query_value, **params)
        for query_value in queries
    ]
    query_items = []
    qualities = []
    attempts = []
    source_chain = []
    providers = []
    for query_value, result in zip(queries, results):
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        meta = result.get("_meta", {})
        items = data.get("queries") if isinstance(data, dict) else None
        if isinstance(items, list) and items:
            query_item = dict(items[0])
            item_meta = dict(query_item.get("_meta", {}))
            item_meta["canonical_meta"] = dict(meta)
            query_item["_meta"] = item_meta
            query_items.append(query_item)
        else:
            query_items.append(
                {
                    "query": query_value,
                    "result": data,
                    "_meta": {
                        "provider": meta.get("provider_used"),
                        "source": meta.get("source"),
                        "source_chain": meta.get("source_chain", []),
                        "quality": meta.get("quality", {}),
                        "canonical_meta": dict(meta),
                    },
                }
            )
        quality = meta.get("quality")
        if isinstance(quality, dict):
            qualities.append(quality)
        attempts.extend(meta.get("attempts", []))
        source_chain.extend(meta.get("source_chain", []))
        if meta.get("provider_used"):
            providers.append(meta["provider_used"])

    rolled = rollup_qualities(qualities)
    status_counts = {}
    for quality in qualities:
        status = str(quality.get("status", "normal"))
        status_counts[status] = status_counts.get(status, 0) + 1
    nonempty = sum(int(quality.get("returned_count", 0) or 0) > 0 for quality in qualities)
    if status_counts.get("error", 0) == len(qualities):
        batch_status = "error"
    elif nonempty and (status_counts.get("empty", 0) or status_counts.get("error", 0)):
        batch_status = "partial_success"
    elif status_counts.get("empty", 0) == len(qualities):
        batch_status = "empty"
    elif status_counts.get("semantic_degraded", 0):
        batch_status = "semantic_degraded"
    elif status_counts.get("partial", 0):
        batch_status = "partial"
    else:
        batch_status = "normal"
    summary = {
        "total_queries": len(qualities),
        "nonempty_queries": nonempty,
        "empty_queries": status_counts.get("empty", 0),
        "error_queries": status_counts.get("error", 0),
        "semantic_degraded_queries": status_counts.get("semantic_degraded", 0),
        "partial_queries": status_counts.get("partial", 0),
        "normal_queries": status_counts.get("normal", 0),
        "batch_status": batch_status,
    }
    aggregates = aggregate_review_sentiment(query_items)
    data = {"queries": query_items, "query_count": len(query_items), "query_summary": summary}
    for key in ("涨停收益均值", "红盘率", "炸板率", "最高板"):
        data[key] = aggregates.get(key)
    data["aggregates"] = {
        key: value
        for key, value in aggregates.items()
        if key not in {"涨停收益均值", "红盘率", "炸板率", "最高板"}
    }
    first_meta = dict(results[0].get("_meta", {}))
    providers_used = list(dict.fromkeys(providers))
    first_meta.pop("contract_version", None)
    first_meta.pop("provider_used", None)
    first_meta.pop("source", None)
    first_meta.update(
        {
            "status": "error" if batch_status == "error" else (
                "empty" if batch_status == "empty" else (
                    "degraded" if batch_status != "normal" else "success"
                )
            ),
            "compatibility_contract": "v2-review-batch",
            "providers_used": providers_used,
            "source_chain": source_chain,
            "attempts": attempts,
            "quality": rolled,
        }
    )
    return _project(
        {"data": data, "_meta": first_meta},
        intent="review_sentiment",
        now=now,
        queries=queries,
    )


def resolve(intent: str, *, _now: datetime | None = None, **kwargs) -> dict:
    """Validate compatibility parameters and delegate only to canonical query."""

    if intent not in SUPPORTED_INTENTS:
        raise ValueError(
            f"v2 compatibility wrapper 暂不支持 intent: {intent}. 当前支持: {SUPPORTED_INTENTS}"
        )
    query_value = kwargs.get("query") if intent == "review_sentiment" else None
    if isinstance(query_value, (list, tuple)):
        params = dict(kwargs)
        params.pop("query", None)
        return _batch_review(_queries(query_value), now=_now, params=params)
    result = public_api.query(intent, **kwargs)
    queries = [query_value] if isinstance(query_value, str) else None
    return _project(result, intent=intent, now=_now, queries=queries)
