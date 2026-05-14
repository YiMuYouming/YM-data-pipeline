"""腾讯财经 API — PE/PB/市值/换手率/涨跌停价

数据源: qt.gtimg.cn
鉴权: 无 (仅 User-Agent)
实测: 0.29s, 88字段
风险: 低 (HTTP不限频)
"""

from typing import Optional
import urllib.request


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
            "source": "tencent",
        }

    return result
