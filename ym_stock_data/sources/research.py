"""研报 — 东方财富 reportapi.eastmoney.com

数据源: reportapi.eastmoney.com (需 Referer)
鉴权: 无
字段: 含评级/目标价/未来3年EPS预测

用法:
    from ym_stock_data.sources import research
    reports = research.fetch_reports("600519")
"""

import requests
from datetime import date, timedelta

_URL = "https://reportapi.eastmoney.com/report/list"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
    "Referer": "https://data.eastmoney.com/report/",
}


def fetch_reports(code: str, days: int = 90, max_pages: int = 15) -> dict:
    """获取个股研报列表

    API 不支持服务端 stockCode 过滤，采用拉取+客户端过滤策略。
    大盘股研报可能在后几页，max_pages 默认 15。

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
        }
        try:
            r = requests.get(_URL, params=params, headers=_HEADERS, timeout=10)
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get("data", [])
            if not items:
                break
            all_reports.extend(items)
        except Exception:
            break

    # 客户端过滤指定股票
    matched = [r for r in all_reports if r.get("stockCode", "") == code]

    reports = []
    for item in matched:
        reports.append({
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
        })

    # 按日期降序
    reports.sort(key=lambda r: r["publish_date"], reverse=True)

    return {
        "total": len(reports),
        "reports": reports,
        "source": "eastmoney_reportapi",
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
