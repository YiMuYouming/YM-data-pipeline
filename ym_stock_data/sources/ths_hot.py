"""同花顺热点 — 当日强势股 + 题材归因 reason tags

数据源: zx.10jqka.com.cn (同花顺)
鉴权: 无 (仅 User-Agent)
实测: 146只, 1.5s, 100% reason tags
风险: 极低 (零鉴权)
"""

from datetime import date as _date, datetime
from typing import Optional
import requests

_THS_HOT_URL = (
    "http://zx.10jqka.com.cn/event/api/getharden/"
    "date/{date}/orderby/date/orderway/desc/charset/GBK/"
)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"


def fetch_hot_stocks(date_str: Optional[str] = None) -> dict:
    """获取同花顺当日强势股 + 题材归因

    Args:
        date_str: 'YYYY-MM-DD' 格式, None=今天

    Returns:
        dict with keys:
          - date: 查询日期
          - total: 强势股总数
          - stocks: [{code, name, zhangfu, reason, huanshou, chengjiaoe, ddejingliang, market}, ...]
          - reason_stats: {题材tag: 出现次数} 按次数降序
          - source: "ths_hot"
    """
    if date_str is None:
        date_str = _date.today().strftime("%Y-%m-%d")

    url = _THS_HOT_URL.format(date=date_str)
    headers = {"User-Agent": _UA}

    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    err = data.get("errocode", 0)
    if err != 0:
        return {
            "date": date_str,
            "total": 0,
            "stocks": [],
            "reason_stats": {},
            "error": data.get("errormsg", f"errocode={err}"),
            "source": "ths_hot",
        }

    rows = data.get("data") or []
    stocks = []
    from collections import Counter

    reason_counter = Counter()

    for row in rows:
        code = row.get("code", "")
        name = row.get("name", "")
        zhangfu = float(row["zhangfu"]) if row.get("zhangfu") else 0.0
        reason = (row.get("reason") or "").strip()

        if reason:
            tags = [t.strip() for t in reason.split("+") if t.strip()]
            reason_counter.update(tags)

        stocks.append({
            "code": code,
            "name": name,
            "zhangfu": zhangfu,
            "reason": reason,
            "huanshou": float(row["huanshou"]) if row.get("huanshou") else None,
            "chengjiaoe": float(row["chengjiaoe"]) if row.get("chengjiaoe") else None,
            "ddejingliang": float(row.get("ddejingliang", 0)) if row.get("ddejingliang") else None,
            "market": row.get("market", ""),
        })

    # 按涨幅降序
    stocks.sort(key=lambda s: s["zhangfu"], reverse=True)

    return {
        "date": date_str,
        "total": len(stocks),
        "stocks": stocks,
        "reason_stats": dict(reason_counter.most_common()),
        "source": "ths_hot",
    }


def fetch_hot_with_zt_count(date_str: Optional[str] = None) -> dict:
    """获取同花顺热点 + 涨停统计

    返回包含涨停家数、涨停股票列表、题材词频统计等增强数据。
    """
    result = fetch_hot_stocks(date_str)

    # 涨停判定: 主板>=9.9%, 科创/创业板>=19.9%
    zt_stocks = []
    for s in result["stocks"]:
        code = s["code"]
        zhangfu = s["zhangfu"]
        if code.startswith(("688", "300")):
            is_zt = zhangfu >= 19.5
        else:
            is_zt = zhangfu >= 9.8
        if is_zt:
            zt_stocks.append(s)

    result["zt_count"] = len(zt_stocks)
    result["zt_stocks"] = zt_stocks

    return result
