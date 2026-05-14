"""东方财富直连 API — 全市场龙虎榜

数据源: datacenter-web.eastmoney.com
鉴权: 无 (需 Accept+Referer headers)
实测: 112条, 0.37s
风险: 中 (东财有反爬，建议 3次/分钟 频率限制)
"""

from datetime import datetime
from typing import Optional
import requests

_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json",
}


def fetch_daily_dragon_tiger(
    trade_date: Optional[str] = None,
    min_net_buy_wan: Optional[float] = None,
) -> dict:
    """全市场龙虎榜 — 单日所有上榜股票

    Args:
        trade_date: 'YYYY-MM-DD', None=今天
        min_net_buy_wan: 净买入下限(万元), None 不过滤

    Returns:
        {date, total_records,
         stocks: [{code, name, reason, close, change_pct,
                   net_buy_wan, buy_wan, sell_wan, turnover_pct}],
         top10: [涨幅前10], source: "eastmoney_dragon_tiger"}
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": "ALL",
        "filter": f"(TRADE_DATE>='{trade_date}')(TRADE_DATE<='{trade_date}')",
        "pageNumber": "1",
        "pageSize": "500",
        "sortTypes": "-1",
        "sortColumns": "BILLBOARD_NET_AMT",
        "source": "WEB",
        "client": "WEB",
    }

    resp = requests.get(url, params=params, headers=_EM_HEADERS, timeout=15)
    resp.raise_for_status()
    d = resp.json()

    if not d.get("success") or not d.get("result") or not d["result"].get("data"):
        return {
            "date": trade_date,
            "total_records": 0,
            "stocks": [],
            "top10": [],
            "note": "无数据（非交易日或盘后未更新）",
            "source": "eastmoney_dragon_tiger",
        }

    raw_data = d["result"]["data"]
    actual_date = raw_data[0].get("TRADE_DATE", "")[:10] if raw_data else trade_date

    stocks = []
    for row in raw_data:
        net_buy = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
        if min_net_buy_wan is not None and net_buy < min_net_buy_wan:
            continue

        stocks.append({
            "code": row.get("SECURITY_CODE", ""),
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "reason": row.get("EXPLANATION", ""),
            "close": float(row.get("CLOSE_PRICE") or 0),
            "change_pct": round(float(row.get("CHANGE_RATE") or 0), 2),
            "net_buy_wan": round(net_buy, 1),
            "buy_wan": round((row.get("BILLBOARD_BUY_AMT") or 0) / 10000, 1),
            "sell_wan": round((row.get("BILLBOARD_SELL_AMT") or 0) / 10000, 1),
            "turnover_pct": round(float(row.get("TURNOVERRATE") or 0), 2),
        })

    # 涨幅前10
    by_change = sorted(stocks, key=lambda s: s["change_pct"], reverse=True)[:10]

    return {
        "date": actual_date,
        "total_records": len(stocks),
        "stocks": stocks,
        "top10": by_change,
        "source": "eastmoney_dragon_tiger",
    }
