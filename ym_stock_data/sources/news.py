"""新闻 — 财联社电报 cls.cn

数据源: cls.cn/v1/roll/get_roll_list (财联社快讯)
鉴权: 无 (需 User-Agent + Referer)
实时性: 分钟级更新

用法:
    from ym_stock_data.sources import news
    r = news.fetch_news(limit=20)
"""

import hashlib
from datetime import datetime

import requests

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
    "Referer": "https://www.cls.cn/telegraph",
}


def _signed_roll_url(limit: int) -> str:
    params = {
        "appName": "CailianpressWeb",
        "last_time": "",
        "os": "web",
        "refresh_type": "1",
        "rn": str(limit),
        "sv": "7.7.5",
    }
    query = "&".join(f"{key}={params[key]}" for key in sorted(params))
    sha1 = hashlib.sha1(query.encode("utf-8")).hexdigest()
    sign = hashlib.md5(sha1.encode("utf-8")).hexdigest()
    return f"https://www.cls.cn/v1/roll/get_roll_list?{query}&sign={sign}"


def fetch_news(limit: int = 20) -> dict:
    """获取财联社实时电报

    Args:
        limit: 返回条数上限

    Returns:
        {total: N, items: [{id, title, content, time}, ...]}
    """
    try:
        r = requests.get(_signed_roll_url(limit), headers=_HEADERS, timeout=10)
        if r.status_code != 200:
            return {"total": 0, "items": [], "error": f"HTTP {r.status_code}"}
        d = r.json()
    except Exception as e:
        return {"total": 0, "items": [], "error": str(e)}

    error_code = d.get("errno", d.get("error", 0))
    if error_code != 0:
        return {"total": 0, "items": [], "error": str(error_code)}

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
