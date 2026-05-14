"""PyTDX 连接和数据查询测试"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ym_stock_data.sources.pytdx import (
    _get_api, to_tdx_code, fetch_quotes, fetch_index,
    fetch_breadth, fetch_sector, fetch_kline, fetch_kline_15m, disconnect
)


def test_connection():
    api = _get_api()
    assert api is not None, "PyTDX 连接失败"
    print("✅ test_connection: 连接成功")


def test_to_tdx_code():
    assert to_tdx_code("688017") == (1, "688017")
    assert to_tdx_code("300476") == (0, "300476")
    assert to_tdx_code("000001") == (0, "000001")
    print("✅ test_to_tdx_code: 格式正确")


def test_fetch_index():
    r = fetch_index()
    assert "上证指数" in r, f"missing 上证指数: {list(r.keys())[:3]}"
    assert "深证指数" in r
    assert "成交额" in r
    print(f"✅ test_fetch_index: 上证={r['上证指数']}")


def test_fetch_quotes():
    r = fetch_quotes(["688017"])
    assert len(r) > 0, "empty result"
    q = list(r.values())[0]
    assert "最新价" in q
    assert "涨幅" in q
    assert "MA10_60m" in q
    print(f"✅ test_fetch_quotes: {len(r)}只, fields={list(q.keys())[:4]}")


def test_fetch_sector():
    r = fetch_sector(["半导体"])
    assert len(r) > 0
    s = list(r.values())[0]
    assert "涨跌幅" in s
    assert "MA5" in s
    print(f"✅ test_fetch_sector: {list(r.keys())[0]}")


def test_fetch_kline():
    r = fetch_kline("688017", period="daily")
    assert r["total_bars"] > 0
    assert "MA5" in r.get("mas", {})
    print(f"✅ test_fetch_kline: {r['total_bars']}条K线")


if __name__ == "__main__":
    test_connection()
    test_to_tdx_code()
    test_fetch_index()
    test_fetch_quotes()
    test_fetch_sector()
    test_fetch_kline()
    disconnect()
    print("\n全部通过 ✅")
