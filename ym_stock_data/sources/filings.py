"""公告 — 巨潮资讯 cninfo.com.cn (A股信息披露权威源)

数据源: cninfo.com.cn (需 Referer)
鉴权: 无 (HTTP POST)
字段: 标题/日期/类型/PDF下载链接

用法:
    from ym_stock_data.sources import filings
    r = filings.fetch_filings("600519", days=90)
"""

from datetime import date, timedelta, datetime
import threading
import time

import requests

_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
_SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch",
}
_ORG_ID_CACHE_TTL = 300.0
_ORG_ID_CACHE: dict[str, tuple[float, str]] = {}
_ORG_ID_CACHE_LOCK = threading.Lock()


def _resolve_org_id(code: str) -> str:
    now = time.monotonic()
    with _ORG_ID_CACHE_LOCK:
        cached = _ORG_ID_CACHE.get(code)
        if cached and now < cached[0]:
            return cached[1]

    response = requests.post(
        _SEARCH_URL,
        data={"keyWord": code, "maxSecNum": 10},
        headers={
            "User-Agent": _HEADERS["User-Agent"],
            "Referer": _HEADERS["Referer"],
        },
        timeout=10,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    payload = response.json()
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("result") or []
    else:
        rows = payload
    for row in rows if isinstance(rows, list) else []:
        if str(row.get("code") or "") != code:
            continue
        org_id = str(row.get("orgId") or "")
        if not org_id:
            continue
        with _ORG_ID_CACHE_LOCK:
            _ORG_ID_CACHE[code] = (now + _ORG_ID_CACHE_TTL, org_id)
        return org_id
    raise LookupError(f"未找到股票 {code} 的 cninfo orgId")


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
    try:
        org_id = _resolve_org_id(code)
    except LookupError as exc:
        return {
            "total": 0,
            "filings": [],
            "error": str(exc),
            "error_type": "orgid_unresolved",
            "source": "cninfo",
        }
    except Exception as exc:
        return {
            "total": 0,
            "filings": [],
            "error": str(exc),
            "error_type": type(exc).__name__,
            "source": "cninfo",
        }
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
            "stock": f"{code},{org_id}",
            "searchkey": "",
            "isHLtitle": "false",
        }
        try:
            r = requests.post(_URL, data=data, headers=_HEADERS, timeout=15)
            if r.status_code != 200:
                return {
                    "total": 0,
                    "filings": [],
                    "error": f"HTTP {r.status_code}",
                    "error_type": "http_error",
                    "source": "cninfo",
                }
            d = r.json()
            items = d.get("announcements") or []
            if not items:
                break
            all_filings.extend(items)
            if not d.get("hasMore"):
                break
        except Exception as exc:
            return {
                "total": 0,
                "filings": [],
                "error": str(exc),
                "error_type": type(exc).__name__,
                "source": "cninfo",
            }

    filings = []
    for item in all_filings:
        filings.append({
            "date": _ms_to_date(item.get("announcementTime", 0)),
            "title": item.get("announcementTitle", ""),
            "type_name": item.get("announcementTypeName", ""),
            "type": item.get("announcementType", ""),
            "pdf_url": f"https://static.cninfo.com.cn/{item['adjunctUrl']}"
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
