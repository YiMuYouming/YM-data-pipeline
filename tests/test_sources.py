"""HTTP 数据源全量测试 (零鉴权)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ym_stock_data import fetch


def test_ths_hot():
    r = fetch("ths_hot")
    assert r["total"] > 0, "empty hot list"
    reason_stats = r.get("reason_stats", {})
    print(f"✅ ths_hot: {r['total']}只, zt_count={r.get('zt_count',0)}, 题材={len(reason_stats)}种")


def test_tencent():
    r = fetch("tencent", codes=["688017", "300476"])
    for code, q in r.items():
        if code == "_meta": continue
        assert q.get("name"), f"missing name for {code}"
        assert q.get("pe_ttm", 0) != 0 or q.get("pb", 0) != 0, f"no PE/PB for {code}"
    print(f"✅ tencent: {len([k for k in r if k!='_meta'])}只 OK")


def test_northbound():
    r = fetch("northbound")
    assert r["minute_count"] > 0, "empty northbound"
    print(f"✅ northbound: {r['minute_count']}分钟点, HGT={r.get('hgt_current_yi',0):+.1f}亿")


def test_sector_inflow():
    r = fetch("sector_inflow", top_n=5)
    assert len(r.get("top", [])) > 0, "empty sector inflow"
    top = r["top"][0]
    assert "name" in top and "net_inflow_yi" in top
    print(f"✅ sector_inflow: {r['total']}行业, TOP={top['name']}")


def test_dragon_tiger():
    r = fetch("dragon_tiger")
    total = r.get("total_records", 0)
    print(f"{'✅' if total > 0 else '⚠️'} dragon_tiger: {total}条 (可能非交易日)")


def test_news():
    r = fetch("news", limit=5)
    assert r["total"] > 0, "empty news"
    print(f"✅ news: {r['total']}条")


def test_filings():
    r = fetch("filings", code="600519", days=30, max_pages=1)
    assert r["total"] > 0, "empty filings"
    print(f"✅ filings: 贵州茅台 {r['total']}条公告")


def test_research():
    r = fetch("research", code="600519", days=30, max_pages=15)
    print(f"{'✅' if r['total'] > 0 else '⚠️'} research: {r['total']}条研报")


if __name__ == "__main__":
    test_ths_hot()
    test_tencent()
    test_northbound()
    test_sector_inflow()
    test_dragon_tiger()
    test_news()
    test_filings()
    test_research()
    print("\n全部通过 ✅")
