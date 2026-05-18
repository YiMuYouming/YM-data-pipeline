"""看板数据适配器 — ym_stock_data.fetch() → dashboard_live.json 兼容格式

输出与现有 poll_live.py 格式完全兼容，并追加新字段。

用法:
    from ym_stock_data.consumer.dashboard import build_live
    data = build_live()     # 包含 northbound/hot_list/sector_inflow
    data = build_live(include_extras=False)  # 仅核心字段（与poll_live.py完全相同）
"""

import json
from pathlib import Path
from datetime import datetime

from ..fetch import fetch
from ..sources.pytdx import fetch_quotes as _pytdx_quotes, fetch_breadth, \
    fetch_sector, fetch_kline_15m, _get_api, _format_amount

_OUTPUT = Path(__file__).resolve().parent.parent.parent / "outputs" / "dashboard_live_new.json"
_DASHBOARD_DATA = Path.home() / "Documents/YM_Capital/live-dashboard/data/dashboard_data.json"


def get_stock_codes():
    codes = set()
    try:
        with open(_DASHBOARD_DATA) as f:
            data = json.load(f)
        for key in ("positions", "lianban_pool", "trend_pool"):
            for item in data.get(key, []):
                c = str(item.get("代码", ""))
                if c.isdigit() and len(c) == 6:
                    codes.add(c)
        for a in (data.get("decision", {}).get("锚定股状态") or []):
            c = str(a.get("代码", ""))
            if c.isdigit() and len(c) == 6:
                codes.add(c)
    except Exception:
        pass
    return sorted(codes)


def get_sector_names():
    try:
        with open(_DASHBOARD_DATA) as f:
            data = json.load(f)
        return [s["板块"] for s in data.get("sectors", []) if s.get("板块")]
    except Exception:
        return []


def _yesterday_baseline():
    """昨日收盘基线（与 poll_live.py 一致）"""
    api = _get_api()
    if not api:
        return {}
    idx_list = [(1, "000001", "上证"), (0, "399001", "深证"), (0, "399006", "创业")]
    today_str = datetime.now().strftime("%Y-%m-%d")
    result = {}
    for mkt, code, name in idx_list:
        try:
            bars = api.get_index_bars(9, mkt, code, 0, 4)
            if not bars or len(bars) < 2:
                continue
            yesterday = None; prev = None
            for b in reversed(bars):
                dt = str(b.get("datetime", ""))
                if today_str in dt:
                    continue
                if yesterday is None: yesterday = b
                elif prev is None: prev = b; break
            if not yesterday:
                continue
            close = yesterday.get("close", 0)
            prev_close = prev.get("close", close) if prev else close
            pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0
            result[f"{name}昨收"] = close
            result[f"{name}昨涨幅"] = f"{pct:+.2f}%"
            result[f"{name}昨成交额"] = _format_amount(yesterday.get("amount", 0))
            result[f"{name}昨上涨"] = yesterday.get("up_count", 0)
            result[f"{name}昨下跌"] = yesterday.get("down_count", 0)
        except Exception:
            pass
    return result


def _add_index_derived(live_index: dict, k15: dict):
    """补充量比和成交额差（从15min K线推算）"""
    # 上证量比: 今日累计量 / 昨日同时段累计量
    sh_15 = k15.get("上证15min", [])
    if sh_15:
        cum = [b for b in sh_15 if b.get("_cum")]
        if cum:
            live_index["量比"] = cum[0].get("volRatio", 1.0)

    # 成交额差: 今日累计 - 昨日同时段累计
    today_amt = 0
    yest_amt = 0
    for key in ("上证15min", "深证15min"):
        for b in k15.get(key, []):
            if not b.get("_cum"):
                today_amt += b.get("amount", 0)
                yest_amt += b.get("yesterdayAmt", 0)
    if today_amt > 0 and yest_amt > 0:
        diff = (today_amt - yest_amt) / 1e8
        if abs(diff) < 10000:
            live_index["成交额差"] = f"{diff:+.0f}亿"
        else:
            live_index["成交额差"] = f"{diff/10000:+.2f}万亿"


def build_live(include_extras: bool = True) -> dict:
    codes = get_stock_codes()
    sectors = get_sector_names()
    data = {}

    # --- L2: PyTDX 实时数据 ---
    data["live_quotes"] = _pytdx_quotes(codes)
    r = fetch("index")
    data["live_index"] = {k: v for k, v in r.items() if k != "_meta"}
    data["live_breadth"] = fetch_breadth()
    data["yesterday_baseline"] = _yesterday_baseline()

    # 15min 量价数据
    k15 = fetch_kline_15m()
    for key in ("上证15min", "深证15min", "创业15min"):
        data[key] = k15.get(key, [])

    # 补充量比、成交额差 (从15min累计推算)
    _add_index_derived(data["live_index"], k15)

    if sectors:
        data["live_sectors"] = fetch_sector(sectors)

    # --- L3: 新增数据 ---
    if include_extras:
        try:
            r = fetch("northbound")
            data["northbound"] = {
                "hgt_yi": r.get("hgt_current_yi", 0),
                "sgt_yi": r.get("sgt_current_yi", 0),
                "hgt_trend": r.get("hgt_trend", ""),
                "sgt_trend": r.get("sgt_trend", ""),
                "minutes": r.get("minutes", []),
            }
        except Exception:
            pass

        try:
            r = fetch("ths_hot")
            data["hot_list"] = {
                "total": r.get("total", 0),
                "zt_count": r.get("zt_count", 0),
                "reason_stats": r.get("reason_stats", {}),
                "zt_stocks": r.get("zt_stocks", []),
            }
        except Exception:
            pass

        try:
            r = fetch("sector_inflow", top_n=20)
            data["sector_inflow"] = r.get("top", [])
        except Exception:
            pass

    data["meta"] = {
        "fetched": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "source": "ym_stock_data",
        "stocks_count": len(codes),
        "sectors_count": len(sectors),
    }
    return data


def write_live(path: Path = None):
    out = path or _OUTPUT
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build_live()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"→ {out.name}: {len(data.get('live_quotes',{}))}股 {len(data.get('live_sectors',{}))}板块")


if __name__ == "__main__":
    write_live()
