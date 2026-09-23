"""新浪指数分钟 K 线只读适配器。

新浪指数分钟接口直接返回股和元；这里仅承接 1m/5m/15m/60m，作为
EastMoney 指数分钟线失败后的 canonical fallback，不承接日/周/月。
"""

from __future__ import annotations

from datetime import datetime
import json
import re
import urllib.parse
import urllib.request

from ..contracts import TZ_SHANGHAI
from .eastmoney_index import build_index_intraday_compare


ENDPOINT = (
    "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_k=/"
    "CN_MarketDataService.getKLineData"
)
_INDEX_SYMBOLS = {
    "000001.SH": "sh000001",
    "399001.SZ": "sz399001",
    "399006.SZ": "sz399006",
}
_PERIOD_SCALE = {"1m": 1, "5m": 5, "15m": 15, "60m": 60}


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


def _stamp(value: object) -> str:
    text = str(value or "").replace("T", " ").strip()
    if len(text) == 10:
        return f"{text} 00:00:00"
    if len(text) == 16:
        return f"{text}:00"
    return text


def _datalen(*, count: int | None, ranged: bool) -> int:
    if count is not None:
        return max(1, int(count))
    return 200 if ranged else 48


def fetch_index_kline(
    index_code: str,
    *,
    period: str = "15m",
    count: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    _compare_fast: bool = False,
) -> dict:
    """Fetch Sina index minute bars in canonical share/CNY units."""

    normalized_code = str(index_code).upper()
    symbol = _INDEX_SYMBOLS.get(normalized_code)
    if symbol is None:
        return {"error": "unsupported index code", "error_type": "INVALID_PARAMS"}
    scale = _PERIOD_SCALE.get(period)
    if scale is None:
        return {
            "error": "Sina index adapter supports minute periods only",
            "error_type": "INCOMPATIBLE_PERIOD",
        }

    start = _date(start_date)
    end = _date(end_date)
    params = {
        "symbol": symbol,
        "scale": str(scale),
        "ma": "no",
        "datalen": str(_datalen(count=count, ranged=start is not None or end is not None)),
    }
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
            "Connection": "close",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=2.5 if _compare_fast else 10) as response:
            text = response.read().decode("utf-8", "replace")
        match = re.search(r"\((\[.*\])\)\s*;?\s*$", text, re.DOTALL)
        rows = json.loads(match.group(1)) if match else []
    except Exception as exc:
        return {"error": str(exc), "error_type": type(exc).__name__}

    bars = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        stamp = _stamp(row.get("day") or row.get("datetime") or row.get("time"))
        open_price = _number(row.get("open"))
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        close = _number(row.get("close"))
        volume = _number(row.get("volume"))
        amount = _number(row.get("amount"))
        if not stamp or any(
            value is None
            for value in (open_price, high, low, close, volume, amount)
        ):
            continue
        stamp_date = _date(stamp)
        if start and (stamp_date is None or stamp_date < start):
            continue
        if end and (stamp_date is None or stamp_date > end):
            continue
        bars.append(
            {
                "datetime": stamp,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
            }
        )
    bars.sort(key=lambda item: item["datetime"])
    if count is not None:
        bars = bars[-count:]
    if not bars:
        return {
            "error": "sina index kline returned no rows",
            "error_type": "NO_DATA",
            "source": "sina_index",
        }
    return {
        "index_code": normalized_code,
        "period": period,
        "bars": bars,
        "adjustment": "none",
        "volume_unit": "share",
        "amount_unit": "CNY",
        "source": "sina_index",
        "requested_end": end or datetime.now(TZ_SHANGHAI).strftime("%Y%m%d"),
    }


def fetch_index_intraday_compare(
    *, period: str = "15m", trade_date: str | None = None
) -> dict:
    """Build the shared legacy comparison shape from Sina bars."""

    return build_index_intraday_compare(
        lambda code, **kwargs: fetch_index_kline(code, _compare_fast=True, **kwargs),
        source="sina_index",
        period=period,
        trade_date=trade_date,
    )
