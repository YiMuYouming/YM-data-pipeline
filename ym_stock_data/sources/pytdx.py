"""PyTDX 实时行情 — 个股/指数/K线/板块/涨跌分布

TCP 长连接通达信行情服务器 (7709)，零鉴权，高稳定性。
所有函数共享同一连接池，自动重连。

用法:
    from ym_stock_data.sources import pytdx
    quotes = pytdx.fetch_quotes(["688017", "300476"])
    index = pytdx.fetch_index()
    breadth = pytdx.fetch_breadth()
"""

import json
import os
import time
import threading
import urllib.parse
import urllib.request
from datetime import datetime

from ..config import PYTDX_SERVERS, PYTDX_CONNECT_TIMEOUT, PYTDX_MAX_AGE

# === 连接池（线程安全）===
_api = None
_lock = threading.Lock()
_connected_at = 0
_fail_count = 0
_using_fallback = False

# 均线缓存: {code: {ma5_d, ma10_d, ma20_d, ma10_60m, ma10_60m_dir, _strong}}
_ma_cache = {}
_vol_cache = {}


def _get_api():
    """获取 PyTDX 连接（自动重连，线程安全）"""
    global _api, _connected_at, _fail_count

    if os.getenv("YIMU_DISABLE_PYTDX", "").strip().lower() in {"1", "true", "yes", "on"}:
        return None

    with _lock:
        if _api and (time.time() - _connected_at) < PYTDX_MAX_AGE:
            return _api

        if _api:
            try:
                _api.disconnect()
            except Exception:
                pass

        try:
            from pytdx.hq import TdxHq_API
        except ImportError:
            _fail_count += 1
            return None

        for ip, port in PYTDX_SERVERS:
            try:
                api = TdxHq_API()
                if api.connect(ip, port, time_out=PYTDX_CONNECT_TIMEOUT):
                    _api = api
                    _connected_at = time.time()
                    _fail_count = 0
                    return api
            except Exception:
                continue

        _fail_count += 1
    return None


def disconnect():
    """主动断开连接"""
    global _api
    if _api:
        try:
            _api.disconnect()
        except Exception:
            pass
        _api = None


def to_tdx_code(code: str):
    """6位代码 → PyTDX (market, code) 格式"""
    code = str(code).zfill(6)
    if code.startswith("6") or code.startswith("688"):
        return (1, code)
    elif code.startswith(("0", "3")):
        return (0, code)
    return None


def _format_amount(amt: float) -> str:
    """金额格式化: 元 → 亿"""
    if not amt:
        return "—"
    yi = amt / 1e8
    return f"{yi:.2f}亿" if yi < 10000 else f"{yi/10000:.2f}万亿"


