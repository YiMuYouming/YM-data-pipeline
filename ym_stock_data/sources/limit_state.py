"""东方财富涨停、炸板、跌停与昨日涨停池旁路。"""

from __future__ import annotations

from datetime import datetime

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
