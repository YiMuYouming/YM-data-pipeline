"""行业板块 — 同花顺行业板块涨跌幅+主力净流入（零鉴权 HTTP）

数据源: q.10jqka.com.cn (同花顺行情中心)
底层路径: 与 akshare stock_board_industry_summary_ths() 同源，但直接 HTTP 调用
鉴权: 无 (仅 User-Agent)
稳定性: 高 (同花顺公共页面)
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

_URL_TEMPLATE = "http://q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/{page}/"
# 2026-11-02 R09：调用方 8 秒预算内必须返回。requests 的 timeout=(connect, read)
# 只是单次 socket 等待，滴流响应仍可越过预算；因此每页读取放在有界 future 里，
# 到整轮 deadline 即返回，晚到结果丢弃（本模块无缓存可回写）。
SECTOR_WALL_BUDGET_SEC = 7.5
_monotonic = time.monotonic
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
    _server.value = None
    deadline = _monotonic() + SECTOR_WALL_BUDGET_SEC
    server_holder: dict = {}
    rows = _fetch_all_industry_rows(deadline=deadline, server_holder=server_holder)
    server_time = server_holder.get("value")
    if isinstance(rows, dict) and rows.get("error"):
        # 已取到的页不因后页失败而丢弃；部分返回必须显式标 partial。
        # 但一个请求对象都没取到仍是失败，不能伪装成合法空或部分成功。
        if rows.get("rows"):
            result = _select_industries(rows["rows"], codes, names)
            if result["items"]:
                result["partial"] = True
                result["partial_reason_code"] = (
                    f"SECTOR_PARTIAL_{rows.get('error_type') or 'PROVIDER_ERROR'}")
                if server_time:
                    result["server_time"] = server_time
                    result["server_time_basis"] = "http_date_header"
                return result
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
        retried_rows = _fetch_all_industry_rows(
            deadline=deadline, server_holder=server_holder)
        if isinstance(retried_rows, dict) and retried_rows.get("error"):
            # 重读失败也要保留本轮已取对象，并合并重读已取到的行；
            # 不能把失败悄悄吞掉后返回一个看似完整的普通结果。
            retry_rows = retried_rows.get("rows") or []
            merged = _merge_industry_rows(rows, retry_rows)
            merged_result = _select_industries(merged, codes, names)
            merged_result["retry_attempts"] = 2
            merged_result["recovered_missing"] = sorted(
                set(result["missing"]) - set(merged_result["missing"]))
            if merged_result["items"]:
                merged_result["partial"] = True
                merged_result["partial_reason_code"] = (
                    f"SECTOR_PARTIAL_{retried_rows.get('error_type') or 'PROVIDER_ERROR'}")
                result = merged_result
            else:
                return {
                    "items": [],
                    "by_code": {},
                    "by_name": {},
                    "missing": list(codes or []) + list(names or []),
                    "error": retried_rows.get("error") or "industry retry failed",
                    "error_type": retried_rows.get("error_type") or "PROVIDER_ERROR",
                    "source": "ths_industry",
                }
        else:
            retried = _select_industries(retried_rows, codes, names)
            result["retry_attempts"] = 2
            result["recovered_missing"] = sorted(set(result["missing"]) - set(retried["missing"]))
            result = {**retried, "retry_attempts": 2,
                      "recovered_missing": result["recovered_missing"]}
    if server_time:
        result["server_time"] = server_time
        result["server_time_basis"] = "http_date_header"
    return result


def _source_time(value) -> datetime | None:
    """只接受源显式给出的时间；缺失或非法即未知，不由调用方补造。"""
    try:
        if not isinstance(value, str) or len(value) < 19:
            return None
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.replace(tzinfo=timezone(timedelta(hours=8))) if stamp.tzinfo is None else stamp
    except ValueError:
        return None


def _merge_industry_rows(*groups) -> list[dict]:
    """合并多轮已取到的行，按 code 去重；先到的行优先。"""
    merged = []
    seen = set()
    for group in groups:
        for row in group or []:
            if not isinstance(row, dict):
                continue
            code = row.get("code")
            if not code or code in seen:
                continue
            merged.append(row)
            seen.add(code)
    return merged


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

    # 板块行情时点与领涨股时点是两件事：领涨股只能用它自己的源时间，
    # 不能继承 quote_time、HTTP Date 或 fetched_at（11-02 #2 百洋医药
    # +14.84% 与同轮快照 -3.68% 矛盾，但页面没有时间可证）。
    # 新旧必须逐行比该行自己的 quote_time；跨板块取 min 会漏掉单行旧领涨。
    quote_times = []
    quote_complete = bool(deduped)
    leader_times = []
    leader_statuses = []
    has_leader = False
    items = []
    for row in deduped:
        item = dict(row)
        quote_stamp = _source_time(row.get('quote_time') or row.get('data_as_of'))
        if quote_stamp is None:
            quote_complete = False
        else:
            quote_times.append(quote_stamp)
        leader_stamp = None
        leader_status = 'not_applicable'
        if str(row.get('leader') or '').strip():
            has_leader = True
            leader_stamp = _source_time(
                row.get('leader_quote_time') or row.get('leader_data_as_of'))
            if leader_stamp is None:
                leader_status = 'unknown'
            elif quote_stamp is not None and leader_stamp < quote_stamp:
                leader_status = 'older_than_quote'
            else:
                leader_status = 'upstream_quote_time'
            if leader_stamp is not None:
                leader_times.append(leader_stamp)
            leader_statuses.append(leader_status)
        item['leader_data_as_of'] = (leader_stamp.isoformat(timespec='seconds')
                                     if leader_stamp else None)
        item['leader_time_status'] = leader_status
        items.append(item)

    data_time = (min(quote_times).isoformat(timespec='seconds')
                 if quote_complete and quote_times else None)
    if not has_leader:
        leader_time, leader_status, leader_reason = None, 'not_applicable', None
    elif 'unknown' in leader_statuses:
        leader_time, leader_status, leader_reason = None, 'unknown', 'DATA_TIME_UNKNOWN'
    elif 'older_than_quote' in leader_statuses:
        leader_time = min(leader_times)
        leader_status, leader_reason = 'older_than_quote', 'LEADER_TIME_OLDER_THAN_QUOTE'
    else:
        leader_time = min(leader_times)
        leader_status, leader_reason = 'upstream_quote_time', None
    return {
        "items": items,
        "by_code": {row["code"]: row for row in items},
        "by_name": {row["name"]: row for row in items},
        "missing": missing,
        "source": "ths_industry",
        'data_as_of': data_time,
        'data_time_basis': 'upstream_quote_time' if data_time else 'provider_not_supplied',
        'data_time_reason_code': None if data_time else 'DATA_TIME_UNKNOWN',
        'leader_data_as_of': (leader_time.isoformat(timespec='seconds')
                              if leader_time else None),
        'leader_time_basis': 'upstream_quote_time' if leader_time else 'provider_not_supplied',
        'leader_time_status': leader_status,
        'leader_time_reason_code': leader_reason,
    }


_server = threading.local()


def _remember_server_time(resp) -> str | None:
    """页面本身不带数据时间；响应头 Date 是上游服务器生成该页的时间（H20 #6），取最早一页的。"""
    headers = getattr(resp, "headers", None) or {}
    value = headers.get("Date") if hasattr(headers, "get") else None
    if getattr(_server, "value", None) is not None:
        return _server.value
    if not value:
        return None
    try:
        moment = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    _server.value = moment.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    return _server.value


