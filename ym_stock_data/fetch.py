"""统一入口 — fetch() 路由到对应数据源

用法:
    from ym_stock_data import fetch

    # L2 技术分析 (PyTDX)
    fetch("quotes", codes=["688017"])
    fetch("index")
    fetch("breadth")
    fetch("sector_index", names=["算力", "CPO"])
    fetch("kline", code="688017", period="daily")
    fetch("kline_15m")

    # L1 基础行情
    fetch("iwencai", query="涨停 非st")
    fetch("ths_hot")
    fetch("tencent", codes=["688017"])

    # L3 资金流向
    fetch("northbound")
    fetch("dragon_tiger")
    fetch("sector_inflow", top_n=20)
"""

from datetime import datetime
from importlib import import_module


# 路由表: data_type → (模块名, 函数名, {元数据})
_ROUTES = {

    # === L2 技术分析 (PyTDX) ===
    "quotes":        ("pytdx", "fetch_quotes",      {"layer": 2, "desc": "个股实时报价/涨幅/量比/均线"}),
    "index":         ("pytdx", "fetch_index",       {"layer": 2, "desc": "三大指数/涨跌家数/成交额"}),
    "breadth":       ("pytdx", "fetch_breadth",     {"layer": 2, "desc": "全市场涨跌分布(5000+只)"}),
    "sector_index":  ("pytdx", "fetch_sector",      {"layer": 2, "desc": "板块指数880xxx/均线/量价趋势"}),
    "kline":         ("pytdx", "fetch_kline",       {"layer": 2, "desc": "K线+均线(daily/60m/15m)"}),
    "kline_15m":     ("pytdx", "fetch_kline_15m",   {"layer": 2, "desc": "三大指数15分钟量价(同比昨日)"}),

    # === L1 基础行情 ===
    "iwencai":       ("iwencai", "query",           {"layer": 1, "desc": "问财全能查询(自然语言)"}),
    "ths_hot":       ("ths_hot", "fetch_hot_with_zt_count", {"layer": 1, "desc": "同花顺热点+题材归因"}),
    "tencent":       ("tencent", "fetch_quotes",    {"layer": 1, "desc": "腾讯PE/PB/市值/换手率"}),

    # === L3 资金流向 ===
    "northbound":    ("northbound", "fetch_realtime", {"layer": 3, "desc": "北向资金分钟级262点"}),
    "dragon_tiger":  ("eastmoney", "fetch_daily_dragon_tiger", {"layer": 3, "desc": "全市场龙虎榜"}),
    "sector_inflow": ("ths_industry", "fetch_industry_summary", {"layer": 3, "desc": "行业板块净流入(同花顺直连)"}),

    # === L4 研报/公告/新闻 ===
    "research":      ("research", "fetch_reports",    {"layer": 4, "desc": "个股研报(东财reportapi)"}),
    "filings":       ("filings", "fetch_filings",     {"layer": 4, "desc": "公司公告(巨潮cninfo)"}),
    "news":          ("news", "fetch_news",           {"layer": 4, "desc": "实时新闻(财联社电报)"}),
    "limit_state":   ("limit_state", "fetch_limit_state", {"layer": 3, "desc": "涨停/炸板/跌停/昨涨停与连板情绪"}),
    "market_limit_state": ("limit_state", "fetch_limit_state", {"layer": 3, "desc": "涨停/炸板/跌停/昨涨停与连板情绪"}),
    "stock_event":   ("stock_events", "fetch_stock_event", {"layer": 4, "desc": "解禁/两融/大宗/股东户数/分红低频事实"}),
    "iwencai_content": ("iwencai_content", "search_content", {"layer": 4, "desc": "问财研报/公告/新闻自然语言内容搜索"}),
    "industry_research": ("research", "fetch_industry_reports", {"layer": 4, "desc": "行业名/股票代码行业研报(东财qType=1)"}),
}


_SOURCE_CACHE = {}


def _load_source(name: str):
    """惰性加载数据源模块"""
    if name not in _SOURCE_CACHE:
        _SOURCE_CACHE[name] = import_module(f"ym_stock_data.sources.{name}")
    return _SOURCE_CACHE[name]


def fetch(data_type: str, **kwargs) -> dict:
    """统一数据获取入口

    Args:
        data_type: 数据类型（见 _ROUTES 表）
        **kwargs: 传给具体数据源的参数

    Returns:
        dict: 数据结果，始终包含 _meta 字段

    Raises:
        ValueError: 不支持的数据类型
    """
    if data_type not in _ROUTES:
        supported = list(_ROUTES.keys())
        raise ValueError(
            f"不支持的数据类型: '{data_type}'. "
            f"支持: {supported}"
        )

    module_name, func_name, meta = _ROUTES[data_type]

    try:
        source = _load_source(module_name)
        func = getattr(source, func_name)
        result = func(**kwargs)

        if not isinstance(result, dict):
            result = {"data": result}

        result["_meta"] = {
            "data_type": data_type,
            "source": module_name,
            "layer": meta["layer"],
            "fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        }
        return result

    except Exception as e:
        return {
            "error": str(e),
            "error_type": type(e).__name__,
            "_meta": {
                "data_type": data_type,
                "source": module_name,
                "layer": meta["layer"],
                "fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "error": True,
            },
        }


def list_supported() -> dict:
    """列出所有支持的数据类型"""
    return {k: v[2]["desc"] for k, v in _ROUTES.items()}
