"""问财 OpenAPI 查询测试"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ym_stock_data.sources.iwencai import query, query_stocks, query_rank


def test_query():
    r = query("涨停 非st", limit=3)
    assert "error" not in r, f"API error: {r.get('error')}"
    datas = r.get("datas", [])
    assert len(datas) > 0, "empty datas"
    print(f"✅ test_query: {len(datas)}条, fields={len(r.get('columns',[]))}")


def test_query_stocks():
    r = query_stocks(["信维通信"])
    found = [k for k, v in r.items() if v]
    assert len(found) > 0, "no stock data"
    print(f"✅ test_query_stocks: {len(found)}只")


def test_query_rank():
    r = query_rank("信维通信")
    assert "error" not in r, f"error: {r.get('error')}"
    print(f"✅ test_query_rank: ok")


if __name__ == "__main__":
    test_query()
    test_query_stocks()
    test_query_rank()
    print("\n全部通过 ✅")
