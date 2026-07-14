"""MVP intent router for the v2 sidecar pipeline."""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from . import adapters
from .aggregates import aggregate_review_sentiment
from .normalize import normalize_result
from .quality import assess_quality, rollup_qualities


DEFAULT_REVIEW_SENTIMENT_QUERY = "昨日涨停 今日涨跌幅 非st"
SUPPORTED_INTENTS = ["realtime_market", "sector_index", "review_sentiment", "stock_snapshot", "stock_kline"]
SUPPORTED_KLINE_PERIODS = {"daily", "weekly", "monthly", "60m", "15m", "5m"}


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
    raw_meta = raw.get("_meta", {}) if isinstance(raw, dict) else {}
    primary_source = raw_meta.get("fallback_from")
    fallback_to = raw_meta.get("fallback_to")
    chain = [primary_source if source == fallback_to and primary_source else source]
    if source not in chain:
        chain.append(source)
    for key in ("fallback_from", "fallback_to"):
        fallback_source = raw_meta.get(key)
        if fallback_source and fallback_source not in chain:
            chain.append(fallback_source)
    raw_source = raw.get("_source") if isinstance(raw, dict) else None
    if raw_source and raw_source not in chain:
        chain.append(raw_source)
    return chain


def _fallback_quality(
    quality: dict,
    *,
    source: str,
    primary: str,
    missing_fields: list[str] | None = None,
) -> dict:
    if source == primary:
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


def _review_queries(query: str | list[str] | tuple[str, ...] | None = None) -> list[str]:
    if isinstance(query, (list, tuple)):
        queries = []
        seen = set()
        for item in query:
            value = str(item).strip()
            if value and value not in seen:
                queries.append(value)
                seen.add(value)
        if not queries:
            raise ValueError("review_sentiment query 列表不能为空")
        return queries
    if query:
        return [str(query)]

    queries = []
    seen = set()
    for policy in _intent_policies("review_sentiment"):
        policy_query = (policy.get("primary") or {}).get("query")
        if policy_query and policy_query not in seen:
            queries.append(policy_query)
            seen.add(policy_query)
    return queries or [DEFAULT_REVIEW_SENTIMENT_QUERY]


def _breadth_count(raw: dict, keys: tuple[str, ...]) -> int:
    total = 0
    for key in keys:
        value = raw.get(key, 0)
        try:
            total += int(float(value or 0))
        except (TypeError, ValueError):
            continue
    return total


def _resolve_review_sentiment_from_breadth(raw: dict, *, now: datetime | None) -> dict | None:
    raw_meta = raw.get("_meta", {}) if isinstance(raw, dict) else {}
    source = raw_meta.get("source", "pytdx")
    total = _breadth_count(raw, ("_total",))
    if total <= 0 or _source_error(raw):
        return None

    up_count = _breadth_count(raw, ("涨停", ">7%", "5~7%", "3~5%", "0~3%"))
    down_count = _breadth_count(raw, ("-0~-3%", "-3~-5%", "-5~-7%", "<-7%", "跌停"))
    directional_total = up_count + down_count
    red_rate = round(up_count / directional_total * 100, 2) if directional_total else None
    exact_limit_counts = source == "pytdx"
    limit_up_count = _breadth_count(raw, ("涨停",)) if exact_limit_counts else None
    limit_down_count = _breadth_count(raw, ("跌停",)) if exact_limit_counts else None

    row = {
        "上涨家数": up_count,
        "下跌家数": down_count,
        "涨停家数": limit_up_count,
        "跌停家数": limit_down_count,
        "红盘率": red_rate,
    }
    missing = ["涨停收益均值", "炸板率", "最高板"]
    if not exact_limit_counts:
        missing.extend(["涨停家数", "跌停家数"])
    quality = assess_quality([row], missing=missing)
    source_chain = _source_chain(source, raw)
    query_meta = dict(raw_meta)
    query_meta.update({
        "source": source,
        "source_chain": source_chain,
        "quality": quality,
        "zero_auth": True,
    })
    breadth = {
        key: value
        for key, value in raw.items()
        if key not in {"_meta", "_source"}
    }
    result_raw = {
        "queries": [{
            "query": "全市场涨跌分布",
            "result": {
                "datas": [row],
                "row_count": 1,
                "breadth": breadth,
                "_source": source,
            },
            "_meta": query_meta,
        }],
        "query_count": 1,
        "query_summary": {
            "total_queries": 1,
            "nonempty_queries": 1,
            "empty_queries": 0,
            "error_queries": 0,
            "semantic_degraded_queries": 0,
            "partial_queries": 1,
            "normal_queries": 0,
            "batch_status": "partial",
        },
        "涨停收益均值": None,
        "红盘率": red_rate,
        "炸板率": None,
        "最高板": None,
        "涨停家数": limit_up_count,
        "跌停家数": limit_down_count,
        "上涨家数": up_count,
        "下跌家数": down_count,
        "aggregates": {
            "red_rate": red_rate,
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "up_count": up_count,
            "down_count": down_count,
            "breadth": breadth,
        },
        "_meta": {
            **raw_meta,
            "data_type": "review_sentiment",
            "source": source,
        },
    }
    return normalize_result(
        intent="review_sentiment",
        raw=result_raw,
        source=source,
        source_chain=source_chain,
        data_scope=(
            "PyTDX全市场涨跌分布口径"
            if source == "pytdx"
            else f"{source}全市场涨跌分布降级口径"
        ),
        staleness_sec=_intent_staleness("review_sentiment"),
        trade_usage=_intent_trade_usage("review_sentiment"),
        query=["全市场涨跌分布"],
        now=now,
        quality=quality,
    )


