"""问财 OpenAPI — A 股全字段查询

包装 iwencai_query.py 为可 import 的 Python 模块。

用法:
    from ym_stock_data.sources import iwencai
    raw = iwencai.query("涨停 非st", limit=50)       # 原始JSON
    stocks = iwencai.query_stocks(["信维通信"])        # 批量个股
    rank = iwencai.query_rank("信维通信")              # 热度排名

问财返回格式: datas = [{列名: 值}, ...], columns = [{index_name, key, type}, ...]
"""

import os, re, json, urllib.request, secrets

IWENCAI_BASE = "https://openapi.iwencai.com"
_DEFAULT_FIELDS = ["涨跌幅", "成交额", "主力净流入", "换手率", "收盘价"]

# 加载一次，后续复用
_API_KEY = None


def _load_api_key() -> str:
    """读取 IWENCAI_API_KEY: 环境变量 → .zshrc → .bash_profile → .bashrc"""
    global _API_KEY
    if _API_KEY:
        return _API_KEY
    key = os.environ.get("IWENCAI_API_KEY")
    if key:
        _API_KEY = key
        return key
    for rc in [os.path.expanduser(p) for p in ["~/.zshrc", "~/.bash_profile", "~/.bashrc"]]:
        try:
            with open(rc, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r'export\s+IWENCAI_API_KEY=["\']?(.+?)["\']?\s*$', line)
                    if m:
                        _API_KEY = m.group(1)
                        return _API_KEY
        except (FileNotFoundError, PermissionError):
            continue
    return None


# === 共享 headers ===

def _iwencai_headers() -> dict:
    """生成 X-Claw 请求头 (SkillHub 2.0 规范)"""
    return {
        "Authorization": f"Bearer {_load_api_key()}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "X-Claw-Call-Type": "normal",
        "X-Claw-Skill-Id": "hithink-astock-selector",
        "X-Claw-Skill-Version": "1.0.0",
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": secrets.token_hex(32),
    }


def query(query: str, limit: int = 50, page: int = 1) -> dict:
    """问财结构化数据查询 → /v1/query2data

    返回 columns + datas 格式，适合数值分析和量化回测。
    同行见: iwencai_query.py

    Args:
        query: 自然语言查询，如 "涨停 非st"
        limit: 返回条数上限
        page: 页码

    Returns:
        {columns: [...], datas: [{列名:值}, ...], row_count: N}
    """
    key = _load_api_key()
    if not key:
        raise ValueError("IWENCAI_API_KEY 未设置")

    req = urllib.request.Request(
        f"{IWENCAI_BASE}/v1/query2data",
        data=json.dumps({
            "query": query,
            "page": str(page),
            "limit": str(limit),
            "is_cache": "1",
            "expand_index": "true",
        }).encode("utf-8"),
        headers=_iwencai_headers(),
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.request.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}", "query": query}
    except Exception as e:
        return {"error": str(e), "query": query}


def search(query: str, channel: str = "report", size: int = 30) -> dict:
    """问财综合搜索 → /v1/comprehensive/search

    搜索研报、公告、新闻等非结构化文本。
    参照: simonlin1212/a-stock-data Layer 2/3

    已知: 当前 API key 可能无此端点权限 (返回 500)。
    权限升级后可正常使用。降级方案见 L4 研报/公告/新闻独立模块。

    Args:
        query: 自然语言检索词, 如 "人形机器人 行星滚柱丝杠"
        channel: 搜索渠道 — "report"(研报) / "announcement"(公告) / "news"(新闻)
        size: 返回条数上限
    """
    key = _load_api_key()
    if not key:
        raise ValueError("IWENCAI_API_KEY 未设置")

    req = urllib.request.Request(
        f"{IWENCAI_BASE}/v1/comprehensive/search",
        data=json.dumps({
            "channels": channel,
            "app_id": "AIME_SKILL",
            "query": query,
            "size": size,
        }).encode("utf-8"),
        headers=_iwencai_headers(),
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.request.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}", "query": query}
    except Exception as e:
        return {"error": str(e), "query": query}

    if raw.get("status_code") != 0:
        return {"error": f"status_code={raw.get('status_code')}", "query": query}

    articles = raw.get("articles", raw.get("data", []))
    return {
        "articles": articles,
        "total": len(articles),
        "channel": channel,
        "query": query,
    }


def query_stocks(codes_or_names: list, fields: list = None) -> dict:
    """批量查询多只股票的标准字段

    Args:
        codes_or_names: 股票代码或名称列表, 如 ["信维通信", "688017"]
        fields: 查询字段, 默认涨跌幅/成交额/主力净流入/换手率/收盘价

    Returns:
        {代码或名称: {field: value}, ...}  — 数据不存在的股票返回 {}
    """
    if not codes_or_names:
        return {}

    f = fields or _DEFAULT_FIELDS
    f_str = " ".join(f) if isinstance(f, list) else f
    query_str = " ".join(codes_or_names) + " " + f_str

    raw = query(query_str, limit=len(codes_or_names))
    if "error" in raw:
        return {name: {} for name in codes_or_names}

    datas = raw.get("datas", [])
    result = {}
    for item in datas:
        if not isinstance(item, dict):
            continue
        key = item.get("股票代码", "") or item.get("code", "")
        if not key:
            continue
        result[key] = item

    return result


def query_rank(code_or_name: str) -> dict:
    """个股全市场热度排名

    Args:
        code_or_name: 股票代码或名称

    Returns:
        {股票代码, 股票简称, 今日热股人气排名, 热度值, ...} 或 {error: msg}
    """
    raw = query(f"{code_or_name} 今日热股人气排名 热度值", limit=1)
    if "error" in raw:
        return raw
    datas = raw.get("datas", [])
    if datas:
        return datas[0]
    return {"error": "无数据"}
