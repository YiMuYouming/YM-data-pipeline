"""行业板块 — 同花顺行业板块涨跌幅+主力净流入（零鉴权 HTTP）

数据源: q.10jqka.com.cn (同花顺行情中心)
底层路径: 与 akshare stock_board_industry_summary_ths() 同源，但直接 HTTP 调用
鉴权: 无 (仅 User-Agent)
稳定性: 高 (同花顺公共页面)
"""

import re
import requests

_URL_TEMPLATE = "http://q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/{page}/"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
}
_THS_INDUSTRY_ALIASES = {
    "消费电子": "881124",
    "通信设备": "881129",
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
    rows = _fetch_all_industry_rows()
    if isinstance(rows, dict) and rows.get("error"):
        return {"total": 0, "top": [], "bottom": [], "note": rows["error"],
                "error": rows["error"], "error_type": rows.get("error_type", "PROVIDER_ERROR"),
                "source": "ths_industry"}

    rows.sort(key=lambda r: r["change_pct"], reverse=True)

    return {
        "total": len(rows),
        "top": rows[:top_n],
        "bottom": rows[-top_n:] if top_n > 0 and rows else [],
        "source": "ths_industry",
    }


def fetch_sector_index(codes: list[str] | None = None, names: list[str] | None = None) -> dict:
    """同花顺 881xxx 行业板块涨跌幅和主力净流入。

    Args:
        codes: 同花顺行业代码列表，如 ["881124"]
        names: 同花顺行业名称列表，如 ["消费电子", "通信设备"]

    Returns:
        {items, by_code, by_name, missing, source}
    """
    rows = _fetch_all_industry_rows()
    if isinstance(rows, dict) and rows.get("error"):
        return {
            "items": [],
            "by_code": {},
            "by_name": {},
            "missing": list(codes or []) + list(names or []),
            "error": rows["error"],
            "error_type": rows.get("error_type", "PROVIDER_ERROR"),
            "source": "ths_industry",
        }

    result = _select_industries(rows, codes, names)
    # 2026-10-26（批次4 / B2）：摘要分页后页偶发空页时上游会静默截断
    # （实测 page1=50 / page2=40，"电机"在 page2；09:12 生产出现电机 empty、
    # 同日稍后同参数成功）。显式请求缺失时整表重读一次再判定 missing，
    # 避免把瞬时截断报成"上游没有该板块"；重读仍缺才是合法 empty。
    if (codes or names) and result["missing"]:
        retried_rows = _fetch_all_industry_rows()
        if not (isinstance(retried_rows, dict) and retried_rows.get("error")):
            retried = _select_industries(retried_rows, codes, names)
            result["retry_attempts"] = 2
            result["recovered_missing"] = sorted(set(result["missing"]) - set(retried["missing"]))
            result = {**retried, "retry_attempts": 2,
                      "recovered_missing": result["recovered_missing"]}
    return result


def _select_industries(rows: list[dict], codes, names) -> dict:
    by_code = {row["code"]: row for row in rows}
    by_name = {row["name"]: row for row in rows}
    selected = []
    missing = []

    for code in codes or []:
        normalized = str(code).strip().removesuffix(".TI")
        row = by_code.get(normalized)
        if row:
            selected.append(row)
        else:
            missing.append(normalized)

    for name in names or []:
        normalized = str(name).strip()
        alias_code = _THS_INDUSTRY_ALIASES.get(normalized)
        row = by_code.get(alias_code) if alias_code else by_name.get(normalized)
        if row:
            selected.append(row)
        else:
            missing.append(normalized)

    if not codes and not names:
        selected = rows

    deduped = []
    seen = set()
    for row in selected:
        if row["code"] in seen:
            continue
        deduped.append(row)
        seen.add(row["code"])

    return {
        "items": deduped,
        "by_code": {row["code"]: row for row in deduped},
        "by_name": {row["name"]: row for row in deduped},
        "missing": missing,
        "source": "ths_industry",
    }


def _fetch_industry_page(page: int) -> tuple[list[dict], str | None]:
    resp = requests.get(_URL_TEMPLATE.format(page=page), headers=_HEADERS, timeout=10)
    if resp.status_code != 200:
        return [], f"HTTP_{resp.status_code}"
    resp.encoding = "gbk"
    return _parse_industry_rows(resp.text), None


def _fetch_all_industry_rows(max_pages: int = 5) -> list[dict] | dict:
    rows: list[dict] = []
    first_page_size: int | None = None
    last_page_size: int | None = None
    try:
        for page in range(1, max_pages + 1):
            page_rows, error = _fetch_industry_page(page)
            if error:
                return {"error": "industry HTTP failure", "error_type": error}
            # 2026-10-26（批次4 / B2）：上一页是满页时，空页是偶发截断而不是
            # 分页结束，重试一次；上一页本来就不满时，空页才是正常终止。
            if not page_rows and page > 1 and last_page_size == first_page_size:
                page_rows, error = _fetch_industry_page(page)
                if error:
                    return {"error": "industry HTTP failure", "error_type": error}
            if not page_rows:
                if not rows:
                    return {"error": "industry table not parsed", "error_type": "PARSE_FAILED"}
                break
            rows.extend(page_rows)
            if first_page_size is None:
                first_page_size = len(page_rows)
            last_page_size = len(page_rows)
    except Exception as e:
        return {"error": "industry request failed", "error_type": "TIMEOUT" if isinstance(e, requests.Timeout) else "NETWORK_ERROR"}
    return rows


def _parse_industry_rows(html: str) -> list[dict]:
    rows = []
    # 解析表格行: <tr><td>排序</td><td><a href=".../code/881124/">板块名</a></td><td>涨跌幅</td>...
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)
        if len(tds) < 8:
            continue

        code_match = re.search(r"/code/(\d{6})/", tds[1])
        if not code_match:
            continue
        code = code_match.group(1)
        if not code.startswith("881"):
            continue

        name_match = re.search(r'<a[^>]*>(.*?)</a>', tds[1])
        if not name_match:
            continue
        name = name_match.group(1).strip()

        try:
            change_pct = float(re.sub(r'<[^>]+>', '', tds[2]).strip())
        except ValueError:
            continue

        leader = ""
        leader_price = 0.0
        leader_chg = 0.0
        if len(tds) >= 10:
            leader_match = re.search(r'<a[^>]*>(.*?)</a>', tds[9])
            if leader_match:
                leader = leader_match.group(1).strip()
        if len(tds) >= 11:
            leader_price = _parse_float(tds[10])
        if len(tds) >= 12:
            leader_chg = _parse_float(tds[11])

        net_inflow = _parse_float(tds[5])
        rows.append({
            "code": code,
            "name": name,
            "change_pct": change_pct,
            # This summary page has volume here, not index points. Preserve
            # the old key as unavailable rather than relabeling volume as price.
            "latest": None,
            "volume_wan_lots": _parse_float(tds[3]),
            "turnover_yi": _parse_float(tds[4]),
            "net_inflow_yi": net_inflow,
            "main_net_inflow_yi": net_inflow,
            "up_count": _parse_int(tds[6]),
            "down_count": _parse_int(tds[7]),
            "flat_count": None,
            "avg_price": _parse_float(tds[8]) if len(tds) >= 9 else None,
            "leader": leader,
            "leader_price": leader_price,
            "leader_change_pct": leader_chg,
        })

    return rows


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
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return 0
