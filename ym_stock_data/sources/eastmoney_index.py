"""东方财富指数历史 K 线只读适配器。

`push2his` 的指数分钟线成交量以手返回，成交额以元返回；这里在源边界
统一成 canonical contract 的股和元，避免把 PyTDX 指数分钟 ``vol`` 的
非成交量口径带入公共结果。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import re
import time
from typing import Callable

from ..contracts import TZ_SHANGHAI
from .eastmoney_http import CLIENT


ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
UT = "fa5fd1943c7b386f172d6893dbfba10b"
_INDEX_MARKETS = {
    "000001.SH": (1, "000001"),
    "399001.SZ": (0, "399001"),
    "399006.SZ": (0, "399006"),
}
_PERIOD_KLT = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "60m": 60,
    "daily": 101,
    "weekly": 102,
    "monthly": 103,
}
_INDEX_GET_ATTEMPTS = 3
_INDEX_RETRY_BACKOFF_SECONDS = (0.05, 0.1)


def get_json_payload(
    endpoint: str,
    *,
    params: dict,
    headers: dict,
    timeout: float = 15,
    fast: bool = False,
) -> tuple[dict | None, dict | None]:
    """GET one EastMoney JSON payload with the bounded source retry policy."""

    last_error = None
    attempts = 1 if fast else _INDEX_GET_ATTEMPTS
    for attempt in range(attempts):
        try:
            response = CLIENT.get(
                endpoint,
                params=params,
                headers=headers,
                timeout=timeout,
                retry=not fast,
            )
            if getattr(response, "skipped_by_breaker", False) is True:
                return None, {"error": response.reason, "error_type": "BREAKER_OPEN"}
            raise_for_status = getattr(response, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            payload = response.json()
            return payload, None
        except Exception as exc:
            last_error = exc
            if attempt + 1 == attempts:
                return None, {"error": str(exc), "error_type": type(exc).__name__}
            time.sleep(_INDEX_RETRY_BACKOFF_SECONDS[attempt])
    return None, {
        "error": str(last_error or "eastmoney request failed"),
        "error_type": type(last_error).__name__ if last_error else "UPSTREAM",
    }


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


def _default_limit(period: str, *, ranged: bool, count: int | None) -> int:
    if count is not None:
        return count
    if ranged:
        return 1_000_000
    return 30 if period in {"daily", "weekly", "monthly"} else 48


def _parse_bar(row: object) -> dict | None:
    if isinstance(row, str):
        values = row.split(",")
    elif isinstance(row, (list, tuple)):
        values = list(row)
    else:
        return None
    if len(values) < 7:
        return None
    stamp = str(values[0] or "").replace("T", " ")
    if not stamp:
        return None
    open_price, close, high, low = (_number(values[index]) for index in (1, 2, 3, 4))
    raw_volume = _number(values[5])
    amount = _number(values[6])
    if any(value is None for value in (open_price, close, high, low)):
        return None
    return {
        "datetime": stamp,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": raw_volume * 100 if raw_volume is not None else None,
        "amount": amount,
    }


def fetch_index_kline(
    index_code: str,
    *,
    period: str = "daily",
    count: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    _compare_fast: bool = False,
) -> dict:
    """Fetch an index K-line range from Eastmoney's read-only push2his API."""

    normalized_code = str(index_code).upper()
    target = _INDEX_MARKETS.get(normalized_code)
    if target is None:
        return {"error": "unsupported index code", "error_type": "INVALID_PARAMS"}
    klt = _PERIOD_KLT.get(period)
    if klt is None:
        return {"error": "unsupported index period", "error_type": "INVALID_PARAMS"}

    start = _date(start_date)
    end = _date(end_date)
    ranged = start is not None or end is not None
    limit = _default_limit(period, ranged=ranged, count=count)
    now = datetime.now(TZ_SHANGHAI)
    params = {
        "secid": f"{target[0]}.{target[1]}",
        "ut": UT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": klt,
        "fqt": 0,
        "beg": start or "0",
        "end": end or "20500101",
        "lmt": limit,
    }
    request_headers = {
        "Referer": "https://quote.eastmoney.com/",
        "Connection": "close",
    }
    payload, request_error = get_json_payload(
        ENDPOINT,
        params=params,
        headers=request_headers,
        timeout=2.5 if _compare_fast else 15,
        fast=_compare_fast,
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
            "error": "eastmoney index kline returned no rows",
            "error_type": "NO_DATA",
            "source": "eastmoney_index",
        }
    return {
        "index_code": normalized_code,
        "period": period,
        "bars": bars,
        "adjustment": "none",
        "volume_unit": "share",
        "amount_unit": "CNY",
        "source": "eastmoney_index",
        "requested_end": end or now.strftime("%Y%m%d"),
    }


