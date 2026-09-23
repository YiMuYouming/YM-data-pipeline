"""Normalize v2 sidecar results into a common response shape."""

from datetime import datetime, timedelta, timezone
from typing import Any


TZ_SHANGHAI = timezone(timedelta(hours=8))


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_SHANGHAI)
    return parsed.astimezone(TZ_SHANGHAI)


def strip_v1_meta(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {"value": raw}
    return {key: value for key, value in raw.items() if key != "_meta"}


def normalize_result(
    *,
    intent: str,
    raw: Any,
    source: str,
    source_chain: list[str],
    data_scope: str,
    staleness_sec: int,
    trade_usage: str,
    query: str | list[str] | None = None,
    now: datetime | None = None,
    quality: dict | None = None,
) -> dict:
    raw_meta = raw.get("_meta", {}) if isinstance(raw, dict) else {}
    fetched_at = raw_meta.get("fetched_at")
    fetched_dt = parse_timestamp(fetched_at)
    now_dt = now or datetime.now(TZ_SHANGHAI)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=TZ_SHANGHAI)
    now_dt = now_dt.astimezone(TZ_SHANGHAI)

    error = bool(
        isinstance(raw, dict)
        and (raw.get("error") or raw_meta.get("error"))
    )

    age_sec = None
    confidence = "normal"
    warn = None
    if error:
        confidence = "error"
        warn = "数据源返回错误"
    elif raw_meta.get("degraded") or raw_meta.get("status") == "degraded":
        confidence = "degraded"
        warn = "主数据源不可用，当前使用降级数据源"
    elif fetched_dt is None:
        confidence = "unknown"
        warn = "无法取得 fetched_at，无法判断数据新鲜度"
    else:
        age_sec = max(0, int((now_dt - fetched_dt).total_seconds()))
        if age_sec > staleness_sec:
            confidence = "stale"
            warn = f"数据距今 {age_sec}s，超过阈值 {staleness_sec}s"

    meta = {
        "intent": intent,
        "source": source,
        "source_chain": source_chain,
        "fetched_at": fetched_at,
        "age_sec": age_sec,
        "staleness_sec": staleness_sec,
        "data_scope": data_scope,
        "trade_usage": trade_usage,
        "confidence": confidence,
        "error": error,
    }
    for key in (
        "contract_version",
        "status",
        "provider_used",
        "attempts",
        "auth",
        "freshness",
    ):
        if key in raw_meta:
            meta[key] = raw_meta[key]
    if isinstance(raw_meta.get("source_chain"), list):
        meta["source_chain"] = list(raw_meta["source_chain"])
    if raw_meta.get("source"):
        meta["source"] = raw_meta["source"]
    if isinstance(query, list):
        meta["queries"] = query
    elif query is not None:
        meta["query"] = query
    if warn:
        meta["warn"] = warn
    if quality is not None:
        meta["quality"] = quality

    return {
        "data": strip_v1_meta(raw),
        "_meta": meta,
    }
