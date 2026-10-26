"""腾讯财经 API — PE/PB/市值/换手率/涨跌停价

数据源: qt.gtimg.cn
鉴权: 无 (仅 User-Agent)
实测: 0.29s, 88字段
风险: 低 (HTTP不限频)
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
import urllib.request

from ..a_share_codes import is_star_market_code


_SHANGHAI = timezone(timedelta(hours=8))


def volume_unit_multiplier(code: str) -> float:
    """腾讯行情成交量字段 → 股 的换算系数（2026-10-26 批次4 / A3）。

    实测（10-26 收盘后多票交叉验证，evidence/BATCH4/a3-unit-verify.json）：

    - 科创板（688/689）：快照字段 v6、分钟累计量、日 K vol 的单位都是**股**，
      系数 1；
    - 沪深主板/创业板：同一字段是**手**，系数 100；
    - 北交所：腾讯这两个接口返回 0/空，不涉及本换算。

    修正前适配器两个方向都错：快照一律 ×100（科创板放大约 100 倍），
    分钟一律不乘（主板/创业板少 100 倍）。
    """

    return 1.0 if is_star_market_code(code) else 100.0


class TencentTransportError(RuntimeError):
    """腾讯 HTTP/传输层失败——与「该日确实没有数据」是两件事。

    2026-10-26 R3 item8a：分钟接口原先把所有异常吞成 `[]`，公共 query 只能报
    `status=empty / EMPTY_UPSTREAM`，与合法空完全不可区分。现在传输失败向上抛本
    异常，由适配器转成 `error`（或 `timeout`）并保留 error_type / HTTP 状态 / 耗时。
    """

    def __init__(self, message: str, *, error_type: str, http_status: int | None = None,
                 elapsed_ms: int = 0) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.http_status = http_status
        self.elapsed_ms = elapsed_ms

    @property
    def error_code(self) -> str:
        if self.http_status is not None:
            return f"HTTP_{self.http_status}"
        if self.error_type in _TIMEOUT_ERROR_TYPES:
            return "TIMEOUT"
        return "TENCENT_TRANSPORT_ERROR"

    @property
    def status(self) -> str:
        return "timeout" if self.error_type in _TIMEOUT_ERROR_TYPES else "provider_error"

    def describe(self) -> str:
        return (f"tencent transport failure: error_type={self.error_type} "
                f"http_status={self.http_status} elapsed_ms={self.elapsed_ms}")


_TIMEOUT_ERROR_TYPES = frozenset({"timeout", "TimeoutError", "socket.timeout"})


def classify_transport_error(exc: BaseException, *, elapsed_ms: int = 0) -> TencentTransportError:
    """把任意 HTTP/传输异常规整成可审计的 TencentTransportError。"""

    error_type = type(exc).__name__
    http_status = getattr(exc, "code", None)
    if isinstance(http_status, bool) or not isinstance(http_status, int):
        http_status = None
    return TencentTransportError(str(exc) or error_type, error_type=error_type,
                                 http_status=http_status, elapsed_ms=int(elapsed_ms))


def _quote_time(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(str(value), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=_SHANGHAI).isoformat(timespec="seconds")


def get_market_prefix(code: str) -> str:
    """6位代码 → 腾讯财经市场前缀"""
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    else:
        return "sz"


def fetch_quotes(codes: list[str]) -> dict:
    """批量拉取腾讯财经实时行情

    Args:
        codes: 6位股票代码列表, 如 ["688017", "300476"]

    Returns:
        {code: {name, price, last_close, change_pct, pe_ttm, pb,
                mcap_yi, float_mcap_yi, limit_up, limit_down,
                turnover_pct, vol_ratio, amplitude_pct, high, low,
                amount_wan, source}}
    """
    if not codes:
        return {}

    prefixed = [f"{get_market_prefix(c)}{c}" for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("Accept", "*/*")

    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode("gbk")

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue

        code = key[2:]  # 去掉 sh/sz 前缀
        price = float(vals[3]) if vals[3] else 0
        last_close = float(vals[4]) if vals[4] else 0
        change_pct = float(vals[32]) if vals[32] else 0

        result[code] = {
            "name": vals[1],
            "price": price,
            "last_close": last_close,
            "open": float(vals[5]) if vals[5] else 0,
            "change_pct": change_pct,
            "change_amt": float(vals[31]) if vals[31] else 0,
            "high": float(vals[33]) if vals[33] else 0,
            "low": float(vals[34]) if vals[34] else 0,
            # Verified Tencent quote positions: vals[6]=volume in lots for
            # 沪深主板/创业板 and in shares for 科创板 (see volume_unit_multiplier),
            # vals[30]=YYYYMMDDHHMMSS, vals[37]=amount in ten-thousand CNY.
            "volume": float(vals[6]) * volume_unit_multiplier(code) if vals[6] else 0,
            "amount": float(vals[37]) * 10000 if vals[37] else 0,
            "quote_time": _quote_time(vals[30]),
            "amount_wan": float(vals[37]) if vals[37] else 0,
            "turnover_pct": float(vals[38]) if vals[38] else 0,
            "pe_ttm": float(vals[39]) if vals[39] else 0,
            "amplitude_pct": float(vals[43]) if vals[43] else 0,
            "mcap_yi": float(vals[44]) if vals[44] else 0,
            "float_mcap_yi": float(vals[45]) if vals[45] else 0,
            "pb": float(vals[46]) if vals[46] else 0,
            "limit_up": float(vals[47]) if vals[47] else 0,
            "limit_down": float(vals[48]) if vals[48] else 0,
            "vol_ratio": float(vals[49]) if vals[49] else 0,
            "pe_static": float(vals[52]) if vals[52] else 0,
            # Level-1 book: vals[9]/[10] bid1 price/lots, vals[19]/[20] ask1.
            "bid_price1": float(vals[9]) if vals[9] else 0,
            "bid_volume1": float(vals[10]) * 100 if vals[10] else 0,
            "ask_price1": float(vals[19]) if vals[19] else 0,
            "ask_volume1": float(vals[20]) * 100 if vals[20] else 0,
            "source": "tencent",
        }

    return result


_INDEX_SYMBOLS = {"000001.SH": "sh000001", "399001.SZ": "sz399001", "399006.SZ": "sz399006"}
_PERIOD_MINUTES = {"5m": 5, "15m": 15, "60m": 60}


def _get_json(url: str) -> dict:
    import json

    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _session_minute(clock: str) -> int | None:
    """Minutes since 09:30 on the trading clock (09:30→0, 11:30→120, 15:00→240)."""
    hour, minute = int(clock[:2]), int(clock[2:4])
    value = hour * 60 + minute
    if 9 * 60 + 30 <= value <= 11 * 60 + 30:
        return value - (9 * 60 + 30)
    if 13 * 60 < value <= 15 * 60:
        return 120 + value - 13 * 60
    return None


def _clock_of(session_minute: int) -> str:
    base = 9 * 60 + 30 + session_minute if session_minute <= 120 else 13 * 60 + session_minute - 120
    return f"{base // 60:02d}:{base % 60:02d}"


def fetch_index_minute_bars(index_code: str, *, period: str = "15m", **_ignored) -> dict:
    """Last five sessions of index bars built from Tencent cumulative minute data.

    Each minute row is ``HHMM price cum_volume(lots) cum_amount(CNY)``; a bar's
    volume and amount are the differences of the cumulative values at its end.
    """
    symbol = _INDEX_SYMBOLS.get(str(index_code).upper())
    size = _PERIOD_MINUTES.get(period)
    if symbol is None or size is None:
        return {"error": "unsupported tencent index or period", "error_type": "INVALID_PARAMS"}
    payload = _get_json(f"https://web.ifzq.gtimg.cn/appstock/app/day/query?code={symbol}")
    days = ((payload.get("data") or {}).get(symbol) or {}).get("data") or []
    bars = []
    for day in days:
        date = str(day.get("date") or "")
        if len(date) != 8:
            continue
        slots: dict[int, dict] = {}
        previous_volume = previous_amount = 0.0
        for raw in day.get("data") or []:
            parts = str(raw).split()
            if len(parts) < 4:
                continue
            minute = _session_minute(parts[0])
            if minute is None:
                continue
            price, cum_volume, cum_amount = float(parts[1]), float(parts[2]), float(parts[3])
            end = max(1, -(-minute // size)) * size
            bar = slots.setdefault(end, {"open": price, "high": price, "low": price,
                                         "start_volume": previous_volume, "start_amount": previous_amount})
            bar.update(close=price, high=max(bar["high"], price), low=min(bar["low"], price),
                       end_volume=cum_volume, end_amount=cum_amount)
            previous_volume, previous_amount = cum_volume, cum_amount
        for end in sorted(slots):
            bar = slots[end]
            bars.append({
                "datetime": f"{date[:4]}-{date[4:6]}-{date[6:]} {_clock_of(end)}",
                "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"],
                "volume": (bar["end_volume"] - bar["start_volume"]) * 100,
                "amount": bar["end_amount"] - bar["start_amount"],
            })
    if not bars:
        return {"error": "tencent index minutes empty", "error_type": "NO_DATA"}
    bars.sort(key=lambda bar: bar["datetime"])
    return {"index_code": index_code, "period": period, "bars": bars, "adjustment": "none",
            "volume_unit": "share", "amount_unit": "CNY", "source": "tencent"}


def fetch_index_intraday_compare(*, period: str = "15m", trade_date: str | None = None) -> dict:
    from .eastmoney_index import build_index_intraday_compare

    return build_index_intraday_compare(
        fetch_index_minute_bars, source="tencent", period=period, trade_date=trade_date,
    )