_COMPARE_INDEXES = (
    ("000001.SH", "上证15min"),
    ("399001.SZ", "深证15min"),
    ("399006.SZ", "创业15min"),
)


def _compare_rows(
    bars: list[dict],
    *,
    target_date: str,
    today: bool,
    now: datetime,
) -> list[dict]:
    by_date: dict[str, dict[str, dict]] = {}
    for bar in bars:
        stamp = str(bar.get("datetime") or "")
        stamp_date = _date(stamp)
        if not stamp_date or len(stamp) < 16:
            continue
        by_date.setdefault(stamp_date, {})[stamp[11:16]] = bar
    current = by_date.get(target_date, {})
    previous_dates = sorted(
        date_value for date_value in by_date if date_value < target_date
    )
    previous = by_date.get(previous_dates[-1], {}) if previous_dates else {}
    rows = []
    for slot in sorted(current):
        if today:
            hour, minute = (int(value) for value in slot.split(":")[:2])
            if hour * 60 + minute > now.hour * 60 + now.minute:
                continue
        bar = current[slot]
        prior = previous.get(slot, {})
        volume = float(bar.get("volume") or 0)
        prior_volume = float(prior.get("volume") or 0)
        amount = float(bar.get("amount") or 0)
        prior_amount = float(prior.get("amount") or 0)
        opening = float(bar.get("open") or 0)
        close = float(bar.get("close") or 0)
        rows.append(
            {
                "t": slot,
                "chg": round((close - opening) / opening * 100, 2) if opening else 0,
                "vol": volume,
                "volRatio": round(volume / prior_volume, 2) if prior_volume else 1.0,
                "amount": amount,
                "yesterdayAmt": prior_amount,
            }
        )
    if not rows:
        return []
    total_volume = sum(row["vol"] for row in rows)
    total_prior_volume = sum(
        float(previous.get(row["t"], {}).get("volume") or 0) for row in rows
    )
    total_amount = sum(row["amount"] for row in rows)
    total_prior_amount = sum(row["yesterdayAmt"] for row in rows)
    rows.append(
        {
            "t": "累计",
            "chg": 0,
            "vol": 0,
            "volRatio": round(total_volume / total_prior_volume, 2)
            if total_prior_volume
            else 1.0,
            "amount": total_amount,
            "cumYesterdayAmt": total_prior_amount,
            "_cum": True,
        }
    )
    return rows


def build_index_intraday_compare(
    fetcher: Callable[..., dict],
    *,
    source: str,
    period: str = "15m",
    trade_date: str | None = None,
) -> dict:
    """Build the legacy three-index comparison shape from canonical bars."""

    if period not in {"5m", "15m", "60m"}:
        return {"error": "unsupported index compare period", "error_type": "INVALID_PARAMS"}
    target_date = _date(trade_date) or datetime.now(TZ_SHANGHAI).strftime("%Y%m%d")
    target_dt = datetime.strptime(target_date, "%Y%m%d")
    start_date = (target_dt - timedelta(days=7)).strftime("%Y%m%d")
    now = datetime.now(TZ_SHANGHAI)
    result = {}
    items = []
    for index_code, name in _COMPARE_INDEXES:
        payload = fetcher(
            index_code,
            period=period,
            count=200,
            start_date=start_date,
            end_date=target_date,
        )
        if payload.get("error"):
            break
        rows = _compare_rows(
            payload.get("bars") or [],
            target_date=target_date,
            today=target_date == now.strftime("%Y%m%d"),
            now=now,
        )
        if not rows:
            break
        result[name] = rows
        items.append({"index_code": index_code, "period": period, "bars": rows})
    if len(items) != len(_COMPARE_INDEXES):
        return {
            "error": f"{source} index compare is incomplete",
            "error_type": "INCOMPLETE_INDEX_COMPARE",
        }
    return {
        **result,
        "items": items,
        "trade_date": target_date,
        "period": period,
        "source": source,
    }


def fetch_index_intraday_compare(
    *, period: str = "15m", trade_date: str | None = None
) -> dict:
    """Build the legacy three-index comparison shape from Eastmoney bars."""

    return build_index_intraday_compare(
        lambda code, **kwargs: fetch_index_kline(code, _compare_fast=True, **kwargs),
        source="eastmoney_index",
        period=period,
        trade_date=trade_date,
    )
