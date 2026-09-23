"""东方财富个股历史 K 线只读适配器。

`fqt=0` 与 `fqt=1` 分别代表不复权和前复权；两种语义在源边界严格
保留，不把一类结果标记成另一类。成交量由手转换为股，成交额保留元。
"""

from __future__ import annotations

from datetime import datetime
import re

from ..contracts import TZ_SHANGHAI
from .eastmoney_index import get_json_payload


ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
UT = "fa5fd1943c7b386f172d6893dbfba10b"
_PERIOD_KLT = {"daily": 101, "weekly": 102, "monthly": 103}


def _date(value: object) -> str | None:
    text = str(value or "")[:10]
    if re.fullmatch(r"\d{8}", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace("-", "")
    return None


def _number(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _symbol(code: str) -> tuple[int, str] | None:
    normalized = str(code).upper()
    if re.fullmatch(r"\d{6}", normalized):
        normalized = normalized
    if not re.fullmatch(r"\d{6}", normalized):
        return None
    market = 1 if normalized.startswith(("6", "9")) else 0
    return market, normalized


def _parse_bar(row: object) -> dict | None:
    values = row.split(",") if isinstance(row, str) else list(row) if isinstance(row, (list, tuple)) else []
    if len(values) < 7:
        return None
    stamp = str(values[0] or "").replace("T", " ")
    numbers = [_number(values[index]) for index in (1, 2, 3, 4, 5, 6)]
    if not stamp or any(value is None for value in numbers):
        return None
    open_price, close, high, low, raw_volume, amount = numbers
    return {
        "datetime": stamp,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": raw_volume * 100,
        "amount": amount,
    }


def fetch_kline(
    code: str,
    *,
    period: str = "daily",
    count: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    adjustment: str = "none",
) -> dict:
    """Fetch daily/weekly/monthly stock K lines in canonical units."""

    normalized_code = str(code).upper()
    target = _symbol(normalized_code)
    if target is None:
        return {"error": "unsupported stock code", "error_type": "INVALID_PARAMS"}
    klt = _PERIOD_KLT.get(period)
    if klt is None:
        return {
            "error": "EastMoney stock adapter supports long periods only",
            "error_type": "INCOMPATIBLE_PERIOD",
        }
    if adjustment not in {"none", "qfq"}:
        return {"error": "unsupported stock adjustment", "error_type": "INVALID_PARAMS"}

    start = _date(start_date)
    end = _date(end_date)
    limit = count if count is not None else (1_000_000 if start or end else 30)
    params = {
        "secid": f"{target[0]}.{target[1]}",
        "ut": UT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": klt,
        "fqt": 1 if adjustment == "qfq" else 0,
        "beg": start or "0",
        "end": end or "20500101",
        "lmt": limit,
    }
    payload, request_error = get_json_payload(
        ENDPOINT,
        params=params,
        headers={
            "Referer": "https://quote.eastmoney.com/",
            "Connection": "close",
        },
        timeout=15,
    )
    if request_error is not None:
        return request_error

    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("klines") if isinstance(data, dict) else None
    bars = []
    for row in rows if isinstance(rows, list) else []:
        bar = _parse_bar(row)
        if bar is None:
            continue
        stamp_date = _date(bar["datetime"])
        if start and (stamp_date is None or stamp_date < start):
            continue
        if end and (stamp_date is None or stamp_date > end):
            continue
        bars.append(bar)
    bars.sort(key=lambda item: item["datetime"])
    if count is not None:
        bars = bars[-count:]
    if not bars:
        return {
            "error": "eastmoney stock kline returned no rows",
            "error_type": "NO_DATA",
            "source": "eastmoney_stock",
        }
    return {
        "code": normalized_code,
        "period": period,
        "bars": bars,
        "adjustment": adjustment,
        "volume_unit": "share",
        "amount_unit": "CNY",
        "source": "eastmoney_stock",
        "requested_end": end or datetime.now(TZ_SHANGHAI).strftime("%Y%m%d"),
    }
