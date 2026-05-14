"""行业板块 — 同花顺行业板块涨跌幅+主力净流入（零鉴权 HTTP）

数据源: q.10jqka.com.cn (同花顺行情中心)
底层路径: 与 akshare stock_board_industry_summary_ths() 同源，但直接 HTTP 调用
鉴权: 无 (仅 User-Agent)
稳定性: 高 (同花顺公共页面)
"""

import re
import requests

_URL = "http://q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/1/"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
}


def fetch_industry_summary(top_n: int = 20) -> dict:
    """全行业涨跌幅排名（~70 个同花顺行业）

    Args:
        top_n: 返回前 N 名和后 N 名

    Returns:
        {total, top: [{name, change_pct, turnover_yi, net_inflow_yi,
                       up_count, down_count, leader, leader_change_pct}, ...],
         bottom: [...], source: "ths_industry"}
    """
    try:
        resp = requests.get(_URL, headers=_HEADERS, timeout=10)
        resp.encoding = "gbk"
        html = resp.text
    except Exception as e:
        return {"total": 0, "top": [], "bottom": [], "note": str(e), "source": "ths_industry"}

    # 解析表格行: <tr><td>排序</td><td><a>板块名</a></td><td>涨跌幅</td>...
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)
        if len(tds) < 8:
            continue

        # 提取板块名
        name_match = re.search(r'<a[^>]*>(.*?)</a>', tds[1])
        if not name_match:
            continue
        name = name_match.group(1).strip()

        # 提取涨跌幅
        try:
            change_pct = float(re.sub(r'<[^>]+>', '', tds[2]).strip())
        except ValueError:
            continue

        # 提取成交额（亿元）
        turnover = _parse_float(tds[4])

        # 提取主力净流入（亿元）
        net_inflow = _parse_float(tds[5])

        # 上涨/下跌家数
        up_count = _parse_int(tds[6])
        down_count = _parse_int(tds[7])

        # 领涨股
        leader = ""
        leader_chg = 0
        if len(tds) >= 10:
            leader_match = re.search(r'<a[^>]*>(.*?)</a>', tds[9])
            if leader_match:
                leader = leader_match.group(1).strip()
            if len(tds) >= 12:
                leader_chg = _parse_float(tds[11])

        rows.append({
            "name": name,
            "change_pct": change_pct,
            "turnover_yi": turnover,
            "net_inflow_yi": net_inflow if net_inflow is not None else 0,
            "up_count": up_count,
            "down_count": down_count,
            "leader": leader,
            "leader_change_pct": leader_chg,
        })

    # 按涨跌幅排序
    rows.sort(key=lambda r: r["change_pct"], reverse=True)

    return {
        "total": len(rows),
        "top": rows[:top_n],
        "bottom": rows[-top_n:] if top_n > 0 and rows else [],
        "source": "ths_industry",
    }


def _parse_float(td_html: str) -> float:
    val = re.sub(r'<[^>]+>', '', td_html).strip()
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _parse_int(td_html: str) -> int:
    val = re.sub(r'<[^>]+>', '', td_html).strip()
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