def _bounded_timeout(remaining: float) -> tuple[float, float]:
    connect = min(1.0, max(.01, remaining / 4))
    return connect, max(.01, remaining - connect)


def _fetch_industry_page(page: int, timeout=None) -> tuple[list[dict], str | None, str | None]:
    resp = requests.get(
        _URL_TEMPLATE.format(page=page),
        headers=_HEADERS,
        timeout=_bounded_timeout(10.0) if timeout is None else timeout,
    )
    if resp.status_code != 200:
        return [], f"HTTP_{resp.status_code}", None
    server_time = _remember_server_time(resp)
    resp.encoding = "gbk"
    return _parse_industry_rows(resp.text), None, server_time


def _fetch_all_industry_rows(max_pages: int = 5, deadline: float | None = None,
                             server_holder: dict | None = None) -> list[dict] | dict:
    rows: list[dict] = []
    first_page_size: int | None = None
    last_page_size: int | None = None
    if deadline is None:
        deadline = _monotonic() + SECTOR_WALL_BUDGET_SEC
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ths-industry")
    try:
        for page in range(1, max_pages + 1):
            remaining = deadline - _monotonic()
            if remaining <= 0:
                # 保留已取到的页，交给调用方如实标 partial；不当作合法结束。
                return {"error": "industry wall-clock budget exhausted",
                        "error_type": "TIMEOUT", "rows": rows}
            future = pool.submit(
                _fetch_industry_page, page, timeout=_bounded_timeout(remaining))
            try:
                page_rows, error, page_server = future.result(timeout=max(.01, remaining))
            except FuturesTimeout:
                # 上游忽略 socket timeout（滴流响应/多地址连接）时不再等待；
                # 线程继续在后台跑完，其结果不会进入本轮返回值。
                return {"error": "industry wall-clock budget exhausted",
                        "error_type": "TIMEOUT", "rows": rows}
            if page_server and server_holder is not None and not server_holder.get("value"):
                server_holder["value"] = page_server
            if error:
                return {"error": "industry HTTP failure", "error_type": error, "rows": rows}
            # 2026-10-26（批次4 / B2）：上一页是满页时，空页是偶发截断而不是
            # 分页结束，重试一次；上一页本来就不满时，空页才是正常终止。
            if not page_rows and page > 1 and last_page_size == first_page_size:
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    return {"error": "industry wall-clock budget exhausted",
                            "error_type": "TIMEOUT", "rows": rows}
                future = pool.submit(
                    _fetch_industry_page, page, timeout=_bounded_timeout(remaining))
                try:
                    page_rows, error, page_server = future.result(timeout=max(.01, remaining))
                except FuturesTimeout:
                    return {"error": "industry wall-clock budget exhausted",
                            "error_type": "TIMEOUT", "rows": rows}
                if page_server and server_holder is not None and not server_holder.get("value"):
                    server_holder["value"] = page_server
                if error:
                    return {"error": "industry HTTP failure", "error_type": error, "rows": rows}
            if not page_rows:
                if not rows:
                    return {"error": "industry table not parsed", "error_type": "PARSE_FAILED"}
                break
            rows.extend(page_rows)
            if first_page_size is None:
                first_page_size = len(page_rows)
            last_page_size = len(page_rows)
    except Exception as e:
        return {"error": "industry request failed",
                "error_type": "TIMEOUT" if isinstance(e, requests.Timeout) else "NETWORK_ERROR",
                "rows": rows}
    finally:
        # 晚到的上游线程不能被取消；不 join，否则预算在收尾时又被花一次。
        pool.shutdown(wait=False)
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
