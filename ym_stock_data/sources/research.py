"""研报 — 东方财富 reportapi.eastmoney.com

数据源: reportapi.eastmoney.com (需 Referer)
鉴权: 无
字段: 含评级/目标价/未来3年EPS预测

用法:
    from ym_stock_data.sources import research
    reports = research.fetch_reports("600519")
"""

from datetime import date, timedelta
from functools import lru_cache
from html import unescape
import re

import requests

from .eastmoney_http import CLIENT

_URL = "https://reportapi.eastmoney.com/report/list"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
    "Referer": "https://data.eastmoney.com/report/",
}
_INDUSTRY_PAGE_URL = "https://data.eastmoney.com/report/industry.jshtml"


def _normalize_report(item: dict) -> dict:
    return {
        "title": item.get("title", ""),
        "publish_date": str(item.get("publishDate", ""))[:10],
        "org": item.get("orgSName", ""),
        "rating": item.get("emRatingName", ""),
        "rating_change": item.get("ratingChange", ""),
        "target_price": float(item.get("indvAimPriceT") or 0),
        "eps_cur": float(item.get("predictThisYearEps") or 0),
        "eps_next": float(item.get("predictNextYearEps") or 0),
        "eps_next2": float(item.get("predictNextTwoYearEps") or 0),
        "info_code": item.get("infoCode", ""),
        "author": item.get("author", ""),
        "industry": item.get("industryName", ""),
    }


@lru_cache(maxsize=128)
def _resolve_industry_code(industry: str) -> str:
    response = CLIENT.get(
        _INDUSTRY_PAGE_URL,
        headers=_HEADERS,
        timeout=10,
    )
    if getattr(response, "skipped_by_breaker", False) is True:
        raise RuntimeError(response.reason)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    rows = re.findall(
        r'data-bkval="([^"*]+)"[^>]*>([^<]+)</span>',
        response.text,
    )
    normalized = industry.strip()
    exact = [code for code, name in rows if unescape(name).strip() == normalized]
    if exact:
        return exact[0]
    partial = [
        code
        for code, name in rows
        if normalized in unescape(name).strip()
        or unescape(name).strip() in normalized
    ]
    return partial[0] if len(partial) == 1 else ""


def _resolve_stock_industry_code(code: str, start: str, end: str) -> str:
    response = CLIENT.get(
        _URL,
        params={
            "pageSize": 1,
            "pageNo": 1,
            "qType": 0,
            "beginTime": start,
            "endTime": end,
            "industryCode": "*",
            "industry": "*",
            "rating": "*",
            "ratingChange": "*",
            "orgCode": "",
            "code": code,
            "rcode": "",
        },
        headers=_HEADERS,
        timeout=10,
    )
    if getattr(response, "skipped_by_breaker", False) is True:
        raise RuntimeError(response.reason)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    items = response.json().get("data") or []
    if not items:
        return ""
    first = items[0]
    return str(first.get("indvInduCode") or first.get("industryCode") or "")


def fetch_reports(code: str, days: int = 90, max_pages: int = 15) -> dict:
    """获取个股研报列表

    服务端按股票代码筛选，并保留客户端防御性代码校验。

    Args:
        code: 6位股票代码, 如 "600519"
        days: 回溯天数
        max_pages: 最大翻页数 (每页约50条)

    Returns:
        {total: N, reports: [{title, publish_date, org, rating, target_price,
          eps_cur, eps_next, eps_next2, info_code, author}, ...], source}
    """
    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    all_reports = []
    for page in range(1, max_pages + 1):
        params = {
            "pageSize": 50,
            "pageNo": page,
            "qType": 0,
            "beginTime": start,
            "endTime": end,
            "industryCode": "*",
            "industry": "*",
            "rating": "*",
            "ratingChange": "*",
            "orgCode": "",
            "code": code,
            "rcode": "",
        }
        try:
            r = CLIENT.get(_URL, params=params, headers=_HEADERS, timeout=10)
            if getattr(r, "skipped_by_breaker", False) is True:
                return {
                    "error": r.reason,
                    "error_type": "breaker_open",
                    "_source": "none",
                }
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get("data") or []
            if not items:
                break
            all_reports.extend(item for item in items if item.get("stockCode") == code)
            if page >= int(data.get("TotalPage") or 1):
                break
        except Exception:
            break

    reports = [_normalize_report(item) for item in all_reports]

    # 按日期降序
    reports.sort(key=lambda r: r["publish_date"], reverse=True)

    return {
        "total": len(reports),
        "reports": reports,
        "source": "eastmoney_reportapi",
    }


def fetch_industry_reports(
    industry: str | None = None,
    code: str | None = None,
    days: int = 90,
    max_pages: int = 5,
) -> dict:
    """按行业名或股票代码查询行业研报（东财 qType=1）。"""
    if not industry and not code:
        raise ValueError("industry_research 需要 industry 或 code")

    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        industry_code = (
            _resolve_industry_code(industry)
            if industry
            else _resolve_stock_industry_code(code or "", start, end)
        )
    except Exception as exc:
        return {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "reports": [],
            "source": "eastmoney_industry_reportapi",
        }
    if not industry_code:
        return {
            "error": "无法解析行业代码",
            "error_type": "industry_unresolved",
            "reports": [],
            "source": "eastmoney_industry_reportapi",
        }
    all_reports = []
    for page in range(1, max_pages + 1):
        params = {
            "pageSize": 50,
            "pageNo": page,
            "qType": 1,
            "beginTime": start,
            "endTime": end,
            "industryCode": industry_code,
            "industry": "*",
            "rating": "*",
            "ratingChange": "*",
            "orgCode": "",
            "code": "",
            "rcode": "",
        }
        try:
            response = CLIENT.get(
                _URL,
                params=params,
                headers=_HEADERS,
                timeout=10,
            )
            if getattr(response, "skipped_by_breaker", False) is True:
                raise RuntimeError(response.reason)
            if response.status_code != 200:
                return {
                    "error": f"HTTP {response.status_code}",
                    "error_type": "http_error",
                    "reports": [],
                    "source": "eastmoney_industry_reportapi",
                }
            payload = response.json()
        except Exception as exc:
            return {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "reports": [],
                "source": "eastmoney_industry_reportapi",
            }
        items = payload.get("data") or []
        if not items:
            break
        all_reports.extend(items)
        if page >= int(payload.get("TotalPage") or 1):
            break

    reports = [_normalize_report(item) for item in all_reports]
    reports.sort(key=lambda report: report["publish_date"], reverse=True)
    query_type = (
        "industry_and_code"
        if industry and code
        else "industry_name" if industry else "stock_code"
    )
    return {
        "query_type": query_type,
        "industry": industry or "",
        "code": code or "",
        "total": len(reports),
        "reports": reports,
        "source": "eastmoney_industry_reportapi",
    }


def download_pdf(info_code: str, target_dir: str = "./reports") -> str:
    """下载研报 PDF

    Args:
        info_code: 研报 infoCode (从 fetch_reports 获取)
        target_dir: 目标目录

    Returns:
        pdf 文件路径，失败返回空串
    """
    from pathlib import Path
    import re

    url = f"https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
    headers = {
        "User-Agent": _HEADERS["User-Agent"],
        "Referer": "https://data.eastmoney.com/",
    }

    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200 or len(r.content) < 1024:
            return ""
        dest = Path(target_dir)
        dest.mkdir(parents=True, exist_ok=True)
        fname = f"{info_code}.pdf"
        path = dest / fname
        with open(path, "wb") as f:
            f.write(r.content)
        return str(path)
    except Exception:
        return ""
