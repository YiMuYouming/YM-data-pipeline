"""问财 OpenAPI — A 股全字段查询（晋级率/封板率/资金流/板块等）

包装 iwencai_query.py 的现有逻辑为可 import 的 Python 模块。

用法:
    from ym_stock_data.sources import iwencai
    data = iwencai.query("涨停 非st", limit=50)
    stocks = iwencai.query_stocks(["信维通信"])
    inflow = iwencai.query_sector_inflow(top_n=20)
"""

import os, re, json, urllib.request, secrets

IWENCAI_URL = "https://openapi.iwencai.com/v1/query2data"
_DEFAULT_FIELDS = ["涨跌幅", "成交额", "主力净流入", "换手率", "收盘价"]
# 同花顺行业板块列表（~90 个）
_THS_INDUSTRIES = [
    "白酒", "养殖业", "饮料制造", "银行", "燃气", "保险", "机场航运",
    "石油加工", "食品加工", "公路铁路运输", "煤炭", "港口航运", "医药商业",
    "钢铁", "建筑材料", "房地产", "化工", "电力", "半导体", "计算机设备",
    "计算机应用", "通信设备", "传媒", "汽车零部件", "汽车整车", "国防军工",
    "电气设备", "电子制造", "医疗器械", "生物制品", "中药", "化学制药",
    "零售", "纺织", "服装", "食品饮料", "农业服务", "环保工程",
]


def _load_api_key() -> str:
    """读取 IWENCAI_API_KEY"""
    key = os.environ.get("IWENCAI_API_KEY")
    if key:
        return key
    for rc in [os.path.expanduser("~/.zshrc"), os.path.expanduser("~/.bash_profile")]:
        try:
            with open(rc, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r'export\s+IWENCAI_API_KEY=["\'](.+?)["\']', line)
                    if m:
                        return m.group(1)
        except (FileNotFoundError, PermissionError):
            continue
    return None


def query(query: str, limit: int = 50, page: int = 1) -> dict:
    """问财通用查询，返回原始 JSON

    Args:
        query: 自然语言查询，如 "涨停 非st" "板块 涨幅排名"
        limit: 返回条数
        page: 页码
    """
    key = _load_api_key()
    if not key:
        raise ValueError("IWENCAI_API_KEY 未设置")

    req = urllib.request.Request(
        IWENCAI_URL,
        data=json.dumps({
            "query": query,
            "page": str(page),
            "limit": str(limit),
            "is_cache": "1",
            "expand_index": "true",
        }).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "X-Claw-Call-Type": "normal",
            "X-Claw-Skill-Id": "hithink-astock-selector",
            "X-Claw-Skill-Version": "1.0.0",
            "X-Claw-Plugin-Id": "none",
            "X-Claw-Plugin-Version": "none",
            "X-Claw-Trace-Id": secrets.token_hex(32),
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e), "query": query}

    return raw


def extract_data(raw: dict) -> tuple:
    """从问财返回中提取字段名和数据

    Returns:
        (fields: list[str], datas: list[list])
    """
    result = raw.get("result", {})
    if isinstance(result, dict):
        # 新版格式: result.data
        data_block = result.get("data", result)
    else:
        data_block = result

    if isinstance(data_block, dict):
        fields = data_block.get("fields", [])
        datas = data_block.get("datas", [])
    elif isinstance(data_block, list):
        # 尝试从第一项推测
        if data_block:
            first = data_block[0]
            if isinstance(first, dict):
                fields = list(first.keys())
                datas = [[d.get(f, "") for f in fields] for d in data_block]
            else:
                fields = []
                datas = data_block
        else:
            fields, datas = [], []
    else:
        fields, datas = [], []

    return fields, datas


def query_stocks(codes_or_names: list, fields: list = None) -> dict:
    """批量查询多只股票的标准字段

    Args:
        codes_or_names: 股票代码或名称列表
        fields: 查询字段，默认涨跌幅/成交额/主力净流入/换手率/收盘价

    Returns:
        {代码或名称: {field: value}, ...}
    """
    if not codes_or_names:
        return {}

    f = fields or _DEFAULT_FIELDS
    if isinstance(f, list):
        f = " ".join(f)

    query_str = " ".join(codes_or_names) + " " + f
    raw = query(query_str, limit=len(codes_or_names))

    fields_list, datas = extract_data(raw)
    if not fields_list or not datas:
        return {name: {} for name in codes_or_names}

    result = {}
    for row in datas:
        item = dict(zip(fields_list, row))
        # 用代码或名称做 key
        key = item.get("code", "") or str(item.get("股票代码", ""))
        if not key:
            key = str(row[0]) if row else ""
        result[key] = item

    return result


def query_sector_inflow(top_n: int = 20) -> dict:
    """查询同花顺行业板块主力净流入（替代 akshare）

    通过问财查询板块涨跌幅+主力净流入+成交额+领涨股。

    Returns:
        {total: N, top: [{name, change_pct, net_inflow_yi, turnover_yi, leader}, ...],
         bottom: [...], source: "iwencai"}
    """
    raw = query("同花顺行业板块 涨跌幅 主力净流入 成交额 领涨股", limit=90)
    fields_list, datas = extract_data(raw)

    if not fields_list or not datas:
        # pywencai 降级
        return _query_sector_inflow_pywencai(top_n)

    rows = []
    for row in datas:
        item = dict(zip(fields_list, row))
        name = (item.get("板块名称") or item.get("板块") or item.get("name") or "")
        if not name:
            continue
        rows.append({
            "name": name,
            "change_pct": float(item.get("涨跌幅", 0) or 0),
            "net_inflow_yi": float(item.get("主力净流入", 0) or 0),
            "turnover_yi": float(item.get("成交额", 0) or 0) / 1e8 if item.get("成交额") else None,
            "leader": item.get("领涨股", "") or "",
        })

    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    return {
        "total": len(rows),
        "top": rows[:top_n],
        "bottom": rows[-top_n:] if top_n > 0 else [],
        "source": "iwencai",
    }


def _query_sector_inflow_pywencai(top_n: int) -> dict:
    """pywencai 降级查询行业板块"""
    try:
        import pywencai
        df = pywencai.get(query="同花顺行业板块 涨跌幅 主力净流入 成交额 领涨股", perpage=90)
        if df is None or df.empty:
            return {"total": 0, "top": [], "bottom": [], "note": "空数据", "source": "pywencai_fallback"}
    except Exception as e:
        return {"total": 0, "top": [], "bottom": [], "note": str(e), "source": "pywencai_fallback"}

    rows = []
    for _, row in df.iterrows():
        rows.append({
            "name": str(row.get("板块名称", row.get("板块", ""))),
            "change_pct": float(row.get("涨跌幅", 0) or 0),
            "net_inflow_yi": float(row.get("主力净流入", 0) or 0),
            "turnover_yi": float(row.get("成交额", 0) or 0) / 1e8 if row.get("成交额") else None,
            "leader": str(row.get("领涨股", "")),
        })

    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    return {
        "total": len(rows),
        "top": rows[:top_n],
        "bottom": rows[-top_n:] if top_n > 0 else [],
        "source": "pywencai_fallback",
    }
