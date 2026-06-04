"""MVP intent router for the v2 sidecar pipeline."""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from . import adapters
from .normalize import normalize_result


DEFAULT_REVIEW_SENTIMENT_QUERY = "昨日涨停 今日涨跌幅 非st"
SUPPORTED_INTENTS = ["realtime_market", "review_sentiment", "stock_snapshot"]


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
    raw_meta = raw.get("_meta", {}) if isinstance(raw, dict) else {}
    for key in ("fallback_from", "fallback_to"):
        fallback_source = raw_meta.get(key)
        if fallback_source and fallback_source not in chain:
            chain.append(fallback_source)
    raw_source = raw.get("_source") if isinstance(raw, dict) else None
    if raw_source and raw_source not in chain:
        chain.append(raw_source)
    return chain


def _review_queries(query: str | None = None) -> list[str]:
    if query:
        return [query]

    queries = []
    seen = set()
    for policy in _intent_policies("review_sentiment"):
        policy_query = (policy.get("primary") or {}).get("query")
        if policy_query and policy_query not in seen:
            queries.append(policy_query)
            seen.add(policy_query)
    return queries or [DEFAULT_REVIEW_SENTIMENT_QUERY]


def _merge_source_chains(chains: list[list[str]]) -> list[str]:
    merged = []
    for chain in chains:
        for source in chain:
            if source and source not in merged:
                merged.append(source)
    return merged


def resolve(intent: str, *, _now: datetime | None = None, **kwargs) -> dict:
    """Resolve a business intent through the v2 sidecar.

    v2.0 MVP intentionally supports a small intent set. Production consumers
    continue using v1 until v2.2 migration work.
    """
    if intent == "realtime_market":
        raw = adapters.fetch_index()
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

    if intent == "stock_snapshot":
        codes = kwargs.get("codes")
        if isinstance(codes, str):
            codes = [codes]
        if not codes:
            raise ValueError("stock_snapshot 需要提供 codes，例如 codes=['002475']")

        raw = adapters.fetch_quotes(codes)
        source = raw.get("_meta", {}).get("source", "pytdx") if isinstance(raw, dict) else "pytdx"
        return normalize_result(
            intent=intent,
            raw=raw,
            source=source,
            source_chain=_source_chain(source, raw if isinstance(raw, dict) else {}),
            data_scope=_intent_data_scope(intent, "PyTDX个股实时行情口径"),
            staleness_sec=_intent_staleness(intent),
            trade_usage=_intent_trade_usage(intent),
            now=_now,
        )

    if intent == "review_sentiment":
        queries = _review_queries(kwargs.get("query"))
        limit = int(kwargs.get("limit", 50))
        rows = []
        chains = []
        fetched_at = None
        has_error = False
        for query in queries:
            raw = adapters.query_iwencai(query, limit=limit)
            raw_dict = raw if isinstance(raw, dict) else {"data": raw}
            raw_meta = raw_dict.get("_meta", {})
            source = raw_meta.get("source", "iwencai")
            chains.append(_source_chain(source, raw_dict))
            if raw_meta.get("fetched_at"):
                fetched_at = raw_meta["fetched_at"]
            if raw_dict.get("error") or raw_meta.get("error"):
                has_error = True
            rows.append({
                "query": query,
                "result": {key: value for key, value in raw_dict.items() if key != "_meta"},
            })

        raw = {
            "queries": rows,
            "query_count": len(rows),
            "_meta": {
                "data_type": "iwencai",
                "source": "iwencai",
                "fetched_at": fetched_at,
                "error": has_error,
            },
        }
        return normalize_result(
            intent=intent,
            raw=raw,
            source="iwencai",
            source_chain=_merge_source_chains(chains) or ["iwencai"],
            data_scope=_intent_data_scope(intent, "问财口径"),
            staleness_sec=_intent_staleness(intent),
            trade_usage=_intent_trade_usage(intent),
            query=queries,
            now=_now,
        )

    raise ValueError(
        f"v2.0 MVP 暂不支持 intent: {intent}. "
        f"当前支持: {SUPPORTED_INTENTS}"
    )
