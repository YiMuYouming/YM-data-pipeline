#!/usr/bin/env python3
"""ym-stock-data 对比验证脚本

同时跑新系统(ym_stock_data)和老系统(poll_live/pipeline)，逐项对照输出。
"""

import sys, os, json, time
from pathlib import Path
from datetime import datetime

# === 路径 ===
_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))

_DASHBOARD_DIR = Path.home() / "Documents/YouMingVault/10_⚡Now/01_💰弈沐资本/live-dashboard"
_DASHBOARD_DATA = _DASHBOARD_DIR / "data/dashboard_data.json"
_PIPELINE_DIR = Path.home() / "WorkBuddy/Tools/ym_data_pipeline"
sys.path.insert(0, str(_PIPELINE_DIR))

from ym_stock_data import fetch

PASS, FAIL, WARN = "✅", "❌", "⚠️"


def log(msg):
    print(f"  {msg}")


def get_stock_codes():
    """从 dashboard_data.json 提取代码（与 poll_live.py 一致）"""
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
    return sorted(codes)[:10]  # 只对比前10只，更快


def compare_tdx_quotes():
    """对比 PyTDX 个股报价"""
    print("\n--- L2: PyTDX 个股报价 ---")
    codes = get_stock_codes()
    if not codes:
        print(f"  {FAIL} 没有找到股票代码（检查 dashboard_data.json）")
        return

    # 新系统
    t0 = time.time()
    new = fetch("quotes", codes=codes)
    t_new = time.time() - t0

    # 老系统：直接调用 PyTDX
    from pytdx.hq import TdxHq_API
    servers = [("110.41.147.114", 7709)]
    tdx_codes = []
    code_map = {}
    for c in codes:
        c = str(c).zfill(6)
        if c.startswith(("6", "688")):
            tdx_codes.append((1, c))
            code_map[c] = 1
        elif c.startswith(("0", "3")):
            tdx_codes.append((0, c))
            code_map[c] = 0

    t0 = time.time()
    api = TdxHq_API()
    if not api.connect("110.41.147.114", 7709):
        print(f"  {FAIL} 老系统 PyTDX 连接失败")
        return
    raw = api.get_security_quotes(tdx_codes)
    api.disconnect()
    t_old = time.time() - t0

    if not raw:
        print(f"  {FAIL} 老系统返回空数据")
        return

    print(f"  {PASS} 新系统: {t_new:.2f}s, 老系统: {t_old:.2f}s")
    print(f"  {PASS} 新系统返回 {len(new)-1} 只, 老系统返回 {len(raw)} 只")

    # 逐字段对比
    mismatches = 0
    for r in raw:
        code = r.get("code", "")
        if code not in code_map:
            continue
        new_q = new.get(code, {})
        if not new_q:
            print(f"  {WARN} {code} 在老系统有但新系统无")
            mismatches += 1
            continue

        # 对比价格
        old_price = r.get("price", 0)
        new_price = new_q.get("最新价", 0)
        if old_price and new_price:
            diff = abs(old_price - new_price)
            if diff > 0.05:  # 允许0.05误差（TCP vs HTTP获取时间差）
                print(f"  {FAIL} {code} 价格: 老={old_price} 新={new_price} 差={diff}")
                mismatches += 1

        # 对比涨幅
        old_close = r.get("last_close", 1)
        if old_close:
            old_pct = round((old_price - old_close) / old_close * 100, 2)
            new_pct_str = new_q.get("涨幅", "0%")
            new_pct = float(new_pct_str.replace("%", "").replace("+", ""))
            if abs(old_pct - new_pct) > 0.05:
                print(f"  {FAIL} {code} 涨幅: 老={old_pct}% 新={new_pct}%")
                mismatches += 1

    if mismatches == 0:
        print(f"  {PASS} 逐字段对比全部一致")
    else:
        print(f"  {WARN} {mismatches} 处差异")


