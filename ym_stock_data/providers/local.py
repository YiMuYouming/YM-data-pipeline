"""Adapters over existing local source modules.

This module owns no HTTP implementation.  It only maps a canonical provider
name and intent to the source functions that already implement that transport.
"""

from __future__ import annotations

import re
import socket
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Callable

from ..contracts import TZ_SHANGHAI
from ..sources import (
    eastmoney_index,
    eastmoney_stock,
    filings,
    news,
    northbound,
    pytdx,
    research,
    sina_index,
    stock_events,
    tencent,
    ths_hot,
    ths_industry,
)
from ..sources.limit_state import fetch_limit_promotion, fetch_limit_state
from .base import ProviderOutcome


# 1m/5m/15m/60m 是盘中分钟周期：默认窗口是"当日 09:30 起的全部已完成分钟"，
# 不是隐式的最近 48 根。
MINUTE_PERIODS = frozenset({"1m", "5m", "15m", "60m"})
MINUTE_SIZES = {"1m": 1, "5m": 5, "15m": 15, "60m": 60}
SESSION_START = dtime(9, 30)
SESSION_END = dtime(15, 0)
LUNCH_START, LUNCH_END = dtime(11, 30), dtime(13, 0)
#: 创业板（300/301）、科创板（688/689）有 15:05—15:30 盘后固定价格交易；它不是连续竞价分钟
AFTER_HOURS_PRICING_PREFIXES = ("300", "301", "688", "689")


def _now() -> datetime:
    """分钟线完成度判定的时钟（测试可替换；生产就是当前台北时间）。"""
    return datetime.now(TZ_SHANGHAI)


def annotate_minute_bars(raw: dict, params: dict, *, source: str | None = None,
                         now: datetime | None = None) -> dict:
    """盘中分钟契约（P10/#6#8、D27-05）：窗口、截断、未完成末根与采样价语义。

    2026-10-27 真实会话暴露四件事，都在这里如实标：
    1. 默认只回最近 48 根（缺开盘段）且没有截断标记；
    2. 未完成的末根照常返回、没有 incomplete 标记，其桶标签还晚于抓取时间；
    3. 腾讯分时给的是每分钟**采样价**，O=H=L=C 都是该采样价，不是该分钟的真实
       区间高低；
    4. 09:30 之前的集合竞价采样混进当日分钟序列。
    这里只加标记与窗口，不改任何价格、不补 0、不补缺失分钟。
    """

    bars = raw.get("bars") if isinstance(raw, dict) else None
    if not isinstance(bars, list):
        return raw
    period = params.get("period")
    size = MINUTE_SIZES.get(period)
    if not size:
        return raw
    now = now or _now()
    session_bars: list[tuple[datetime | None, dict]] = []
    pre_dropped = 0
    post_dropped = 0
    off_dropped = 0
    after_hours: list[dict] = []
    code = str(params.get("code") or raw.get("code") or "")
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        label = str(bar.get("datetime") or bar.get("time") or "")
        try:
            moment = datetime.fromisoformat(label)
        except ValueError:
            # 时间标签不可解析的行不静默丢弃（也不臆断它的时段/完成度）。
            session_bars.append((None, bar))
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=TZ_SHANGHAI)
        clock = moment.time()
        if clock < SESSION_START:
            pre_dropped += 1
            continue
        if LUNCH_START < clock < LUNCH_END:
            off_dropped += 1  # 午休时段不该有分钟行
            continue
        if clock > SESSION_END:
            # H6：15:00 之后的行不是连续竞价分钟，不混进 1m
            if code.startswith(AFTER_HOURS_PRICING_PREFIXES):
                after_hours.append(bar)
            else:
                post_dropped += 1
            continue
        session_bars.append((moment, bar))
    total = len(session_bars)
    requested = params.get("count")
    dropped = 0
    truncated = False
    if requested is not None and total > requested:
        dropped = total - requested
        truncated = True
        session_bars = session_bars[-requested:]
    kept: list[dict] = []
    incomplete = 0
    for moment, bar in session_bars:
        if moment is None:
            kept.append(bar)
            continue
        finished = moment + timedelta(minutes=size)
        if moment.date() == now.date() and now < finished:
            bar = dict(bar, incomplete=True)
            incomplete += 1
        kept.append(bar)
    completed = [bar for bar in kept if not bar.get("incomplete")]
    closes = [bar.get("close") for bar in completed
              if isinstance(bar.get("close"), (int, float))]
    mas: dict = {}
    for span, key in ((5, "MA5"), (10, "MA10"), (20, "MA20")):
        if len(closes) >= span:
            mas[key] = round(sum(closes[-span:]) / span, 2)
    result = dict(raw)
    result["bars"] = kept
    result["period"] = period
    result["total_bars"] = total
    result["returned_bars"] = len(kept)
    result["completed_bars"] = len(kept) - incomplete
    result["incomplete_bars"] = incomplete
    result["truncated"] = truncated
    result["dropped_bars"] = dropped
    result["pre_session_bars_dropped"] = pre_dropped
    result["post_session_bars_dropped"] = post_dropped
    result["off_session_bars_dropped"] = off_dropped
    if after_hours:
        result["after_hours_pricing"] = {
            "bars": after_hours,
            "note": "创业板／科创板盘后固定价格交易（15:05—15:30），不是连续竞价分钟，单列、不混入 1m。"}
    result["requested_count"] = requested
    if closes:
        # 未完成末根的量价还会变：last_close 与均线只按已完成分钟。
        result["last_close"] = closes[-1]
        result["mas"] = mas
    result["bar_semantics"] = {
        "label": "bucket_start",
        "coverage_seconds": size * 60,
        "incomplete_rule": (
            "桶标签是起始分钟，覆盖 [label, label+period)；标签+period 之后才算完成。"
            "抓取时刻之前的最后一桶常是按右端标签给出的未完成桶，已标 incomplete=true，"
            "其 O/H/L/C 会随本轮后续采样变化，不当已完成分钟引用。"),
        "price_kind": "tencent_minute_sample" if source == "tencent" else "bar_ohlc",
        "high_low_note": (
            "腾讯分时接口给的是每分钟采样价：O=H=L=C 都是该采样价，不是该分钟完整区间的"
            "真实高低；vwap/avg_price 是上游当日累计口径。"
            if source == "tencent" else
            "bar 的 high/low 是上游给出的区间高低。"),
        "session_window": {"start": "09:30", "end": "15:00",
                           "lunch_gap": ["11:30", "13:00"],
                           "default": "当日 09:30 起的全部已完成分钟"},
    }
    return result


