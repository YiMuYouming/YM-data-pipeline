"""MVP intent router for the v2 sidecar pipeline."""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from . import adapters
from .normalize import normalize_result


DEFAULT_REVIEW_SENTIMENT_QUERY = "昨日涨停 今日涨跌幅 非st"


@lru_cache(maxsize=1)
def _load_fields() -> list[dict]:
    path = Path(__file__).resolve().parent / "policies" / "fields.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _intent_policies(intent: str) -> list[dict]:
    return [item for item in _load_fields() if item.get("intent") == intent]


def _intent_staleness(intent: str) -> int:
    values = [
        int(item["staleness_sec"])
        for item in _intent_policies(intent)
        if item.get("staleness_sec") is not None
    ]
    return min(values) if values else 300


def _intent_data_scope(intent: str, default: str) -> str:
    scopes = {
        item.get("data_scope")
        for item in _intent_policies(intent)
        if item.get("data_scope")
    }
    if len(scopes) == 1:
        return next(iter(scopes))
    return default


def _intent_trade_usage(intent: str) -> str:
    usages = {
        item.get("trade_usage")
        for item in _intent_policies(intent)
        if item.get("trade_usage")
    }
    if len(usages) == 1:
        return next(iter(usages))
    return "辅助，不单独触发交易"


def _source_chain(source: str, raw: dict) -> list[str]:
    chain = [source]
    raw_source = raw.get("_source") if isinstance(raw, dict) else None
    if raw_source and raw_source not in chain:
        chain.append(raw_source)
    return chain


def resolve(intent: str, *, _now: datetime | None = None, **kwargs) -> dict:
    """Resolve a business intent through the v2 sidecar.

    v2.0 MVP intentionally supports only two intents. Production consumers
    continue using v1 fetch() until v2.2 migration work.
    """
    if intent == "realtime_market":
        raw = adapters.fetch_v1("index")
        source = raw.get("_meta", {}).get("source", "pytdx") if isinstance(raw, dict) else "pytdx"
        return normalize_result(
            intent=intent,
            raw=raw,
            source=source,
            source_chain=_source_chain(source, raw if isinstance(raw, dict) else {}),
            data_scope=_intent_data_scope(intent, "PyTDX实时行情口径"),
            staleness_sec=_intent_staleness(intent),
            trade_usage=_intent_trade_usage(intent),
            now=_now,
        )

    if intent == "review_sentiment":
        query = kwargs.get("query") or DEFAULT_REVIEW_SENTIMENT_QUERY
        limit = int(kwargs.get("limit", 50))
        raw = adapters.fetch_v1("iwencai", query=query, limit=limit)
        source = raw.get("_meta", {}).get("source", "iwencai") if isinstance(raw, dict) else "iwencai"
        return normalize_result(
            intent=intent,
            raw=raw,
            source=source,
            source_chain=_source_chain(source, raw if isinstance(raw, dict) else {}),
            data_scope=_intent_data_scope(intent, "问财口径"),
            staleness_sec=_intent_staleness(intent),
            trade_usage=_intent_trade_usage(intent),
            query=query,
            now=_now,
        )

    raise ValueError(
        f"v2.0 MVP 暂不支持 intent: {intent}. "
        "当前支持: ['realtime_market', 'review_sentiment']"
    )