def _merge_source_chains(chains: list[list[str]]) -> list[str]:
    merged = []
    for chain in chains:
        for source in chain:
            if source and source not in merged:
                merged.append(source)
    return merged


def _source_error(raw: dict) -> bool:
    raw_meta = raw.get("_meta", {}) if isinstance(raw, dict) else {}
    return bool(raw.get("error") or raw_meta.get("error"))


def _non_meta_payload_exists(raw: dict) -> bool:
    return any(
        key not in {"_meta", "_source", "error", "error_type", "query"}
        for key in raw
    )


def resolve(intent: str, *, _now: datetime | None = None, **kwargs) -> dict:
    """Resolve a business intent through the v2 sidecar.

    v2.0 MVP intentionally supports a small intent set. Production consumers
    continue using v1 until v2.2 migration work.
    """
    if intent == "realtime_market":
        raw = adapters.fetch_index()
        source = raw.get("_meta", {}).get("source", "pytdx") if isinstance(raw, dict) else "pytdx"
        source_error = _source_error(raw)
        quality = assess_quality(
            [raw] if not source_error and _non_meta_payload_exists(raw) else [],
            source_error=source_error,
        )
        quality = _fallback_quality(quality, source=source, primary="pytdx")
        data_scope = {
            "eastmoney": "东方财富指数降级口径",
            "tencent": "腾讯指数降级口径",
        }.get(source, _intent_data_scope(intent, "PyTDX实时行情口径"))
        return normalize_result(
            intent=intent,
            raw=raw,
            source=source,
            source_chain=_source_chain(source, raw if isinstance(raw, dict) else {}),
            data_scope=data_scope,
            staleness_sec=_intent_staleness(intent),
            trade_usage=_intent_trade_usage(intent),
            now=_now,
            quality=quality,
        )

    if intent == "stock_snapshot":
        codes = kwargs.get("codes")
        if isinstance(codes, str):
            codes = [codes]
        if not codes:
            raise ValueError("stock_snapshot 需要提供 codes，例如 codes=['002475']")

        raw = adapters.fetch_quotes(codes)
        source = raw.get("_meta", {}).get("source", "pytdx") if isinstance(raw, dict) else "pytdx"
        quote_rows = []
        missing = []
        for code in codes:
            normalized_code = str(code)
            quote = raw.get(code)
            if quote is None:
                quote = raw.get(normalized_code)
            if isinstance(quote, dict) and not quote.get("error"):
                quote_rows.append({"股票代码": normalized_code, **quote})
            else:
                missing.append(normalized_code)
        quality = assess_quality(
            quote_rows,
            expected_row_shape="stock_rows",
            expected_count=len(codes),
            missing=missing,
            source_error=_source_error(raw),
        )
        quality = _fallback_quality(quality, source=source, primary="pytdx")
        data_scope = {
            "tencent": "腾讯个股行情降级口径",
            "sina": "新浪个股行情降级口径",
        }.get(source, _intent_data_scope(intent, "PyTDX个股实时行情口径"))
        return normalize_result(
            intent=intent,
            raw=raw,
            source=source,
            source_chain=_source_chain(source, raw if isinstance(raw, dict) else {}),
            data_scope=data_scope,
            staleness_sec=_intent_staleness(intent),
            trade_usage=_intent_trade_usage(intent),
            now=_now,
            quality=quality,
        )

    if intent == "sector_index":
        codes = kwargs.get("codes")
        names = kwargs.get("names")
        if isinstance(codes, str):
            codes = [codes]
        if isinstance(names, str):
            names = [names]
        if not codes and not names:
            raise ValueError("sector_index 需要提供 codes 或 names，例如 codes=['881124']")
        invalid_codes = [str(code) for code in codes or [] if not str(code).startswith("881")]
        if invalid_codes:
            raise ValueError(f"sector_index 只接受同花顺 881xxx 代码，不接受: {invalid_codes}")

        raw = adapters.fetch_sector_index(codes=codes, names=names)
        source = raw.get("_meta", {}).get("source", "ths_industry") if isinstance(raw, dict) else "ths_industry"
        sector_rows = raw.get("items", [])
        if not isinstance(sector_rows, list):
            sector_rows = []
        missing = raw.get("missing", [])
        if not isinstance(missing, list):
            missing = []
        quality = assess_quality(
            sector_rows,
            expected_row_shape="sector_rows",
            expected_count=len(codes or []) + len(names or []),
            missing=missing,
            source_error=_source_error(raw),
        )
        return normalize_result(
            intent=intent,
            raw=raw,
            source=source,
            source_chain=_source_chain(source, raw if isinstance(raw, dict) else {}),
            data_scope=_intent_data_scope(intent, "同花顺881行业板块口径"),
            staleness_sec=_intent_staleness(intent),
            trade_usage=_intent_trade_usage(intent),
            now=_now,
            quality=quality,
        )

    if intent == "stock_kline":
        code = kwargs.get("code")
        if not code:
            raise ValueError("stock_kline 需要提供 code，例如 code='002475'")
        period = kwargs.get("period", "daily")
        if period not in SUPPORTED_KLINE_PERIODS:
            raise ValueError(
                "stock_kline period 仅支持 "
                f"{sorted(SUPPORTED_KLINE_PERIODS)}"
            )
        count = kwargs.get("count")
        if count is not None:
            count = int(count)
            if count <= 0:
                raise ValueError("stock_kline count 必须大于 0")

        raw = adapters.fetch_kline(str(code), period=period, count=count)
        source = raw.get("_meta", {}).get("source", "pytdx") if isinstance(raw, dict) else "pytdx"
        bars = raw.get("bars", [])
        if not isinstance(bars, list):
            bars = []
        quality = assess_quality(
            bars,
            expected_count=count,
            source_error=_source_error(raw),
        )
        missing_fields = []
        if source == "tencent" and any(bar.get("amount") is None for bar in bars):
            missing_fields.append("amount")
        quality = _fallback_quality(
            quality,
            source=source,
            primary="pytdx",
            missing_fields=missing_fields,
        )
        data_scope = {
            "tencent": "腾讯前复权K线降级口径",
            "sina": "新浪分钟K线降级口径",
        }.get(source, _intent_data_scope(intent, "PyTDX个股K线口径"))
        return normalize_result(
            intent=intent,
            raw=raw,
            source=source,
            source_chain=_source_chain(source, raw if isinstance(raw, dict) else {}),
            data_scope=data_scope,
            staleness_sec=_intent_staleness(intent),
            trade_usage=_intent_trade_usage(intent),
            now=_now,
            quality=quality,
        )

    if intent == "review_sentiment":
        query_override = kwargs.get("query")
        if not query_override:
            breadth_result = _resolve_review_sentiment_from_breadth(
                adapters.fetch_breadth(),
                now=_now,
            )
            if breadth_result is not None:
                return breadth_result

        queries = (
            _review_queries(query_override)
            if query_override
            else [DEFAULT_REVIEW_SENTIMENT_QUERY]
        )
        limit = int(kwargs.get("limit", 50))
        expected_row_shape = kwargs.get("expected_row_shape")
        expected_count = kwargs.get("expected_count")
        if expected_count is not None:
            expected_count = int(expected_count)
        rows = []
        chains = []
        qualities = []
        fetched_at = None
        has_error = False
        for query in queries:
            raw = adapters.query_iwencai(query, limit=limit)
            raw_dict = raw if isinstance(raw, dict) else {"data": raw}
            raw_meta = raw_dict.get("_meta", {})
            source = raw_meta.get("source", "iwencai")
            source_chain = _source_chain(source, raw_dict)
            chains.append(source_chain)
            if raw_meta.get("fetched_at"):
                fetched_at = raw_meta["fetched_at"]
            source_error = _source_error(raw_dict)
            if source_error:
                has_error = True
            query_rows = raw_dict.get("datas", [])
            if not isinstance(query_rows, list):
                query_rows = []
            missing = raw_dict.get("missing", [])
            if not isinstance(missing, list):
                missing = []
            query_quality = assess_quality(
                query_rows,
                expected_row_shape=expected_row_shape,
                expected_count=expected_count,
                missing=missing,
                source_error=source_error,
            )
            qualities.append(query_quality)
            query_meta = dict(raw_meta)
            query_meta.setdefault("source", source)
            query_meta["source_chain"] = source_chain
            query_meta["quality"] = query_quality
            if expected_count is not None:
                query_meta["coverage"] = {
                    "requested_count": query_quality["requested_count"],
                    "returned_count": query_quality["returned_count"],
                    "ratio": query_quality["coverage"],
                }
            rows.append({
                "query": query,
                "result": {key: value for key, value in raw_dict.items() if key != "_meta"},
                "_meta": query_meta,
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
        status_counts = {}
        for item in qualities:
            status = str(item.get("status", "normal"))
            status_counts[status] = status_counts.get(status, 0) + 1
        nonempty_queries = sum(
            1 for item in qualities if int(item.get("returned_count", 0)) > 0
        )
        if status_counts.get("error", 0) == len(qualities):
            batch_status = "error"
        elif nonempty_queries and (
            status_counts.get("empty", 0) or status_counts.get("error", 0)
        ):
            batch_status = "partial_success"
        elif status_counts.get("empty", 0) == len(qualities):
            batch_status = "empty"
        elif status_counts.get("semantic_degraded", 0):
            batch_status = "semantic_degraded"
        elif status_counts.get("partial", 0):
            batch_status = "partial"
        else:
            batch_status = "normal"
        query_summary = {
            "total_queries": len(qualities),
            "nonempty_queries": nonempty_queries,
            "empty_queries": status_counts.get("empty", 0),
            "error_queries": status_counts.get("error", 0),
            "semantic_degraded_queries": status_counts.get("semantic_degraded", 0),
            "partial_queries": status_counts.get("partial", 0),
            "normal_queries": status_counts.get("normal", 0),
            "batch_status": batch_status,
        }
        raw["query_summary"] = query_summary
        aggregates = aggregate_review_sentiment(rows)
        raw.update({key: value for key, value in aggregates.items() if key in ("涨停收益均值", "红盘率", "炸板率", "最高板")})
        for key in ("涨停收益均值", "红盘率", "炸板率", "最高板"):
            raw.setdefault(key, None)
        raw["aggregates"] = {
            key: value
            for key, value in aggregates.items()
            if key not in ("涨停收益均值", "红盘率", "炸板率", "最高板")
        }
        rolled_quality = rollup_qualities(qualities)
        if len(qualities) > 1:
            rolled_quality["batch_status"] = batch_status
            rolled_quality["query_counts"] = query_summary
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
            quality=rolled_quality,
        )

    raise ValueError(
        f"v2.0 MVP 暂不支持 intent: {intent}. "
        f"当前支持: {SUPPORTED_INTENTS}"
    )