LOCAL_PROVIDER_NAMES = frozenset(
    {
        "pytdx",
        "eastmoney",
        "tencent",
        "sina",
        "ths_industry",
        # 板块强度是本地合成的（输入来自 market_facts / sector_index /
        # industry_flow），但它确实是一个被路由直接调起的适配器，
        # 与 ths_industry、market_facts 同一性质，所以登记在这里。
        "board_strength",
        "northbound",
        "ths_hot",
        "pytdx_index",
        "sina_index",
        "pytdx_breadth",
        "eastmoney_breadth",
        "eastmoney_limit_pool",
        "eastmoney_index",
        "eastmoney_stock",
        "eastmoney_datacenter",
        "eastmoney_research",
        "cninfo",
        "cls",
    }
)
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _now_iso() -> str:
    return datetime.now(TZ_SHANGHAI).isoformat(timespec="seconds")


def _error_code(value: object, default: str = "PROVIDER_ERROR") -> str:
    candidate = str(value or "")
    return candidate if _SAFE_CODE.fullmatch(candidate) else default


def _compact_date(value: object) -> str | None:
    text = str(value or "")[:10]
    if re.fullmatch(r"\d{8}", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace("-", "")
    return None


def _actual_source(provider: str, raw: dict) -> str:
    meta = raw.get("_meta", {}) if isinstance(raw.get("_meta"), dict) else {}
    source = meta.get("fallback_to") or meta.get("source") or raw.get("source")
    marker = raw.get("_source")
    if isinstance(marker, str):
        source = marker
    if not isinstance(source, str) or not source or source == "none":
        return provider
    if provider in {"pytdx_breadth", "eastmoney_breadth"} and source in {
        "eastmoney",
        "eastmoney_fallback",
        "eastmoney_index_fallback",
    }:
        return "eastmoney_breadth"
    aliases = {
        "eastmoney_fallback": "eastmoney",
        "eastmoney_index_fallback": "eastmoney",
        # research.fetch_reports 返回的 source 是 eastmoney_reportapi，
        # 必须映射到注册的 eastmoney_research，否则被判 INCOMPATIBLE_PROVIDER。
        "eastmoney_reportapi": "eastmoney_research",
        "tencent_fallback": "tencent",
        "tencent_index_fallback": "tencent",
        "sina_fallback": "sina",
        "cls_telegraph": "cls",
        "northbound_hsgt": "northbound",
    }
    return aliases.get(source, source.removesuffix("_fallback"))


def _row_sources(provider: str, raw: dict) -> set[str]:
    """Return effective sources for quote rows, including unmarked PyTDX rows."""

    sources: set[str] = set()
    default_source = _actual_source(provider, raw)
    for key, value in raw.items():
        if key in {"_meta", "_source", "error", "error_type"}:
            continue
        if not isinstance(value, dict) or value.get("error"):
            continue
        marker = value.get("_source")
        if isinstance(marker, str) and marker:
            sources.add(_actual_source(provider, {"_source": marker}))
        else:
            sources.add(default_source)
    return sources


def _row_count(intent: str, raw: dict) -> int:
    if intent == "review_sentiment" and "_total" in raw:
        try:
            return max(0, int(raw.get("_total") or 0))
        except (TypeError, ValueError):
            return 0
    # boards 是 market_board_strength 的行容器（一个板块一行）。
    # 少了它，provider 会把「有板块」报成 status=empty，上层按
    # STATUS_DATA_MISMATCH 拒掉整次查询——在线烟测就是这么发现的。
    for key in ("datas", "items", "bars", "reports", "filings", "boards"):
        value = raw.get(key)
        if isinstance(value, list):
            return len(value)
    if intent in {"market_limit_state", "review_sentiment"} and {
        "zt_count", "zb_count", "dt_count"
    }.issubset(raw):
        return sum(int(raw.get(key, 0) or 0) for key in ("zt_count", "zb_count", "dt_count"))
    if intent == "stock_snapshot":
        return sum(
            1
            for key, value in raw.items()
            if key not in {"_meta", "_source", "error", "error_type"}
            and isinstance(value, dict)
            and not value.get("error")
        )
    if intent == "realtime_market":
        return int(any(key not in {"_meta", "_source"} for key in raw))
    return 0


def _project_limit_board(params: dict) -> dict:
    """Project the existing Eastmoney pool aggregate into board semantics."""

    raw = fetch_limit_state(date=params.get("date"))
    if raw.get("error"):
        return raw
    pool_name = {
        "up": "zt",
        "down": "dt",
        "broken": "zb",
        "yesterday": "yzt",
    }[params["kind"]]
    pools = raw.get("pools") if isinstance(raw.get("pools"), dict) else {}
    return {
        "kind": params["kind"],
        "date": raw.get("date"),
        "items": list(pools.get(pool_name) or []),
        "source": "eastmoney_limit_pool",
    }


def _legacy_industry_flow(params: dict) -> dict:
    raw = ths_industry.fetch_industry_summary(top_n=params.get("limit", 20))
    if raw.get("error"):
        return raw
    raw_date = raw.get("trade_date") or raw.get("date")
    requested_date = _compact_date(params.get("trade_date"))
    observed_date = _compact_date(raw_date)
    current_session = params.get("current_session") is True or params.get("use_case") == "realtime_poll"
    if not raw_date:
        if not (
            current_session
        ) or "trade_date" in params:
            return ProviderOutcome(
                provider="ths_industry",
                status="incompatible",
                error_code="DATE_UNVERIFIED",
            )
    elif current_session and observed_date != datetime.now(TZ_SHANGHAI).strftime("%Y%m%d"):
        return ProviderOutcome(
            provider="ths_industry",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    elif requested_date and observed_date != requested_date:
        return ProviderOutcome(
            provider="ths_industry",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    rows = []
    seen = set()
    for row in [*(raw.get("top") or []), *(raw.get("bottom") or [])]:
        code = row.get("code")
        if code in seen:
            continue
        seen.add(code)
        rows.append(row)
    return {
        "trade_date": raw_date,
        "items": rows,
        "source": "ths_industry",
    }


def _board_strength(params: dict) -> dict:
    """逐板块强度：本地合成，输入全部来自已注册的既有 capability。

    这不是一个新的数据源——涨停来自 market_facts 的封存池、三日资金净额与
    排名变化来自 industry_flow 历史表（moneyflow_ind_ths，逐日全行业 90 行）。
    provider 记成 ``board_strength``，是为了在 attempts 里如实写明"这些数是
    算出来的"。

    资金流**按板块分别取**（审计阻断 1）：把同一份全市场 flows 传给每个板块、
    内核取尾巴三行，等于 103 个板块拿到同一个数。现在按交易日历取三天窗口，
    内核按板块身份过滤后各自求和；概念不在行业资金表里 → null + typed gap。
    """
    from ..board_strength import board_strength, load_board_definitions

    trade_date = _compact_date(params.get("trade_date"))
    definitions = params.get("definitions")
    boards_meta = load_board_definitions(definitions)["boards"] if definitions else []
    wanted = [str(b) for b in (params.get("board_ids") or [])]
    board_ids = wanted or [str(entry["board_id"]) for entry in boards_meta]
    board_names = {
        str(entry["board_id"]): str(entry.get("board_name") or entry.get("name") or "")
        for entry in boards_meta
    }
    concept_ids = {
        str(entry["board_id"]) for entry in boards_meta
        if str(entry.get("type") or "") == "concept"
    }

    limit_pool = _limit_pool_for(trade_date)
    # 三天窗口（含当日）的行业资金流；概念板块不在该表内，rank/net_inflow
    # 由内核记 gap，不拿行业数据顶替（K1）。
    window_days = _flow_window_days(trade_date)
    flow_rows = _industry_flow_window(window_days)
    rank_changes = _rank_changes(flow_rows, window_days)

    boards, gaps = [], []
    for board_id in board_ids:
        payload = board_strength(
            board_id,
            # definitions 里的板块名要传下去：涨停池与资金流表记的是名字，
            # 不传就对不上
            board_name=board_names.get(board_id),
            pool=limit_pool,
            flows=flow_rows,
            # 行业：三日涨幅排名差（industry_flow 逐日 pct_change 排名）。
            # 概念不在表内 → 不传，内核记 typed gap。
            rank_change=(None if board_id in concept_ids
                         else rank_changes.get(board_id)),
            # 覆盖率是"成员样本覆盖率"，只有拿着成员名单的一方才算得出来。
            # 管道这一侧只有涨停池，拿不到成员比例——**不给就不给**，
            # 让内核按 K5 记 typed gap 并把数值置 null；写死 1.0 等于假装
            # 样本齐全，下游会把全 null 看成"这个板块今天没涨停"。
            coverage=_member_coverage(board_id, params),
            data_as_of=trade_date or "",
        )
        boards.append(payload)
        gaps.extend(payload.get("source_gaps") or [])
    return {
        "trade_date": trade_date,
        "boards": boards,
        "source_gaps": gaps,
        "flow_window_days": window_days,
        "source": "board_strength",
    }


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", "")
    if not text or text in {"-", "—", "N"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _flow_window_days(trade_date: str | None) -> list[str]:
    """三日窗口（含当日，按管道日历；K3——不自己算节假日）。"""
    from ..trading_calendar import previous_trading_day

    days = [trade_date] if trade_date else []
    while len(days) < 3:
        try:
            days.append(previous_trading_day(
                datetime.strptime(days[-1], "%Y%m%d").date()).strftime("%Y%m%d"))
        except Exception:
            break
    return days


def _industry_flow_window(days: list[str]) -> list[dict]:
    """逐日取行业资金流（已注册的 industry_flow capability；本地合成的输入）。

    一天一次调用、每次拿到全行业 90 行；按日拼接后交给内核按板块过滤。
    合法 empty（休市日）与失败分开记：失败记一条 provider 侧 gap 行。
    """
    from ym_stock_data import query

    rows: list[dict] = []
    for day in days:
        try:
            result = query("industry_flow", trade_date=day, limit=300)
        except Exception:
            continue
        items = (result.get("data") or {}).get("items") or []
        rows.extend(row for row in items if isinstance(row, dict))
    return rows


def _rank_changes(flow_rows: list[dict], days: list[str]) -> dict[str, int]:
    """行业三日涨幅排名差：今日排名 − 昨日排名（正=排名后退）。

    排名只在行业资金表内部横向做；概念不在表里，不进结果（内核记 gap）。
    """
    if len(days) < 2:
        return {}
    today, previous = days[0], days[1]

    def _ranks(day: str) -> dict[str, int]:
        rows = [row for row in flow_rows
                if str(row.get("trade_date") or "") == day
                and _number(row.get("pct_change")) is not None]
        rows.sort(key=lambda row: -_number(row.get("pct_change")))
        return {
            str(row.get("ts_code") or "").split(".")[0]: index
            for index, row in enumerate(rows)
        }

    today_ranks, previous_ranks = _ranks(today), _ranks(previous)
    changes: dict[str, int] = {}
    for board_id, rank in today_ranks.items():
        if board_id in previous_ranks:
            changes[board_id] = rank - previous_ranks[board_id]
    return changes


def _member_coverage(board_id: str, params: dict) -> float | None:
    """成员覆盖率由调用方给（它才知道成员名单）；没给就返回 None。"""
    table = params.get("member_coverage")
    if not isinstance(table, dict):
        return None
    value = table.get(board_id)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _limit_pool_for(trade_date: str | None) -> list[dict]:
    """涨停池来自封存的 market_facts；没有封存就返回空列表（不猜、不补）。

    封存表里存的是 ``board_count``，计算内核要的是 ``board``（几板）。
    封板时间（首封/最后封）表里没有这一列，``seal_time_distribution`` 已按
    审计回复 11 二.3 从本意图删除，归第二批（``market_facts`` 入库时存下来）。
    """
    if not trade_date:
        return []
    try:
        from ..market_facts import MarketFactStore

        store = MarketFactStore(read_only=True)
        run = store.latest_limit_run(trade_date)
        if not run:
            return []
        rows = store._events(run["id"], "up")
    except Exception:
        return []
    pool = []
    for row in rows:
        pool.append({
            "code": row.get("code"),
            "name": row.get("name"),
            "industry": row.get("industry") or "",
            "board": row.get("board_count") or 0,
        })
    return pool


def _legacy_northbound(params: dict) -> dict:
    raw = northbound.fetch_realtime()
    if raw.get("error"):
        return raw
    raw_date = raw.get("date") or raw.get("trade_date")
    if not raw_date:
        return ProviderOutcome(
            provider="northbound",
            status="incompatible",
            error_code="DATE_UNVERIFIED",
        )
    observed_date = _compact_date(raw_date)
    requested_date = _compact_date(params.get("trade_date"))
    current_session = params.get("current_session") is True or params.get("use_case") == "realtime_poll"
    if current_session and observed_date != datetime.now(TZ_SHANGHAI).strftime("%Y%m%d"):
        return ProviderOutcome(
            provider="northbound",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    if requested_date and not current_session and observed_date != requested_date:
        return ProviderOutcome(
            provider="northbound",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    return {
        "trade_date": raw_date,
        "items": list(raw.get("minutes") or []),
        "summary": {
            key: raw.get(key)
            for key in ("hgt_current_yi", "sgt_current_yi", "hgt_trend", "sgt_trend")
        },
        "source": "northbound_hsgt",
    }


def _legacy_hot_rank(params: dict) -> dict:
    date_value = params.get("trade_date")
    date_str = (
        f"{date_value[:4]}-{date_value[4:6]}-{date_value[6:]}"
        if isinstance(date_value, str) and len(date_value) == 8
        else None
    )
    raw = ths_hot.fetch_hot_with_zt_count(date_str)
    if raw.get("error"):
        return raw
    raw_date = raw.get("date") or raw.get("trade_date")
    observed_date = _compact_date(raw_date)
    requested_date = _compact_date(params.get("trade_date"))
    current_session = params.get("current_session") is True or params.get("use_case") == "realtime_poll"
    if not raw_date:
        return ProviderOutcome(
            provider="ths_hot",
            status="incompatible",
            error_code="DATE_UNVERIFIED",
        )
    if current_session and observed_date != datetime.now(TZ_SHANGHAI).strftime("%Y%m%d"):
        return ProviderOutcome(
            provider="ths_hot",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    if requested_date and not current_session and observed_date != requested_date:
        return ProviderOutcome(
            provider="ths_hot",
            status="incompatible",
            error_code="DATE_MISMATCH",
        )
    rows = list(raw.get("stocks") or [])
    if params.get("limit") is not None:
        rows = rows[: params["limit"]]
    return {
        "trade_date": raw_date,
        "items": rows,
        "reason_stats": raw.get("reason_stats", {}),
        "zt_count": raw.get("zt_count"),
        "source": "ths_hot",
    }


def _legacy_index_kline(params: dict, *, provider: str = "pytdx_index") -> dict | ProviderOutcome:
    codes = params.get("codes") or [params.get("index_code")]
    if provider == "pytdx_index" and params.get("period", "daily") not in {
        "daily",
        "weekly",
        "monthly",
    }:
        return ProviderOutcome(
            provider=provider,
            status="incompatible",
            error_code="INDEX_MINUTE_VOLUME_UNVERIFIED",
        )
    fetcher = {
        "eastmoney_index": eastmoney_index.fetch_index_kline,
        "sina_index": sina_index.fetch_index_kline,
        "pytdx_index": pytdx.fetch_index_kline,
    }[provider]
    items = []
    last_error = None
    for code in codes:
        result = fetcher(
            code,
            period=params.get("period", "daily"),
            count=params.get("count"),
            start_date=params.get("start_date"),
            end_date=params.get("end_date"),
        )
        if isinstance(result, dict) and not result.get("error"):
            items.append(result)
        elif isinstance(result, dict):
            last_error = result
    if len(codes) == 1:
        return items[0] if items else (last_error or {"error": "index kline unavailable", "error_type": "NO_DATA"})
    by_code = {
        item["index_code"]: {"rows": item.get("bars", [])}
        for item in items
    }
    for item in items:
        by_code[item["index_code"].split(".")[0]] = by_code[item["index_code"]]
    return {
        "items": items,
        "by_code": by_code,
        **{key.split(".")[0]: value for key, value in by_code.items() if "." in key},
        "source": provider,
    }


def _legacy_index_intraday_compare(
    params: dict, *, provider: str = "pytdx_index"
) -> dict | ProviderOutcome:
    if provider == "pytdx_index":
        return ProviderOutcome(
            provider=provider,
            status="incompatible",
            error_code="INDEX_MINUTE_VOLUME_UNVERIFIED",
        )
    fetcher = {
        "eastmoney_index": eastmoney_index.fetch_index_intraday_compare,
        "sina_index": sina_index.fetch_index_intraday_compare,
        "tencent": tencent.fetch_index_intraday_compare,
    }.get(provider)
    if fetcher is None:
        return ProviderOutcome(
            provider=provider,
            status="incompatible",
            error_code="INCOMPATIBLE_INTENT",
        )
    raw = fetcher(
        period=params.get("period", "15m"),
        trade_date=params.get("trade_date"),
    )
    if not isinstance(raw, dict) or raw.get("error"):
        return raw
    return raw


class LocalProvider:
    def __init__(self, name: str):
        if name not in LOCAL_PROVIDER_NAMES:
            raise ValueError(f"unknown local provider: {name}")
        self.name = name

    def probe(self) -> dict:
        return {
            "provider": self.name,
            "status": "configured_unverified",
            "auth": {"required": False, "status": "not_required"},
        }

    def call(self, intent: str, params: dict) -> ProviderOutcome:
        started = datetime.now().timestamp()
        try:
            raw = self._dispatch(intent, params)
        except (TimeoutError, socket.timeout):
            return self._failure(started, "timeout", "TIMEOUT")
        except ImportError:
            return self._failure(started, "dependency_missing", "DEPENDENCY_MISSING")
        except Exception as error:
            return self._failure(
                started,
                "provider_error",
                _error_code(type(error).__name__),
            )
        if isinstance(raw, ProviderOutcome):
            return raw
        if not isinstance(raw, dict):
            return self._failure(started, "provider_error", "INVALID_RESPONSE")
        nested_data = raw.get("data")
        if isinstance(nested_data, dict):
            flattened = dict(nested_data)
            flattened.update({key: value for key, value in raw.items() if key != "data"})
            raw = flattened

        meta = raw.get("_meta", {}) if isinstance(raw.get("_meta"), dict) else {}
        if raw.get("error") or meta.get("error"):
            error_type = raw.get("error_type") or meta.get("error_type")
            status = {
                "breaker_open": "breaker_open",
                "timeout": "timeout",
                "auth_error": "auth_error",
            }.get(str(error_type), "provider_error")
            return self._failure(started, status, _error_code(error_type))

        count = _row_count(intent, raw)
        actual_source = _actual_source(self.name, raw)
        if intent == "stock_snapshot":
            row_sources = _row_sources(self.name, raw)
            if len(row_sources) > 1:
                return self._failure(started, "provider_error", "MIXED_PROVENANCE")
            if row_sources:
                actual_source = next(iter(row_sources))
        provenance = None
        if actual_source != self.name:
            provenance = {
                "verified": True,
                "fallback_from": self.name,
                "kind": "source_internal",
            }
        return ProviderOutcome(
            provider=actual_source,
            status="success" if count else "empty",
            data=raw,
            fetched_at=meta.get("fetched_at") or _now_iso(),
            latency_ms=max(0, int((datetime.now().timestamp() - started) * 1000)),
            quality={
                "status": "normal" if count else "empty",
                "returned_count": count,
                "reason_codes": [],
            },
            auth={"required": False, "status": "not_required"},
            provenance=provenance,
        )

    def _failure(self, started: float, status: str, error_code: str) -> ProviderOutcome:
        return ProviderOutcome(
            provider=self.name,
            status=status,
            error_code=error_code,
            latency_ms=max(0, int((datetime.now().timestamp() - started) * 1000)),
            auth={"required": False, "status": "not_required"},
        )

    def _dispatch(self, intent: str, params: dict) -> dict | ProviderOutcome:
        dispatch: dict[tuple[str, str], Callable[[], dict | ProviderOutcome]] = {
            ("pytdx", "realtime_market"): pytdx.fetch_index,
            ("eastmoney", "realtime_market"): pytdx._fallback_index,
            ("tencent", "realtime_market"): pytdx._fallback_index_tencent,
            ("pytdx", "stock_snapshot"): lambda: pytdx.fetch_quotes(
                params["codes"], fast=bool(params.get("_fast_quote"))
            ),
            ("tencent", "stock_snapshot"): lambda: tencent.fetch_quotes(params["codes"]),
            ("pytdx", "stock_kline"): lambda: self._pytdx_kline(params),
            ("tencent", "stock_kline"): lambda: self._http_kline(
                provider="tencent", params=params
            ),
            ("sina", "stock_kline"): lambda: self._http_kline(
                provider="sina", params=params
            ),
            ("eastmoney_stock", "stock_kline"): lambda: eastmoney_stock.fetch_kline(
                params["code"],
                period=params.get("period", "daily"),
                count=params.get("count"),
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
                adjustment=params.get("adjustment", "none"),
            ),
            ("ths_industry", "sector_index"): lambda: ths_industry.fetch_sector_index(
                codes=params.get("codes"), names=params.get("names")
            ),
            ("ths_industry", "industry_flow"): lambda: _legacy_industry_flow(params),
            ("board_strength", "market_board_strength"): lambda: _board_strength(params),
            ("northbound", "northbound_flow"): lambda: _legacy_northbound(params),
            ("ths_hot", "legacy_hot_rank"): lambda: _legacy_hot_rank(params),
            ("pytdx_index", "index_kline"): lambda: _legacy_index_kline(params),
            ("eastmoney_index", "index_kline"): lambda: _legacy_index_kline(
                params, provider="eastmoney_index"
            ),
            ("sina_index", "index_kline"): lambda: _legacy_index_kline(
                params, provider="sina_index"
            ),
            ("pytdx_index", "index_intraday_compare"): lambda: _legacy_index_intraday_compare(
                params
            ),
            ("eastmoney_index", "index_intraday_compare"): lambda: _legacy_index_intraday_compare(
                params, provider="eastmoney_index"
            ),
            ("sina_index", "index_intraday_compare"): lambda: _legacy_index_intraday_compare(
                params, provider="sina_index"
            ),
            ("tencent", "index_intraday_compare"): lambda: _legacy_index_intraday_compare(
                params, provider="tencent"
            ),
            ("pytdx_breadth", "review_sentiment"): pytdx.fetch_breadth,
            ("eastmoney_breadth", "review_sentiment"): pytdx._fallback_breadth,
            ("eastmoney_limit_pool", "review_sentiment"): lambda: fetch_limit_state(
                date=params.get("date")
            ),
            ("eastmoney_limit_pool", "market_limit_state"): lambda: (
                fetch_limit_promotion(params["date"], params["previous_date"])
                if params.get("previous_date")
                else fetch_limit_state(date=params.get("date"))
            ),
            ("eastmoney_limit_pool", "market_limit_board"): lambda: _project_limit_board(
                params
            ),
            ("eastmoney_datacenter", "stock_event"): lambda: stock_events.fetch_stock_event(
                event=params["event"],
                code=params["code"],
                page_size=params.get("page_size", 30),
            ),
            ("eastmoney_research", "research"): lambda: research.fetch_reports(
                code=params["code"],
                days=params.get("days", 90),
                max_pages=params.get("max_pages", 15),
            ),
            ("cninfo", "filings"): lambda: filings.fetch_filings(
                code=params["code"],
                days=params.get("days", 90),
                max_pages=params.get("max_pages", 3),
            ),
            ("cls", "news"): lambda: news.fetch_news(limit=params.get("limit", 20)),
        }
        callback = dispatch.get((self.name, intent))
        if callback is None:
            return ProviderOutcome(
                provider=self.name,
                status="dependency_missing"
                if self.name == "sina" and intent == "stock_snapshot"
                else "incompatible",
                error_code="PROVIDER_ADAPTER_MISSING"
                if self.name == "sina" and intent == "stock_snapshot"
                else "INCOMPATIBLE_INTENT",
            )
        return callback()

    @staticmethod
    def _pytdx_kline(params: dict) -> dict:
        if params.get("adjustment", "none") != "none":
            return ProviderOutcome(
                provider="pytdx",
                status="incompatible",
                error_code="ADJUSTMENT_UNSUPPORTED",
            )
        fetch_params = {"period": params.get("period", "daily")}
        for key in ("count", "start_date", "end_date"):
            if params.get(key) is not None:
                fetch_params[key] = params[key]
        raw = pytdx.fetch_kline(params["code"], **fetch_params)
        if not isinstance(raw, dict):
            return raw
        nested_data = raw.get("data")
        if isinstance(nested_data, dict):
            flattened = dict(nested_data)
            flattened.update({key: value for key, value in raw.items() if key != "data"})
            raw = flattened
        result = dict(raw)
        result.setdefault("period", params.get("period", "daily"))
        period = params.get("period", "daily")
        volume_multiplier = 100 if period in {"daily", "weekly", "monthly"} else 1
        result = LocalProvider._canonicalize_kline(
            result, params, volume_multiplier=volume_multiplier
        )
        if params.get("count") is None:
            return result
        bars = result.get("bars")
        if isinstance(bars, list):
            count = params["count"]
            result["bars"] = list(bars[-count:])
            result["requested_count"] = count
            result["returned_bars"] = len(result["bars"])
        return result

    @staticmethod
    def _http_kline(*, provider: str, params: dict) -> dict:
        period = params.get("period", "daily")
        minute = period in MINUTE_PERIODS
        # 分钟周期默认不截断：由 _annotate_minute_bars 按"当日 09:30 起全部已完成
        # 分钟"给窗口，并把任何截断如实标成 truncated=true（P10/#6#8）。
        count = params.get("count") or (
            30 if period in {"daily", "60m"} else (None if minute else 48))
        adjustment = params.get("adjustment", "none")
        if adjustment != "none" and provider != "tencent":
            return ProviderOutcome(
                provider=provider,
                status="incompatible",
                error_code="ADJUSTMENT_UNSUPPORTED",
            )
        if provider == "tencent":
            minute_window = {
                key: params[key] for key in ("start_date", "end_date")
                if key in params
            } if period in {"1m", "5m", "15m", "60m"} else {}
            try:
                bars = pytdx._fetch_tencent_kline(
                    params["code"],
                    period=period,
                    count=count,
                    adjustment=adjustment,
                    **minute_window,
                )
            except tencent.TencentTransportError as error:
                # 2026-10-26 R3 item8a：传输失败转成显式失败，不再冒充合法空。
                return ProviderOutcome(
                    provider=provider,
                    status=error.status,
                    error_code=error.error_code,
                    detail=error.describe(),
                    latency_ms=error.elapsed_ms,
                    provenance={
                        "kind": "transport_failure",
                        "error_type": error.error_type,
                        "http_status": error.http_status,
                        "elapsed_ms": error.elapsed_ms,
                    },
                    auth={"required": False, "status": "not_required"},
                )
        else:
            bars = pytdx._fetch_sina_kline(params["code"], period=period, count=count)
        result = pytdx._build_kline_result(
            params["code"],
            bars,
            source=provider,
        )
        volume_multiplier = (
            tencent.volume_unit_multiplier(params["code"])
            if provider == "tencent" and period in {"daily", "weekly", "monthly"}
            else 1
        )
        canonical = LocalProvider._canonicalize_kline(
            result, params, volume_multiplier=volume_multiplier
        )
        if minute:
            return annotate_minute_bars(canonical, params, source=provider)
        return canonical

    @staticmethod
    def _canonicalize_kline(
        raw: dict, params: dict, *, volume_multiplier: float
    ) -> dict:
        if not isinstance(raw, dict) or raw.get("error"):
            return raw
        normalized = dict(raw)
        bars = []
        for row in raw.get("bars", []):
            if not isinstance(row, dict):
                continue
            stamp = row.get("datetime", row.get("time"))
            volume = row.get("volume", row.get("vol"))
            amount = row.get("amount")
            try:
                volume = float(volume) * volume_multiplier if volume is not None else None
            except (TypeError, ValueError):
                volume = None
            try:
                amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                amount = None
            bar = {
                "datetime": str(stamp or ""),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": volume,
                "amount": amount,
            }
            # 分时均价字段按来源原样透传（目前只有腾讯分钟 K 提供）；
            # 其它周期/来源没有该字段时不补造，也不改变既有 bar shape。
            for price_key in ("vwap", "avg_price"):
                value = row.get(price_key)
                if value is not None:
                    try:
                        bar[price_key] = float(value)
                    except (TypeError, ValueError):
                        pass
            bars.append(bar)
        normalized["bars"] = bars
        normalized["total_bars"] = len(bars)
        normalized["adjustment"] = params.get("adjustment", "none")
        normalized["volume_unit"] = "share"
        normalized["amount_unit"] = "CNY"
        return normalized
