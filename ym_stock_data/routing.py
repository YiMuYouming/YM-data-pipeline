"""Deterministic, capability-specific provider routing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteSpec:
    intent: str
    providers: tuple[str, ...]
    data_scope: str
    trade_usage: str
    max_age_sec: int


_TRADE_USAGE = "辅助，不单独触发交易"

_ROUTES = {
    "realtime_market": RouteSpec(
        intent="realtime_market",
        providers=("pytdx", "eastmoney", "tencent"),
        data_scope="A股三大指数、成交额与涨跌家数",
        trade_usage=_TRADE_USAGE,
        max_age_sec=60,
    ),
    "sector_index": RouteSpec(
        intent="sector_index",
        providers=("ths_industry",),
        data_scope="同花顺881行业板块指数",
        trade_usage=_TRADE_USAGE,
        max_age_sec=300,
    ),
    "stock_snapshot": RouteSpec(
        intent="stock_snapshot",
        providers=("pytdx", "tencent", "sina", "tdx_quotes"),
        data_scope="A股个股实时行情与标准化报价字段",
        trade_usage=_TRADE_USAGE,
        max_age_sec=60,
    ),
    "market_limit_state": RouteSpec(
        intent="market_limit_state",
        providers=("eastmoney_limit_pool",),
        data_scope="A股涨停、炸板与跌停池聚合",
        trade_usage=_TRADE_USAGE,
        max_age_sec=300,
    ),
    "stock_event": RouteSpec(
        intent="stock_event",
        providers=("eastmoney_datacenter",),
        data_scope="个股白名单低频事件",
        trade_usage=_TRADE_USAGE,
        max_age_sec=86400,
    ),
    "research": RouteSpec(
        intent="research",
        providers=("eastmoney_research", "tdx_report"),
        data_scope="A股研报元数据与报告行",
        trade_usage=_TRADE_USAGE,
        max_age_sec=86400,
    ),
    "filings": RouteSpec(
        intent="filings",
        providers=("cninfo", "tdx_notice", "wind_documents"),
        data_scope="A股公告元数据与文档检索",
        trade_usage=_TRADE_USAGE,
        max_age_sec=86400,
    ),
    "news": RouteSpec(
        intent="news",
        providers=("cls", "tdx_news"),
        data_scope="A股新闻行；重大事实仍需一手来源核实",
        trade_usage=_TRADE_USAGE,
        max_age_sec=1800,
    ),
    "wind_enrichment": RouteSpec(
        intent="wind_enrichment",
        providers=("wind_mcp",),
        data_scope="Wind显式研究增强",
        trade_usage=_TRADE_USAGE,
        max_age_sec=86400,
    ),
}

_STOCK_KLINE_DAILY = RouteSpec(
    intent="stock_kline",
    providers=("pytdx", "tencent", "tdx_kline"),
    data_scope="A股个股日周月K线",
    trade_usage=_TRADE_USAGE,
    max_age_sec=86400,
)
_STOCK_KLINE_MINUTE = RouteSpec(
    intent="stock_kline",
    providers=("pytdx", "sina", "tdx_kline"),
    data_scope="A股个股分钟K线",
    trade_usage=_TRADE_USAGE,
    max_age_sec=300,
)
_REVIEW_SENTIMENT_DEFAULT = RouteSpec(
    intent="review_sentiment",
    providers=("pytdx_breadth", "eastmoney_breadth", "eastmoney_limit_pool"),
    data_scope="A股市场宽度与涨跌停聚合口径",
    trade_usage=_TRADE_USAGE,
    max_age_sec=300,
)
_REVIEW_SENTIMENT_QUERY = RouteSpec(
    intent="review_sentiment",
    providers=("iwencai_openapi", "pywencai", "tdx_screener"),
    data_scope="问财自然语言选股口径",
    trade_usage=_TRADE_USAGE,
    max_age_sec=1800,
)


def route_for(intent: str, params: dict) -> RouteSpec:
    """Return a route using intent semantics only; never inspect provider state."""

    if not isinstance(params, dict):
        raise TypeError("params must be a dict")
    if intent == "review_sentiment":
        if "query" in params and params["query"] not in (None, "", []):
            return _REVIEW_SENTIMENT_QUERY
        return _REVIEW_SENTIMENT_DEFAULT
    if intent == "stock_kline":
        period = str(params.get("period", "daily")).lower()
        if period in {"day", "daily", "week", "weekly", "month", "monthly"}:
            return _STOCK_KLINE_DAILY
        return _STOCK_KLINE_MINUTE
    try:
        return _ROUTES[intent]
    except KeyError as exc:
        raise ValueError(f"unknown intent: {intent}") from exc


def all_route_specs() -> tuple[RouteSpec, ...]:
    """Return every static and parameterized route variant for manifests."""

    return tuple(_ROUTES.values()) + (
        _STOCK_KLINE_DAILY,
        _STOCK_KLINE_MINUTE,
        _REVIEW_SENTIMENT_DEFAULT,
        _REVIEW_SENTIMENT_QUERY,
    )
