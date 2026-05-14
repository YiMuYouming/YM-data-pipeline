"""新闻 — 财联社电报 cls.cn

数据源: cls.cn/nodeapi (财联社快讯)
鉴权: 无 (需 User-Agent + Referer)
实时性: 分钟级更新

用法:
    from ym_stock_data.sources import news
    r = news.fetch_news(limit=20)
"""

import requests
from datetime import datetime

_URL = "https://www.cls.cn/nodeapi/telegraphList"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
    "Referer": "https://www.cls.cn/telegraph",
}


def fetch_news(limit: int = 20) -> dict:
    """获取财联社实时电报

    Args:
        limit: 返回条数上限

    Returns:
        {total: N, items: [{id, title, content, time}, ...]}
    """
    try:
        r = requests.get(_URL, params={"category": "all", "rn": str(limit)}, headers=_HEADERS, timeout=10)
        if r.status_code != 200:
            return {"total": 0, "items": [], "error": f"HTTP {r.status_code}"}
        d = r.json()
    except Exception as e:
        return {"total": 0, "items": [], "error": str(e)}

    if d.get("error") != 0:
        return {"total": 0, "items": [], "error": str(d.get("error", ""))}

    raw = d.get("data", [])
    if isinstance(raw, dict):
        raw = raw.get("roll_data", raw.get("data", []))

    items = []
    for item in (raw if isinstance(raw, list) else []):
        ts = item.get("ctime", 0)
        try:
            t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except (OSError, ValueError):
            t = str(ts)

        items.append({
            "id": item.get("id", ""),
            "title": item.get("title", item.get("brief", "")),
            "content": item.get("content", item.get("title", "")),
            "time": t,
        })

    return {
        "total": len(items),
        "items": items,
        "source": "cls_telegraph",
    }
