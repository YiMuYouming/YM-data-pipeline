"""Deterministic, capability-specific provider routing."""

from __future__ import annotations

from dataclasses import dataclass

EMPTY_POLICY_STOP = "stop"
EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED = "continue_until_exhausted"


@dataclass(frozen=True)
class RouteSpec:
    intent: str
    providers: tuple[str, ...]
    data_scope: str
    trade_usage: str
    max_age_sec: int
    empty_policy: str = EMPTY_POLICY_STOP


_TRADE_USAGE = "辅助，不单独触发交易"

_REALTIME_POLL = RouteSpec(
    intent="realtime_market",
    providers=("pytdx", "tencent", "eastmoney"),
    data_scope="A股三大指数、成交额与涨跌家数",
    trade_usage=_TRADE_USAGE,
    max_age_sec=60,
    empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
)
_STOCK_SNAPSHOT_POLL = RouteSpec(
    intent="stock_snapshot",
    providers=("tencent", "pytdx", "tdx_quotes"),
    data_scope="A股个股实时行情与标准化报价字段",
    trade_usage=_TRADE_USAGE,
    max_age_sec=60,
    empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
)
_INDEX_INTRADAY_POLL = RouteSpec(
    intent="index_intraday_compare",
    providers=("eastmoney_index", "sina_index", "stocktoday"),
    data_scope="三大指数分钟量价与同时间段参考比较",
    trade_usage=_TRADE_USAGE,
    max_age_sec=300,
    empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
)

