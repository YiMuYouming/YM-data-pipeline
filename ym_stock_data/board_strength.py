"""逐板块强度（W4 S1，开工单 W4 第二节第 2 点）。

字段全部按 K5 的 `{value, source, data_as_of, status}` 输出。覆盖率不足
（成员样本 < :data:`MIN_COVERAGE`）时数值置 null 并记 typed gap，**不给 0**——
一个凭空捏造的 0 会被下游当成"今天这个板块没有涨停"来用。

概念板块是另一种 `type`，与同花顺二级行业（`881xxx`）分开装载，不混排。
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
    "seal_time_distribution": "market_facts",
    "index_position_vs_ma5": "sector_index",
    "net_inflow_3d": "industry_flow",
    "midcap_above_ma20_pct": "indicators.midcap_above_ma20_pct",
    "rank_change": "sector_index",
}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _seal_distribution(pool: list[dict[str, Any]]) -> dict[str, Any] | None:
    firsts, lasts = [], []
    for row in pool:
        for key, sink in (("first_seal_time", firsts), ("seal_time", lasts)):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                sink.append(value.strip())
    if not firsts and not lasts:
        return None
    return {"earliest": min(firsts) if firsts else None,
            "latest": max(lasts) if lasts else None,
            "buckets": {"morning": sum(1 for t in lasts if t < "12:00"),
                        "afternoon": sum(1 for t in lasts if t >= "12:00")}}


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
    flow_rows = [row for row in (flows or []) if isinstance(row, dict)]

    # 覆盖率由调用方给（它知道样本是怎么来的）；没给就按"样本齐全"处理，
    # 不擅自猜一个 0 或 1——猜 0 会把整个板块打成全 null，猜 1 会放行空样本。
    effective = coverage
    sufficient = effective is not None and effective >= MIN_COVERAGE

    values: dict[str, Any] = {
        "limit_up_count": len(board_rows),
        "limit_up_2plus_count": sum(1 for row in board_rows if (row.get("board") or 0) >= 2),
        "seal_time_distribution": _seal_distribution(board_rows),
        "index_position_vs_ma5": _index_vs_ma5(bars),
        "net_inflow_3d": _net_inflow_3d(flow_rows),
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
    return {"board_id": board_id, "fields": fields, "source_gaps": gaps}


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
    values = [_number(row.get("net_inflow_yi")) for row in flows[-3:]]
    values = [value for value in values if value is not None]
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