def compare_tdx_index():
    """对比 PyTDX 三大指数"""
    print("\n--- L2: PyTDX 三大指数 ---")

    new = fetch("index")
    if "error" in new:
        print(f"  {FAIL} 新系统失败: {new['error']}")
        return

    # 老系统
    from pytdx.hq import TdxHq_API
    idx_codes = [(1, "000001"), (0, "399001"), (0, "399006")]
    idx_map = {"000001": "上证指数", "399001": "深证指数", "399006": "创业指数"}

    api = TdxHq_API()
    if not api.connect("110.41.147.114", 7709):
        print(f"  {FAIL} 老系统 PyTDX 连接失败")
        return
    raw = api.get_security_quotes(idx_codes)
    api.disconnect()

    mismatches = 0
    for r in raw:
        code = r.get("code", "")
        name = idx_map.get(code)
        if not name:
            continue

        old_price = r.get("price", 0)
        new_price = new.get(name, 0)

        diff = abs(old_price - new_price) if old_price and new_price else 0
        status = PASS if diff < 0.5 else FAIL
        print(f"  {status} {name}: 老={old_price:.2f} 新={new_price:.2f}")
        if diff >= 0.5:
            mismatches += 1

    if mismatches == 0:
        print(f"  {PASS} 三大指数一致")
    else:
        print(f"  {WARN} {mismatches} 处差异")


def compare_tencent():
    """对比腾讯财经"""
    print("\n--- L1: 腾讯财经 ---")
    codes = get_stock_codes()[:5]
    if not codes:
        print(f"  {FAIL} 没有代码")
        return

    new = fetch("tencent", codes=codes)
    new_codes = [k for k in new if k != "_meta"]
    print(f"  {PASS} 新系统返回 {len(new_codes)} 只")

    for code in new_codes:
        q = new[code]
        name = q.get("name", "?")
        pe = q.get("pe_ttm", "?")
        pb = q.get("pb", "?")
        mcap = q.get("mcap_yi", "?")
        status = PASS if name != "?" else WARN
        print(f"  {status} {code}: {name} PE={pe} PB={pb} 市值={mcap}亿")


def compare_pipeline_sources():
    """对比老管道输出的 JSON vs 新系统"""
    print("\n--- 老管道 JSON 对比 ---")

    comparisons = [
        ("ths_hot", "hot_list.json", "同花顺热点", {}),
        ("northbound", "northbound.json", "北向资金", {}),
        ("sector_inflow", "sector_summary.json", "行业板块", {"top_n": 5}),
    ]

    for fetch_type, old_file, label, kwargs in comparisons:
        new = fetch(fetch_type, **kwargs)
        old_path = _PIPELINE_DIR / "outputs" / old_file

        if not old_path.exists():
            print(f"  {WARN} {label}: 老数据文件不存在 ({old_path})")
            continue

        try:
            with open(old_path) as f:
                old = json.load(f)
        except Exception as e:
            print(f"  {FAIL} {label}: 读取老数据失败 {e}")
            continue

        # 简单对比：关键字段有值
        if fetch_type == "ths_hot":
            new_total = len(new.get("stocks", new.get("hot_stocks", [])))
            old_total = len(old.get("hot_stocks", old.get("stocks", [])))
        elif fetch_type == "northbound":
            new_total = new.get("minute_count", 0)
            old_total = old.get("meta", {}).get("minute_count", 0)
        elif fetch_type == "sector_inflow":
            new_total = len(new.get("top", []))
            old_total = len(old.get("top", []))

        if new_total > 0 and old_total > 0:
            print(f"  {PASS} {label}: 新={new_total} 老={old_total}")
        elif new_total > 0:
            print(f"  {PASS} {label}: 新={new_total} (老数据缺失)")
        else:
            print(f"  {WARN} {label}: 新系统返回空 (可能非交易日)")


def compare_iwencai():
    """对比问财"""
    print("\n--- L1: 问财 ---")
    new = fetch("iwencai", query="涨停 非st", limit=5)
    if "error" in new:
        print(f"  {FAIL} 失败: {new['error']}")
        return
    count = new.get("row_count", len(new.get("datas", [])))
    print(f"  {PASS} 返回 {count} 条")


def main():
    print(f"ym-stock-data 对比验证 ({datetime.now().strftime('%H:%M:%S')})\n")

    tests = [
        ("PyTDX 三大指数", compare_tdx_index),
        ("PyTDX 个股报价", compare_tdx_quotes),
        ("腾讯财经", compare_tencent),
        ("老管道 JSON", compare_pipeline_sources),
        ("问财", compare_iwencai),
    ]

    passed, failed = 0, 0
    for name, func in tests:
        try:
            func()
            passed += 1
        except Exception as e:
            print(f"\n  {FAIL} {name} 异常: {e}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"总计: {passed}通过 {failed}失败")


if __name__ == "__main__":
    main()
