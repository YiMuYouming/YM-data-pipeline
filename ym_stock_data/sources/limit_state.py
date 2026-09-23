"""东方财富涨停、炸板、跌停与昨日涨停池旁路。"""

from __future__ import annotations

from datetime import datetime
import re

from .eastmoney_http import CLIENT


POOL_ENDPOINTS = {
    "zt": ("getTopicZTPool", "fbt:asc"),
    "zb": ("getTopicZBPool", "fbt:asc"),
    "dt": ("getTopicDTPool", "fund:asc"),
    "yzt": ("getYesterdayZTPool", "zs:desc"),
}
POOL_TOKEN = "7eea3edcaed734bea9cbfc24409ed989"


def _fetch_pool(kind: str, date: str) -> list[dict]:
    endpoint, sort = POOL_ENDPOINTS[kind]
    response = CLIENT.get(
        f"https://push2ex.eastmoney.com/{endpoint}",
        params={
            "ut": POOL_TOKEN,
            "dpt": "wz.ztzt",
            "Pageindex": 0,
            "pagesize": 10000,
            "sort": sort,
            "date": date,
        },
        headers={"Referer": "https://quote.eastmoney.com/"},
        timeout=10,
    )
    if getattr(response, "skipped_by_breaker", False) is True:
        raise RuntimeError(response.reason)
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("eastmoney limit pool returned invalid payload")
    return (payload.get("data") or {}).get("pool") or []


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_pool(rows: list[dict]) -> list[dict]:
    return [
        {
            "code": str(row.get("c") or ""),
            "name": str(row.get("n") or ""),
            "price": _number(row.get("p")) / 1000,
            "pct": _number(row.get("zdp")),
            "limit_days": int(_number(row.get("lbc"), 1)),
            "seal_fund": _number(row.get("fund")),
            "break_times": int(_number(row.get("zbc"))),
            "industry": str(row.get("hybk") or ""),
        }
        for row in rows
        if isinstance(row, dict)
    ]


def fetch_limit_state(date: str | None = None) -> dict:
    query_date = date or datetime.now().strftime("%Y%m%d")
    try:
        pools = {
            kind: _normalize_pool(_fetch_pool(kind, query_date))
            for kind in POOL_ENDPOINTS
        }
    except Exception as exc:
        return {
            "date": query_date,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "source": "eastmoney_limit_pool",
        }

    zt, zb = pools["zt"], pools["zb"]
    denominator = len(zt) + len(zb)
    return {
        "date": query_date,
        "zt_count": len(zt),
        "zb_count": len(zb),
        "dt_count": len(pools["dt"]),
        "yzt_count": len(pools["yzt"]),
        "break_rate": (
            round(len(zb) / denominator * 100, 2) if denominator else 0.0
        ),
        "max_board": max(
            (int(row.get("limit_days") or 1) for row in zt),
            default=0,
        ),
        "pools": pools,
        "source": "eastmoney_limit_pool",
    }


def derive_limit_promotion(
    previous: dict,
    current: dict,
    *,
    previous_date: str | None = None,
    current_date: str | None = None,
) -> dict:
    """Derive actual next-board seals from two complete, dated stock sets.

    Percentages are 0–100.  A green stock that did not seal its next board is
    never counted as promoted.  The full ladder also includes promotions above
    board four, even though the published tiered rates stop at 3→4.
    """

    def stock_set(snapshot: dict, day: str | None) -> tuple[dict[str, int], int]:
        if not isinstance(snapshot, dict) or snapshot.get("error"):
            raise ValueError("LIMIT_PROMOTION_SOURCE_UNAVAILABLE")
        if day and snapshot.get("date") != day:
            raise ValueError("LIMIT_PROMOTION_DATE_MISMATCH")
        pools = snapshot.get("pools")
        rows = pools.get("zt") if isinstance(pools, dict) else None
        if (
            not isinstance(rows, list)
            or not rows
            or type(snapshot.get("zt_count")) is not int
            or snapshot["zt_count"] != len(rows)
        ):
            raise ValueError("LIMIT_PROMOTION_INCOMPLETE")
        stocks: dict[str, int] = {}
        excluded_st = 0
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("LIMIT_PROMOTION_INCOMPLETE")
            code = str(row.get("code") or "")
            board = row.get("limit_days")
            if not re.fullmatch(r"\d{6}", code) or code in seen or type(board) is not int or board < 1:
                raise ValueError("LIMIT_PROMOTION_INCOMPLETE")
            seen.add(code)
            if "ST" in str(row.get("name") or "").upper():
                excluded_st += 1
                continue
            stocks[code] = board
        if not stocks:
            raise ValueError("LIMIT_PROMOTION_INCOMPLETE")
        return stocks, excluded_st

    previous_stocks, excluded_previous = stock_set(previous, previous_date)
    current_stocks, excluded_current = stock_set(current, current_date)
    promoted = {
        code: (board, current_stocks[code])
        for code, board in previous_stocks.items()
        if current_stocks.get(code) == board + 1
    }

    def rate(previous_board: int | None) -> dict:
        universe = (
            previous_stocks
            if previous_board is None
            else {code: board for code, board in previous_stocks.items() if board == previous_board}
        )
        winners = sorted(code for code in universe if code in promoted)
        return {
            "numerator": len(winners),
            "denominator": len(universe),
            "pct": round(len(winners) / len(universe) * 100, 6) if universe else None,
            "promoted_codes": winners,
        }

    return {
        "previous_date": previous.get("date"),
        "current_date": current.get("date"),
        "previous_non_st_count": len(previous_stocks),
        "current_non_st_count": len(current_stocks),
        "current_consecutive_count": sum(board >= 2 for board in current_stocks.values()),
        "highest_board": max(current_stocks.values()),
        "excluded_st_previous": excluded_previous,
        "excluded_st_current": excluded_current,
        "rates": {
            "one_to_two": rate(1),
            "two_to_three": rate(2),
            "three_to_four": rate(3),
            "overall": rate(None),
        },
        "basis": "previous_complete_non_st_zt_pool_to_current_complete_non_st_zt_pool",
        "source": "eastmoney_limit_pool",
    }


def fetch_limit_promotion(date: str, previous_date: str) -> dict:
    """Read both dates from the same source; return no rate on partial data."""

    previous = fetch_limit_state(date=previous_date)
    if previous.get("error"):
        return {"error": "previous pool unavailable", "error_type": "LIMIT_PROMOTION_SOURCE_UNAVAILABLE"}
    current = fetch_limit_state(date=date)
    if current.get("error"):
        return {"error": "current pool unavailable", "error_type": "LIMIT_PROMOTION_SOURCE_UNAVAILABLE"}
    try:
        promotion = derive_limit_promotion(
            previous, current, previous_date=previous_date, current_date=date
        )
    except ValueError as exc:
        return {"error": "promotion input invalid", "error_type": str(exc)}
    return {**current, "promotion": promotion}
