"""同花顺北向资金 — hsgtApi 实时分钟流向 + 本地自缓存历史

数据源: data.hexin.cn/hsgtApi
鉴权: 无 (仅 User-Agent)
实测: 262分钟点, 实时
风险: 极低 (零鉴权)

注意: 东财全系北向数据自 2024-08 断供, 本模块使用同花顺独立源.
      V2.1 新增本地 CSV 自缓存, 每次拉实时后自动存收盘数据.
"""

from datetime import datetime, date as _date
from pathlib import Path
from typing import Optional
import pandas as pd
import requests

_HSGT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36",
    "Host": "data.hexin.cn",
    "Referer": "https://data.hexin.cn/",
}


def fetch_realtime() -> dict:
    """沪深股通当日实时分钟流向

    Returns:
        {date, minute_count,
         hgt_current_yi: float 沪股通最新累计净买入(亿元),
         sgt_current_yi: float 深股通最新累计净买入(亿元),
         hgt_trend: "净流入"/"净流出"(>0/<0),
         sgt_trend: "净流入"/"净流出",
         minutes: [{time, hgt_yi, sgt_yi}, ...] 分钟级序列,
         source: "northbound_hsgt"}
    """
    url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
    resp = requests.get(url, headers=_HSGT_HEADERS, timeout=10)
    resp.raise_for_status()
    d = resp.json()

    times = d.get("time", [])
    hgt = d.get("hgt", [])
    sgt = d.get("sgt", [])

    n = len(times)
    hgt_padded = hgt[:n] + [None] * (n - len(hgt))
    sgt_padded = sgt[:n] + [None] * (n - len(sgt))

    hgt_current = None
    sgt_current = None
    minutes_data = []

    for i in range(n):
        hv = hgt_padded[i]
        sv = sgt_padded[i]
        if hv is not None:
            hgt_current = hv
        if sv is not None:
            sgt_current = sv
        minutes_data.append({
            "time": times[i],
            "hgt_yi": hv,
            "sgt_yi": sv,
        })

    return {
        "date": _date.today().strftime("%Y-%m-%d"),
        "minute_count": len(times),
        "hgt_current_yi": hgt_current if hgt_current is not None else 0,
        "sgt_current_yi": sgt_current if sgt_current is not None else 0,
        "hgt_trend": "净流入" if (hgt_current or 0) > 0 else "净流出",
        "sgt_trend": "净流入" if (sgt_current or 0) > 0 else "净流出",
        "minutes": minutes_data,
        "source": "northbound_hsgt",
    }


def _cache_path() -> Path:
    """北向资金本地 CSV 缓存路径"""
    p = Path.home() / ".ym-data-pipeline" / "cache" / "northbound_daily.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def save_snapshot(date_str: str, hgt: float, sgt: float):
    """写入/更新当天北向收盘数据到 CSV"""
    path = _cache_path()
    rows = {}
    if path.exists():
        for line in path.read_text().strip().split("\n")[1:]:
            parts = line.split(",")
            if len(parts) == 3:
                rows[parts[0]] = line
    rows[date_str] = f"{date_str},{hgt},{sgt}"
    with open(path, "w") as f:
        f.write("date,hgt,sgt\n")
        for d in sorted(rows.keys()):
            f.write(rows[d] + "\n")


def load_history(n: int = 20) -> pd.DataFrame:
    """读取最近 N 天北向历史"""
    path = _cache_path()
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    return df.tail(n)


def fetch_with_auto_cache() -> dict:
    """拉取实时数据 + 自动缓存今日收盘"""
    result = fetch_realtime()
    minutes = result.get("minutes", [])
    # 找最后一个非空数据点
    last_valid = None
    for m in reversed(minutes):
        if m["hgt_yi"] is not None and m["sgt_yi"] is not None:
            last_valid = m
            break
    if last_valid and result["minute_count"] > 200:
        save_snapshot(
            result["date"],
            last_valid["hgt_yi"],
            last_valid["sgt_yi"],
        )
        result["cached"] = True
    else:
        result["cached"] = False
    return result
