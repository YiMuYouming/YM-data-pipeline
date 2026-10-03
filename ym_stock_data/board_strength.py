"""逐板块强度（W4 S1，开工单 W4 第二节第 2 点）。

字段全部按 K5 的 `{value, source, data_as_of, status}` 输出。覆盖率不足
（成员样本 < :data:`MIN_COVERAGE`）时数值置 null 并记 typed gap，**不给 0**——
一个凭空捏造的 0 会被下游当成"今天这个板块没有涨停"来用。

概念板块是另一种 `type`，与同花顺二级行业（`881xxx`）分开装载，不混排。

`seal_time_distribution`（封板时间分布）2026-10-02 按审计回复 11 二.3 **删除**：
封存表没有这一列，现在没有生产者，铁律 4 下不许留必填字段。上游其实有
（StockToday 涨停明细带首封/最后封时间）——记入遗留归第二批：`market_facts`
入库时把这两列存下来，存了以后再加回本意图。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import indicators as _indicators

MIN_COVERAGE = 0.8
INDUSTRY_PREFIX = "881"
# 概念前缀不是单个 885：实测 ths_index type=N 共 916 条，分布在
# 864/865/875/883/885/886 上。判据始终是数据源的 type 字段（N=概念、I=行业），
# 这里的前缀只是 definitions 文件里的形状检查（见 api.CONCEPT_CODE_PREFIXES）。
CONCEPT_CODE_PREFIXES = ("864", "865", "875", "883", "885", "886")

FIELDS: Mapping[str, str] = {
    "limit_up_count": "market_facts",
    "limit_up_2plus_count": "market_facts",
    # 本期不产出：板块指数日 K 在管道没有意图（ths_daily 未接线；sector_index
    # 返回的是实时快照不是日线序列）。需要时新意图，其次把这里接上。
    "index_position_vs_ma5": "sector_index",
    # 行业走 industry_flow 历史表（moneyflow_ind_ths，逐日全行业 90 行）；
    # 概念不在该表内 —— 置 null + typed gap，不拿行业冒充概念（K1）。
    "net_inflow_3d": "industry_flow",
    # 本期不产出：逐板块中军需要“板块成员 × 20 日日线”，管道 indicators 的
    # 唯一实现只出市场级比例（TOP100 cohort，不出名单）。归第二批。
    "midcap_above_ma20_pct": "indicators.midcap_above_ma20_pct",
    # 行业：industry_flow 逐日 pct_change 的排名差；概念不在表内 → null + gap。
    "rank_change": "industry_flow",
}

# gap_code 第二段：这个字段的生产者/未产出原因（审计阻断 4：null 必须能看出
# 是“没有生产者”还是“今天没有”）。
FIELD_PRODUCERS: Mapping[str, str] = {
    "limit_up_count": "market_facts",
    "limit_up_2plus_count": "market_facts",
    "index_position_vs_ma5": "no_board_daily_k_intent",
    "net_inflow_3d": "industry_flow_history_missing_for_board",
    "midcap_above_ma20_pct": "per_board_member_bars_not_exposed",
    "rank_change": "industry_flow_history_missing_for_board",
}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def board_strength(
    board_id: str,
    *,
    board_name: str | None = None,
    pool: Iterable[Mapping[str, Any]] | None = None,
    industry_bars: Iterable[Mapping[str, Any]] | None = None,
    flows: Iterable[Mapping[str, Any]] | None = None,
    members: Iterable[str] | None = None,
    coverage: float | None = None,
    rank_change: int | None = None,
    midcap_pct: float | None = None,
    data_as_of: str = "",
) -> dict[str, Any]:
    """Build one board's strength payload. Pure: no IO, no provider calls."""
    rows = [row for row in (pool or []) if isinstance(row, dict)]
    # 涨停池按**行业名**记录（"房地产开发"），而 board_id 是代码（881121）。
    # 拿名字跟代码比永远匹配不上——字段会全部 missing，而且看不出是哪儿错的。
    # 两个键都认：池子按名字给的按名字比，按代码给的按代码比。
    keys = {key for key in (board_id, board_name) if key}
    board_rows = [
        row for row in rows
        if str(row.get("industry") or row.get("board_name")
               or row.get("board_id") or "") in keys
        or str(row.get("code") or "") in keys
    ]
    bars = [(str(row.get("trade_date")), row.get("close"))
            for row in (industry_bars or []) if isinstance(row, dict)]
    # 资金流按**本板块**过滤（审计阻断 1）：provider 会把全市场 90 行一起传进来，
    # 不过滤就等于每个板块都拿"榜单尾巴三行之和”——线上实测 103 个板块全是
    # 同一个 −212.32。行业按行业名/ts_code 认领；概念不在行业资金表里，
    # 过滤后为空 → 值 None + typed gap（不许拿行业数据冒充概念，K1/开工单二.2）。
    board_flow_rows = _board_flow_rows(flows, board_id, board_name)
    board_flow_rows.sort(key=lambda row: str(row.get("trade_date") or ""))

    # 覆盖率由调用方给（它知道样本是怎么来的）；没给就按"样本齐全"处理，
    # 不擅自猜一个 0 或 1——猜 0 会把整个板块打成全 null，猜 1 会放行空样本。
    effective = coverage
    sufficient = effective is not None and effective >= MIN_COVERAGE

    values: dict[str, Any] = {
        "limit_up_count": len(board_rows),
        "limit_up_2plus_count": sum(1 for row in board_rows if (row.get("board") or 0) >= 2),
        "index_position_vs_ma5": _index_vs_ma5(bars),
        "net_inflow_3d": _net_inflow_3d(board_flow_rows),
        "midcap_above_ma20_pct": midcap_pct,
        "rank_change": rank_change,
    }
    if not sufficient:
        for name in values:
            values[name] = None

    fields = {
        name: {
            "value": (values[name] if sufficient else None),
            "source": FIELDS[name],
            "data_as_of": data_as_of,
            "status": ("ok" if sufficient and values[name] is not None else
                       ("optional_missing" if sufficient else "missing")),
        }
        for name in FIELDS
    }
    gaps: list[dict[str, Any]] = []
    if not sufficient:
        gaps.append({
            "gap_code": ("board_member_coverage_below_floor:"
                         f"{effective}<{MIN_COVERAGE}"),
            "scope": "advisory",
            "severity": "advisory",
            "affected_actions": [],
            "affected_side": "",
            "affected_candidates": [],
            "evidence_time": data_as_of,
            "affected_review_cells": [f"板块 {board_id} 强度"],
            "coverage": effective,
        })
    else:
        # 覆盖足够但字段取不到值：置 null 且**逐字段记 typed gap**
        # （实施计划第 2 节；审计阻断 4——之前 source_gaps 合计 0，
        # 下游看不出这些 null 是"没有生产者"还是"今天没有"）。
        for name in FIELDS:
            if values[name] is not None:
                continue
            gaps.append({
                "gap_code": f"board_field_unavailable:{name}:{FIELD_PRODUCERS[name]}",
                "scope": "advisory",
                "severity": "advisory",
                "affected_actions": [],
                "affected_side": "",
                "affected_candidates": [],
                "evidence_time": data_as_of,
                "affected_review_cells": [f"板块 {board_id} {name}"],
                "board_id": board_id,
            })
    return {"board_id": board_id, "fields": fields, "source_gaps": gaps}