_ROUTES = {
    "stocktoday_data": RouteSpec(
        intent="stocktoday_data",
        providers=("stocktoday",),
        data_scope="StockToday 显式只读数据集；时点与完整性见 observation",
        trade_usage=_TRADE_USAGE,
        max_age_sec=60,
    ),
    "realtime_market": RouteSpec(
        intent="realtime_market",
        providers=("stocktoday", "tencent", "pytdx", "eastmoney"),
        data_scope="A股三大指数、成交额与涨跌家数",
        trade_usage=_TRADE_USAGE,
        max_age_sec=60,
        empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
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
        providers=("stocktoday", "tencent", "pytdx", "tdx_quotes"),
        data_scope="A股个股实时行情与标准化报价字段",
        trade_usage=_TRADE_USAGE,
        max_age_sec=60,
        empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
    ),
    "market_limit_state": RouteSpec(
        intent="market_limit_state",
        providers=("eastmoney_limit_pool",),
        data_scope="A股涨停、炸板与跌停池聚合",
        trade_usage=_TRADE_USAGE,
        max_age_sec=300,
    ),
    "market_limit_board": RouteSpec(
        intent="market_limit_board",
        providers=("stocktoday", "eastmoney_limit_pool"),
        data_scope="A股涨停、跌停、炸板或昨日涨停明细",
        trade_usage=_TRADE_USAGE,
        max_age_sec=300,
        empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
    ),
    "market_hot_rank": RouteSpec(
        intent="market_hot_rank",
        providers=("stocktoday",),
        data_scope="StockToday 同花顺或东方财富热榜明细",
        trade_usage=_TRADE_USAGE,
        max_age_sec=300,
        empty_policy=EMPTY_POLICY_STOP,
    ),
    "industry_flow": RouteSpec(
        intent="industry_flow",
        providers=("stocktoday", "ths_industry"),
        data_scope="行业资金流与行业净额明细",
        trade_usage=_TRADE_USAGE,
        max_age_sec=86400,
        empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
    ),
    "fund_flow": RouteSpec(
        intent="fund_flow",
        providers=("stocktoday",),
        data_scope="市场资金流明细",
        trade_usage=_TRADE_USAGE,
        max_age_sec=86400,
        empty_policy=EMPTY_POLICY_STOP,
    ),
    "northbound_flow": RouteSpec(
        intent="northbound_flow",
        providers=("stocktoday", "northbound"),
        data_scope="北向资金流明细；辅助参考，不替代港交所收盘事实",
        trade_usage=_TRADE_USAGE,
        max_age_sec=86400,
        empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
    ),
    "legacy_hot_rank": RouteSpec(
        intent="legacy_hot_rank",
        providers=("stocktoday", "ths_hot"),
        data_scope="旧同花顺热榜与题材归因明细",
        trade_usage=_TRADE_USAGE,
        max_age_sec=86400,
        empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
    ),
    "index_kline": RouteSpec(
        intent="index_kline",
        providers=("stocktoday", "eastmoney_index", "sina_index", "pytdx_index"),
        data_scope="指数历史日/周/月或分钟K线；按日期区间返回",
        trade_usage=_TRADE_USAGE,
        max_age_sec=86400,
        empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
    ),
    "index_intraday_compare": RouteSpec(
        intent="index_intraday_compare",
        providers=("eastmoney_index", "sina_index", "stocktoday"),
        data_scope="三大指数分钟量价与同时间段参考比较",
        trade_usage=_TRADE_USAGE,
        max_age_sec=300,
        empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
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
    providers=("stocktoday", "eastmoney_stock", "tencent", "pytdx", "tdx_kline"),
    data_scope="A股个股日周月K线",
    trade_usage=_TRADE_USAGE,
    max_age_sec=86400,
    empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
)
_STOCK_KLINE_MINUTE = RouteSpec(
    intent="stock_kline",
    providers=("stocktoday", "pytdx", "sina", "tdx_kline"),
    data_scope="A股个股分钟K线",
    trade_usage=_TRADE_USAGE,
    max_age_sec=300,
    empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
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
    providers=(
        "iwencai_openapi",
        "pywencai",
        "tdx_screener",
        "wind_screener",
    ),
    data_scope="问财自然语言选股口径",
    trade_usage=_TRADE_USAGE,
    max_age_sec=1800,
    empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
)


def route_for(intent: str, params: dict) -> RouteSpec:
    """Return a route using intent semantics only; never inspect provider state."""

    if not isinstance(params, dict):
        raise TypeError("params must be a dict")
    if intent in {"stock_snapshot", "stock_kline"} and params.get("source") == "stocktoday":
        return RouteSpec(
            intent=intent, providers=("stocktoday",),
            data_scope="StockToday 显式行情；未复权；时点见 observation",
            trade_usage=_TRADE_USAGE, max_age_sec=60 if intent == "stock_snapshot" else 86400,
        )
    if params.get("use_case") == "realtime_poll":
        if intent == "realtime_market":
            return _REALTIME_POLL
        if intent == "stock_snapshot":
            return _STOCK_SNAPSHOT_POLL
        if intent == "industry_flow":
            base = _ROUTES["industry_flow"]
            return RouteSpec(
                intent=base.intent,
                providers=("ths_industry", "stocktoday"),
                data_scope=base.data_scope,
                trade_usage=base.trade_usage,
                max_age_sec=base.max_age_sec,
                empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
            )
        if intent == "northbound_flow":
            base = _ROUTES["northbound_flow"]
            return RouteSpec(
                intent=base.intent,
                providers=("northbound",),
                data_scope=base.data_scope,
                trade_usage=base.trade_usage,
                max_age_sec=base.max_age_sec,
            )
        if intent == "legacy_hot_rank":
            base = _ROUTES["legacy_hot_rank"]
            return RouteSpec(
                intent=base.intent,
                providers=("ths_hot", "stocktoday"),
                data_scope=base.data_scope,
                trade_usage=base.trade_usage,
                max_age_sec=base.max_age_sec,
                empty_policy=EMPTY_POLICY_CONTINUE_UNTIL_EXHAUSTED,
            )
        if intent == "index_intraday_compare":
            return _INDEX_INTRADAY_POLL
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


def capability_for(intent: str, params: dict) -> str:
    """Map a canonical intent and its parameters to one V3 capability."""

    if not isinstance(params, dict):
        raise TypeError("params must be a dict")
    if intent == "stock_kline":
        period = str(params.get("period", "daily")).lower()
        if period in {"day", "daily"}:
            return "stock_kline_daily"
        if period in {"week", "weekly"}:
            return "stock_kline_weekly"
        if period in {"month", "monthly"}:
            return "stock_kline_monthly"
        if period == "60m":
            return "stock_kline_60m"
        if period == "15m":
            return "stock_kline_15m"
        if period == "5m":
            return "stock_kline_5m"
        if period == "1m":
            return "stock_kline_1m"
    return intent


def all_route_specs() -> tuple[RouteSpec, ...]:
    """Return every static and parameterized route variant for manifests."""

    return tuple(_ROUTES.values()) + (
        route_for("stock_snapshot", {"source": "stocktoday"}),
        _REALTIME_POLL,
        _STOCK_SNAPSHOT_POLL,
        route_for("stock_kline", {"source": "stocktoday"}),
        _STOCK_KLINE_DAILY,
        _STOCK_KLINE_MINUTE,
        _REVIEW_SENTIMENT_DEFAULT,
        _REVIEW_SENTIMENT_QUERY,
        _INDEX_INTRADAY_POLL,
    )
