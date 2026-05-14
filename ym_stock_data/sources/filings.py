"""公告 — 巨潮资讯 cninfo.com.cn (A股信息披露权威源)

数据源: cninfo.com.cn (需 Referer)
鉴权: 无 (HTTP POST)
字段: 标题/日期/类型/PDF下载链接

用法:
    from ym_stock_data.sources import filings
    r = filings.fetch_filings("600519", days=90)
"""

import requests
from datetime import date, timedelta, datetime

_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch",
}


def _resolve_market(code: str) -> tuple:
    """6位代码 → (column, plate)"""
    if code.startswith(("6", "9")):
        return ("sse", "sh")
    elif code.startswith("8"):
        return ("bjse", "bj")
    else:
        return ("szse", "sz")


def _ms_to_date(ms: int) -> str:
    """毫秒时间戳 → 日期字符串"""
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    except (OSError, ValueError, TypeError):
        return ""


def fetch_filings(code: str, days: int = 90, max_pages: int = 3) -> dict:
    """获取个股公告列表

    通过 searchkey (股票名称) 搜索，巨潮stock直接参数不可靠。

    Args:
        code: 6位股票代码, 如 "600519"
        days: 回溯天数
        max_pages: 最大翻页数 (每页最多30条)

    Returns:
        {total: N, filings: [{date, title, type, pdf_url, sec_code, sec_name}, ...]}
    """
    column, plate = _resolve_market(code)
    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    all_filings = []
    for page in range(1, max_pages + 1):
        data = {
            "pageNum": page,
            "pageSize": 30,
            "tabName": "fulltext",
            "column": column,
            "plate": plate,
            "seDate": f"{start}~{end}",
            "searchkey": code,  # 用代码搜索
            "isHLtitle": "false",
        }
        try:
            r = requests.post(_URL, data=data, headers=_HEADERS, timeout=15)
            if r.status_code != 200:
                break
            d = r.json()
            items = d.get("announcements") or []
            if not items:
                break
            all_filings.extend(items)
            if not d.get("hasMore"):
                break
        except Exception:
            break

    filings = []
    for item in all_filings:
        filings.append({
            "date": _ms_to_date(item.get("announcementTime", 0)),
            "title": item.get("announcementTitle", ""),
            "type_name": item.get("announcementTypeName", ""),
            "type": item.get("announcementType", ""),
            "pdf_url": f"http://static.cninfo.com.cn/{item['adjunctUrl']}"
            if item.get("adjunctUrl") else "",
            "sec_code": item.get("secCode", ""),
            "sec_name": item.get("secName", ""),
        })

    # 按日期降序
    filings.sort(key=lambda f: f["date"], reverse=True)

    return {
        "total": len(filings),
        "filings": filings,
        "source": "cninfo",
    }