def _board_flow_rows(
    flows: Iterable[Mapping[str, Any]] | None,
    board_id: str,
    board_name: str | None,
) -> list[dict[str, Any]]:
    """从全市场资金流里挑出**本板块**的行（行业名或 ts_code 匹配，.TI 归一）。"""
    keys = {key for key in (board_id, board_name) if key}
    rows = []
    for row in flows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("industry") or row.get("board_name") or "")
        code = str(row.get("ts_code") or row.get("board_id") or "").split(".")[0]
        if name in keys or code in keys:
            rows.append(row)
    return rows


def _index_vs_ma5(bars: list[tuple[str, Any]]) -> float | None:
    if len(bars) < 5:
        return None
    closes = [_number(row[1]) for row in bars[-5:]]
    if any(value is None for value in closes):
        return None
    basis = _mean([value for value in closes if value is not None])
    last = closes[-1]
    if basis in (None, 0) or last is None:
        return None
    return round((last - basis) / basis * 100, 4)


def _net_inflow_3d(flows: list[Mapping[str, Any]]) -> float | None:
    # 历史资金表（moneyflow_cnt/moneyflow_ind_ths）字段名是 net_amount；
    # net_inflow_yi 是实时摘要的字段名，兜底认一下——认不出的字段不静默当 0。
    values = []
    for row in flows[-3:]:
        value = _number(row.get("net_amount"))
        if value is None:
            value = _number(row.get("net_inflow_yi"))
        if value is not None:
            values.append(value)
    if not values:
        return None
    return round(sum(values), 4)


def _number(value: Any) -> float | None:
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


def load_board_definitions(path: str | Path) -> dict[str, Any]:
    """Load the board definitions file; fail closed on a malformed entry."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("board_definitions must be an object")
    taxonomy = payload.get("taxonomy") or {}
    industry = taxonomy.get("industry") or {}
    if str(industry.get("index_prefix") or INDUSTRY_PREFIX) != INDUSTRY_PREFIX:
        raise ValueError("industry taxonomy prefix must be 881")
    boards = payload.get("boards")
    if not isinstance(boards, list):
        raise ValueError("board_definitions.boards must be a list")
    seen: set[str] = set()
    for entry in boards:
        if not isinstance(entry, dict):
            raise ValueError("each board must be an object")
        board_id = str(entry.get("board_id") or "")
        if not board_id:
            raise ValueError("board_id is required")
        kind = str(entry.get("type") or "industry")
        if kind not in {"industry", "concept"}:
            raise ValueError(f"unknown board type: {kind}")
        if kind == "industry":
            if not board_id.startswith(INDUSTRY_PREFIX):
                raise ValueError(
                    f"{board_id} 不是行业板块（前缀应为 {INDUSTRY_PREFIX}）")
        elif not board_id.startswith(CONCEPT_CODE_PREFIXES):
            raise ValueError(
                f"{board_id} 不是概念板块（前缀应为 "
                f"{'/'.join(CONCEPT_CODE_PREFIXES)} 之一）")
        if board_id in seen:
            raise ValueError(f"duplicate board_id: {board_id}")
        seen.add(board_id)
        if not (entry.get("member_source") or {}).get("intent"):
            raise ValueError(f"{board_id} 缺 member_source.intent")
    payload["boards"] = boards
    return payload


__all__ = ["CONCEPT_CODE_PREFIXES", "FIELDS", "INDUSTRY_PREFIX", "MIN_COVERAGE",
           "board_strength", "load_board_definitions"]