def _number(value, default=0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _eastmoney_json(url: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("Referer", "https://quote.eastmoney.com/")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fallback_index() -> dict:
    """HTTP fallback for cloud nodes where PyTDX TCP is unavailable."""
    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get?"
        + urllib.parse.urlencode({
            "fltt": "2",
            "secids": "1.000001,0.399001,0.399006",
            "fields": "f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f104,f105,f106",
        })
    )
    try:
        payload = _eastmoney_json(url)
    except Exception:
        return {}

    rows = (((payload or {}).get("data") or {}).get("diff") or [])
    name_map = {
        "000001": "上证指数",
        "399001": "深证指数",
        "399006": "创业指数",
    }
    result = {}
    amount_total = 0
    up_total = 0
    down_total = 0

    for row in rows:
        code = str(row.get("f12", ""))
        name = name_map.get(code)
        if not name:
            continue
        price = _number(row.get("f2"))
        pct = _number(row.get("f3"))
        amount = _number(row.get("f6"))
        high = _number(row.get("f15"))
        low = _number(row.get("f16"))
        last_close = _number(row.get("f18"))

        result[name] = round(price, 2) if price else 0
        result[f"{name}涨幅"] = f"{pct:+.2f}%"
        result[f"{name}成交额"] = _format_amount(amount)
        if high and low and last_close:
            result[f"{name}振幅"] = f"{round((high - low) / last_close * 100, 2):.2f}%"
        if code in ("000001", "399001"):
            amount_total += amount
            up_total += int(_number(row.get("f104")))
            down_total += int(_number(row.get("f105")))

    if amount_total:
        result["成交额"] = _format_amount(amount_total)
    if up_total or down_total:
        result["上涨家数"] = up_total
        result["下跌家数"] = down_total
    if result:
        result["_source"] = "eastmoney_fallback"
    return result


def _fallback_breadth() -> dict:
    cats = {"涨停": 0, ">7%": 0, "5~7%": 0, "3~5%": 0, "0~3%": 0,
            "-0~-3%": 0, "-3~-5%": 0, "-5~-7%": 0, "<-7%": 0, "跌停": 0}
    base = "https://push2.eastmoney.com/api/qt/clist/get"
    page = 1
    total = 0
    while page <= 80:
        qs = urllib.parse.urlencode({
            "pn": page,
            "pz": 100,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f2,f3,f18",
        })
        try:
            payload = _eastmoney_json(f"{base}?{qs}")
        except Exception:
            break
        rows = (((payload or {}).get("data") or {}).get("diff") or [])
        if not rows:
            break
        for row in rows:
            pct_raw = row.get("f3")
            if pct_raw in (None, "", "-"):
                continue
            pct = _number(pct_raw)
            total += 1
            if pct >= 9.9:
                cats["涨停"] += 1
            elif pct > 7:
                cats[">7%"] += 1
            elif pct > 5:
                cats["5~7%"] += 1
            elif pct > 3:
                cats["3~5%"] += 1
            elif pct >= 0:
                cats["0~3%"] += 1
            elif pct >= -3:
                cats["-0~-3%"] += 1
            elif pct >= -5:
                cats["-3~-5%"] += 1
            elif pct >= -7:
                cats["-5~-7%"] += 1
            elif pct > -9.9:
                cats["<-7%"] += 1
            else:
                cats["跌停"] += 1
        data_total = int(_number(((payload or {}).get("data") or {}).get("total")))
        if page * 100 >= data_total:
            break
        page += 1

    if total:
        cats["_total"] = total
        cats["_source"] = "eastmoney_fallback"
    return cats


# ==================== 个股报价 ====================


def fetch_quotes(codes: list) -> dict:
    """批量个股实时报价

    Returns:
        {code: {最新价, 涨幅, 量比, 换手, MA5_d, MA10_d, MA20_d,
                MA10_60m, MA10_60m_dir, is_strong}, ...}
    """
    global _fail_count

    api = _get_api()
    if not api:
        return _fallback_quotes(codes)

    tdx_codes = []
    code_map = {}
    for c in codes:
        tdx = to_tdx_code(c)
        if tdx:
            tdx_codes.append(tdx)
            code_map[c] = tdx

    if not tdx_codes:
        return {}

    try:
        raw = api.get_security_quotes(tdx_codes)
        if not raw:
            return _fallback_quotes(codes)
    except Exception:
        with _lock:
            _fail_count += 1
        return _fallback_quotes(codes)

    with _lock:
        _fail_count = 0

    now = datetime.now()
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    minutes_traded = max(1, min(240, (now - market_open).total_seconds() / 60))

    result = {}
    for row in raw:
        code = row.get("code", "")
        if not code:
            continue

        price = row.get("price", 0)
        last_close = row.get("last_close", 1)
        pct_chg = round((price - last_close) / last_close * 100, 2) if last_close else 0
        vol = row.get("vol", 0)

        vol_ratio = _compute_vol_ratio(api, code, vol, minutes_traded)

        mas = _get_mas(api, code, price)

        result[code] = {
            "最新价": price,
            "涨幅": f"{pct_chg:+.2f}%",
            "量比": f"{vol_ratio:.2f}" if vol_ratio else "—",
            "换手": "—",  # TDX Level-1 换手率不可靠
            "MA5_d": mas.get("ma5_d"),
            "MA10_d": mas.get("ma10_d"),
            "MA20_d": mas.get("ma20_d"),
            "MA10_60m": mas.get("ma10_60m"),
            "MA10_60m_dir": mas.get("ma10_60m_dir", "—"),
            "is_strong": mas.get("_strong", False),
        }

    return result


def _compute_vol_ratio(api, code, current_vol, minutes_traded):
    """量比 = 当前量 / (近5日均量 / 240 * 已交易分钟数)"""
    global _vol_cache
    if current_vol <= 0:
        return None

    cache_key = str(code)
    if cache_key not in _vol_cache:
        try:
            mkt = 1 if str(code).startswith(("6", "688")) else 0
            bars = api.get_security_bars(9, mkt, str(code), 0, 5)
            if bars and len(bars) >= 3:
                avg_vol = sum(b.get("vol", 0) for b in bars) / len(bars)
                _vol_cache[cache_key] = avg_vol
            else:
                return None
        except Exception:
            return None

    avg_vol = _vol_cache.get(cache_key)
    if not avg_vol or avg_vol <= 0:
        return None

    expected = avg_vol * minutes_traded / 240
    if expected <= 0:
        return None
    return round(current_vol / expected, 2)


def _get_mas(api, code, current_price):
    """均线: 日线 MA5/MA10/MA20 + 60分钟 MA10 + 强势标记"""
    global _ma_cache
    cache_key = str(code)
    if cache_key in _ma_cache:
        return _ma_cache[cache_key]

    mas = {}
    mkt = 1 if str(code).startswith(("6", "688")) else 0

    try:
        bars_d = api.get_security_bars(9, mkt, str(code), 0, 25)
        if bars_d:
            closes = [b.get("close", 0) for b in bars_d if b.get("close", 0) > 0]
            for n, key in [(5, "ma5_d"), (10, "ma10_d"), (20, "ma20_d")]:
                if len(closes) >= n:
                    mas[key] = round(sum(closes[-n:]) / n, 2)

            # 强势趋势股: 近5日收盘从未跌破MA5
            if len(closes) >= 10 and mas.get("ma5_d"):
                ma5 = mas["ma5_d"]
                recent = closes[-5:]
                below_ma5 = sum(1 for c in recent if c < ma5)
                mas["_strong"] = below_ma5 == 0
    except Exception:
        pass

    # 60分钟 MA10
    try:
        bars_60m = api.get_security_bars(3, mkt, str(code), 0, 15)
        if bars_60m:
            closes_60m = [b.get("close", 0) for b in bars_60m[-12:] if b.get("close", 0) > 0]
            if len(closes_60m) >= 8:
                n = min(10, len(closes_60m))
                mas["ma10_60m"] = round(sum(closes_60m[-n:]) / n, 2)

                half = len(closes_60m) // 2
                prev_avg = sum(closes_60m[:half]) / half
                later_avg = sum(closes_60m[half:]) / (len(closes_60m) - half)
                if later_avg > prev_avg * 1.005:
                    mas["ma10_60m_dir"] = "向上"
                elif later_avg < prev_avg * 0.995:
                    mas["ma10_60m_dir"] = "向下"
                else:
                    mas["ma10_60m_dir"] = "走平"
    except Exception:
        pass

    if mas:
        _ma_cache[cache_key] = mas
    return mas


def _fallback_quotes(codes):
    """HTTP quote fallback for environments where PyTDX TCP is unavailable."""
    try:
        from . import tencent
        all_data = tencent.fetch_quotes(codes)
        result = {}
        for code in codes:
            d = all_data.get(code, {})
            if d:
                price = d.get("price", 0)
                change_pct = d.get("change_pct")
                turnover_pct = d.get("turnover_pct")
                vol_ratio = d.get("vol_ratio")
                result[code] = {
                    "最新价": price,
                    "涨幅": f"{change_pct:+.2f}%" if isinstance(change_pct, (int, float)) else "—",
                    "量比": f"{vol_ratio:.2f}" if isinstance(vol_ratio, (int, float)) else "—",
                    "换手": f"{turnover_pct:.2f}" if isinstance(turnover_pct, (int, float)) else "—",
                    "MA5_d": None,
                    "MA10_d": None,
                    "MA20_d": None,
                    "MA10_60m": None,
                    "MA10_60m_dir": "—",
                    "is_strong": False,
                    "_source": "tencent_fallback",
                }
        if result:
            return result
    except Exception:
        pass

    try:
        from easyquotation import use
        eq = use("sina")
        all_data = eq.stocks(codes)
        result = {}
        for code in codes:
            d = all_data.get(code, {})
            if d:
                result[code] = {
                    "最新价": d.get("now", d.get("price", 0)),
                    "涨幅": d.get("涨跌(%)", "—"),
                    "量比": d.get("量比", "—"),
                    "换手": d.get("换手(%)", "—"),
                }
        return result
    except Exception:
        return {}


# ==================== 三大指数 ====================


def fetch_index() -> dict:
    """三大指数实时行情

    Returns:
        {上证指数, 上证指数涨幅, 上证指数成交额, 上证指数振幅,
         深证指数/深证指数涨幅/..., 创业指数/...,
         成交额, 成交额差, 上涨家数, 下跌家数, 量比}
    """
    api = _get_api()
    if not api:
        return _fallback_index()

    idx_map = {
        "000001": "上证指数", "399001": "深证指数", "399006": "创业指数",
    }
    idx_codes = [(1, "000001"), (0, "399001"), (0, "399006")]

    try:
        raw = api.get_security_quotes(idx_codes)
        if not raw:
            return {}
    except Exception:
        return {}

    result = {}
    amount_total = 0
    for row in raw:
        code = row.get("code", "")
        name = idx_map.get(code)
        if not name:
            continue

        price = row.get("price", 0)
        last_close = row.get("last_close", 1)
        pct = round((price - last_close) / last_close * 100, 2) if last_close else 0
        amount = row.get("amount", 0)
        high = row.get("high", 0)
        low = row.get("low", 0)

        result[name] = price
        result[f"{name}涨幅"] = f"{pct:+.2f}%"
        result[f"{name}成交额"] = _format_amount(amount)
        if high and low and last_close:
            result[f"{name}振幅"] = f"{round((high-low)/last_close*100,2):.2f}%"

        if code in ("000001", "399001"):
            amount_total += amount

    result["成交额"] = _format_amount(amount_total)

    # 涨跌家数
    ud = _get_up_down(api)
    if ud:
        result["上涨家数"] = ud[0]
        result["下跌家数"] = ud[1]

    return result


def _get_up_down(api):
    """沪深合计涨跌家数"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    total_up, total_dn = 0, 0
    for mkt, code in [(1, "000001"), (0, "399001")]:
        try:
            bars = api.get_index_bars(1, mkt, code, 0, 2)
            if not bars:
                continue
            for b in reversed(bars):
                if today_str in b.get("datetime", ""):
                    total_up += b.get("up_count", 0)
                    total_dn += b.get("down_count", 0)
                    break
        except Exception:
            pass
    return (total_up, total_dn) if (total_up or total_dn) else None


# ==================== 全市场涨跌分布 ====================


def fetch_breadth() -> dict:
    """全市场涨跌分布 (~5000只)

    Returns:
        {涨停: N, >7%: N, 5~7%: N, 3~5%: N, 0~3%: N,
         -0~-3%: N, -3~-5%: N, -5~-7%: N, <-7%: N, 跌停: N, _total: N}
    """
    api = _get_api()
    if not api:
        return _fallback_breadth()

    codes = _all_share_codes()
    batch_size = 200
    cats = {"涨停": 0, ">7%": 0, "5~7%": 0, "3~5%": 0, "0~3%": 0,
            "-0~-3%": 0, "-3~-5%": 0, "-5~-7%": 0, "<-7%": 0, "跌停": 0}
    total = 0
    errors = 0

    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        try:
            raw = api.get_security_quotes(batch)
        except Exception:
            errors += 1
            if errors > 5:
                break
            continue
        errors = 0
        if not raw:
            continue
        for r in raw:
            price = r.get("price", 0)
            last_close = r.get("last_close", 0)
            if not price or not last_close:
                continue
            pct = round((price - last_close) / last_close * 100, 2)
            total += 1
            if pct >= 9.9:
                cats["涨停"] += 1
            elif pct > 7:
                cats[">7%"] += 1
            elif pct > 5:
                cats["5~7%"] += 1
            elif pct > 3:
                cats["3~5%"] += 1
            elif pct >= 0:
                cats["0~3%"] += 1
            elif pct >= -3:
                cats["-0~-3%"] += 1
            elif pct >= -5:
                cats["-3~-5%"] += 1
            elif pct >= -7:
                cats["-5~-7%"] += 1
            elif pct > -9.9:
                cats["<-7%"] += 1
            else:
                cats["跌停"] += 1

    if total:
        cats["_total"] = total
    return cats


_all_codes_cache = None


def _all_share_codes():
    global _all_codes_cache
    if _all_codes_cache:
        return _all_codes_cache
    codes = []
    for prefix, start, end in [("60", 600000, 606000), ("688", 688000, 689000)]:
        for i in range(start, end):
            codes.append((1, str(i)))
    for i in range(1, 4000):
        codes.append((0, f"{i:06d}"))
    for i in range(300000, 302000):
        codes.append((0, str(i)))
    _all_codes_cache = codes
    return codes


# ==================== K线 ====================


def fetch_kline(code: str, period: str = "daily") -> dict:
    """K线+均线

    Args:
        code: 6位股票代码
        period: daily / 60m / 15m

    Returns:
        {code, name, closes, mas:{}, bars:[{time,open,high,low,close,vol}, ...]}
    """
    api = _get_api()
    if not api:
        return {"code": code, "error": "连接失败"}

    _PERIOD_MAP = {"daily": 9, "weekly": 5, "monthly": 6, "60m": 3, "15m": 1, "5m": 0}
    bar_type = _PERIOD_MAP.get(period, 9)
    count = 30 if period in ("daily", "60m") else 48

    mkt = 1 if str(code).startswith(("6", "688")) else 0
    try:
        bars = api.get_security_bars(bar_type, mkt, str(code), 0, count)
        if not bars:
            return {"code": code, "error": "无数据"}
    except Exception as e:
        return {"code": code, "error": str(e)}

    closes = [b.get("close", 0) for b in bars if b.get("close", 0) > 0]
    mas = {}
    for n, key in [(5, "MA5"), (10, "MA10"), (20, "MA20")]:
        if len(closes) >= n:
            mas[key] = round(sum(closes[-n:]) / n, 2)

    bar_list = []
    for b in bars:
        bar_list.append({
            "time": str(b.get("datetime", "")),
            "open": b.get("open", 0),
            "high": b.get("high", 0),
            "low": b.get("low", 0),
            "close": b.get("close", 0),
            "vol": b.get("vol", 0),
            "amount": b.get("amount", 0),
        })

    return {
        "code": code,
        "total_bars": len(bars),
        "last_close": closes[-1] if closes else 0,
        "mas": mas,
        "bars": bar_list,
    }


def fetch_kline_15m() -> dict:
    """三大指数15分钟量价（同比昨日）

    Returns:
        {上证15min: [{t,chg,vol,volRatio,amount,yesterdayAmt}, ...],
         深证15min: [...], 创业15min: [...]}
    """
    api = _get_api()
    if not api:
        return {}

    indexes = {"上证15min": (1, "000001"), "深证15min": (0, "399001"), "创业15min": (0, "399006")}
    today_str = datetime.now().strftime("%Y-%m-%d")
    result = {}

    for name, (mkt, code) in indexes.items():
        try:
            bars = api.get_index_bars(1, mkt, code, 0, 60)
            if not bars:
                continue
        except Exception:
            continue

        # 今日和昨日数据分离
        today_bars, yesterday_bars = {}, {}
        for b in bars:
            dt = str(b.get("datetime", ""))
            if today_str in dt:
                time_key = dt.split(" ")[-1][:5] if " " in dt else dt[-5:]
                today_bars[time_key] = b
            elif dt:
                time_key = dt.split(" ")[-1][:5] if " " in dt else dt[-5:]
                yesterday_bars[time_key] = {"vol": b.get("vol", 0), "amount": b.get("amount", 0)}

        now = datetime.now()
        current_min = now.hour * 60 + now.minute
        slots = []

        for time_key in sorted(today_bars.keys()):
            parts = time_key.split(":")
            slot_end = int(parts[0]) * 60 + int(parts[1])
            if current_min < slot_end:
                continue
            b = today_bars[time_key]
            open_p = b.get("open", 0)
            close_p = b.get("close", 0)
            vol = b.get("vol", 0)
            chg = round((close_p - open_p) / open_p * 100, 2) if open_p else 0
            ydata = yesterday_bars.get(time_key, {})
            yvol = ydata.get("vol", vol) if isinstance(ydata, dict) else vol
            yamt = ydata.get("amount", 0) if isinstance(ydata, dict) else 0
            vol_ratio = round(vol / yvol, 2) if yvol > 0 else 1.0

            slots.append({
                "t": time_key,
                "chg": chg,
                "vol": vol,
                "volRatio": vol_ratio,
                "amount": b.get("amount", 0),
                "yesterdayAmt": yamt,
            })

        # 累计汇总
        if slots:
            total_vol = sum(s["vol"] for s in slots)
            total_yvol = sum(yesterday_bars.get(s["t"], {}).get("vol", s["vol"])
                             for s in slots)
            cum_ratio = round(total_vol / total_yvol, 2) if total_yvol > 0 else 1.0
            cum_amt = sum(s["amount"] for s in slots)
            cum_yamt = sum(yesterday_bars.get(s["t"], {}).get("amount", 0)
                          for s in slots)
            slots.append({
                "t": "累计", "chg": 0, "vol": 0,
                "volRatio": cum_ratio, "amount": cum_amt,
                "cumYesterdayAmt": round(cum_yamt), "_cum": True,
            })

        result[name] = slots

    return result


# ==================== 板块指数 ====================


# 复盘笔记板块名 → TDX 板块指数代码
_TDX_SECTOR_MAP = {
    "算力": "880565", "算力租赁": "880565",
    "光通信": "880619", "CPO/光通信": "880619",
    "半导体": "880491", "存储芯片": "880589",
    "PCB": "880542",
    "机器人": "880905",
    "电力": "880582",
    "锂电": "880534",
    "光伏": "880544",
    "风电": "880543",
    "航天/军工": "880490", "国防军工": "880490",
    "人工智能": "880569",
    "液冷": "880570",
    "低空经济": "880905",
    "化工": "880324",
    "医药": "880400",
    "大消费": "880375",
    "房地产": "880482",
    "汽车零部件": "880452",
}


def fetch_sector(names: list) -> dict:
    """板块指数实时行情

    Args:
        names: 板块名称列表, 如 ["算力", "CPO/光通信"]

    Returns:
        {名称: {涨跌幅, 最新价, MA5, MA20, MA5方向, 站上MA5, 距MA5, 成交额趋势}}
    """
    api = _get_api()
    if not api:
        return {}

    code_to_name = {}
    tdx_codes = []
    for name in names:
        tdx_code = _resolve_sector_code(name)
        if tdx_code:
            tdx_codes.append((1, tdx_code))
            code_to_name[tdx_code] = name

    if not tdx_codes:
        return {}

    try:
        raw = api.get_security_quotes(tdx_codes)
        if not raw:
            return {}
    except Exception:
        return {}

    result = {}
    for row in raw:
        code = row.get("code", "")
        name = code_to_name.get(code)
        if not name:
            continue

        price = row.get("price", 0)
        last_close = row.get("last_close", 1)
        pct = round((price - last_close) / last_close * 100, 2) if last_close else 0

        ma_info = _get_sector_mas(api, code, price)

        dist_ma5 = None
        if ma_info.get("ma5") and price:
            dist_ma5 = round((price - ma_info["ma5"]) / ma_info["ma5"] * 100, 2)

        result[name] = {
            "涨跌幅": pct,
            "最新价": price,
            "MA5": ma_info.get("ma5"),
            "MA20": ma_info.get("ma20"),
            "MA5方向": ma_info.get("ma5_dir", "—"),
            "站上MA5": ma_info.get("vs_ma5", "—"),
            "距MA5": dist_ma5,
            "成交额趋势": ma_info.get("amt_trend", "—"),
        }

    return result


_sector_ma_cache = {}


def _get_sector_mas(api, code, price):
    """板块指数均线"""
    global _sector_ma_cache
    cache_key = str(code)
    if cache_key in _sector_ma_cache:
        return _sector_ma_cache[cache_key]

    info = {}
    try:
        bars = api.get_security_bars(9, 1, str(code), 0, 30)
        if bars and len(bars) >= 3:
            closes = [b.get("close", 0) for b in bars if b.get("close", 0) > 0]
            amounts = [b.get("amount", 0) for b in bars if b.get("amount", 0) > 0]

            if len(closes) >= 3:
                n = min(5, len(closes))
                info["ma5"] = round(sum(closes[-n:]) / n, 2)
                half = len(closes) // 2
                recent = closes[-half:] if half else closes
                earlier = closes[:half] if half else closes[:1]
                if sum(recent)/len(recent) > sum(earlier)/len(earlier) * 1.005:
                    info["ma5_dir"] = "向上"
                elif sum(recent)/len(recent) < sum(earlier)/len(earlier) * 0.995:
                    info["ma5_dir"] = "向下"
                else:
                    info["ma5_dir"] = "走平"
                info["vs_ma5"] = "站上" if price > info["ma5"] else "跌破"

            if len(closes) >= 20:
                info["ma20"] = round(sum(closes[-20:]) / 20, 2)
            if len(amounts) >= 3:
                n_amt = min(5, len(amounts))
                ma5_amt = sum(amounts[-n_amt:]) / n_amt
                today_amt = amounts[-1] if amounts else 0
                if today_amt > ma5_amt * 1.15:
                    info["amt_trend"] = "放量"
                elif today_amt < ma5_amt * 0.85:
                    info["amt_trend"] = "缩量"
                else:
                    info["amt_trend"] = "持平"
    except Exception:
        pass

    if info:
        _sector_ma_cache[cache_key] = info
    return info


def _resolve_sector_code(name):
    """板块名称 → TDX 代码"""
    if name in _TDX_SECTOR_MAP:
        return _TDX_SECTOR_MAP[name]
    for key, code in _TDX_SECTOR_MAP.items():
        if key in name or name in key:
            return code
    return None
