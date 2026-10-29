"""H20 #6：sector_index 全天 DATA_TIME_UNKNOWN。同花顺行业摘要页不带数据时间，
但响应头 Date 是上游服务器生成该页的时间——取它作数据时间（不是我们的取数时间），
并按交易时段封顶：收盘后封到 15:00，午休封到 11:30，非交易日／盘前取上一收盘；头缺失仍如实 UNKNOWN。"""
import unittest
from datetime import datetime
from unittest.mock import patch

from ym_stock_data import api
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.sources import ths_industry


class _Response:
    def __init__(self, date_header=None, status_code=200):
        self.text = "<html></html>"
        self.status_code = status_code
        self.encoding = "gbk"
        self.headers = {"Date": date_header} if date_header else {}


ROW = {"code": "881175", "name": "医疗服务", "change_pct": -1.74}


def fetch(date_header):
    with patch.object(ths_industry.requests, "get", side_effect=[_Response(date_header) for _ in range(6)]), \
            patch.object(ths_industry, "_parse_industry_rows", side_effect=[[dict(ROW)], [], [], [], [], []]):
        return ths_industry.fetch_sector_index(codes=["881175"])


class ServerTime(unittest.TestCase):
    def test_http_date_becomes_server_time_in_taipei(self):
        result = fetch("Thu, 29 Oct 2026 02:30:00 GMT")  # 10:30 台北
        self.assertEqual("2026-10-29T10:30:00+08:00", result["server_time"])
        self.assertEqual("http_date_header", result["data_time_basis"])
        self.assertEqual(["881175"], list(result["by_code"]))

    def test_missing_header_gives_no_server_time(self):
        result = fetch(None)
        self.assertIsNone(result.get("server_time"))

    def test_garbage_header_gives_no_server_time(self):
        self.assertIsNone(fetch("not a date").get("server_time"))


def as_of(server_time):
    return api._data_as_of("sector_index", {}, {"items": [], "server_time": server_time})


class DataAsOf(unittest.TestCase):
    def test_in_session_uses_the_server_time(self):
        self.assertEqual(datetime.fromisoformat("2026-10-29T10:30:00+08:00"), as_of("2026-10-29T10:30:00+08:00"))

    def test_after_close_is_capped_at_1500(self):
        self.assertEqual(datetime(2026, 10, 29, 15, 0, tzinfo=TZ_SHANGHAI), as_of("2026-10-29T20:47:17+08:00"))

    def test_lunch_break_is_capped_at_1130(self):
        self.assertEqual(datetime(2026, 10, 29, 11, 30, tzinfo=TZ_SHANGHAI), as_of("2026-10-29T12:10:00+08:00"))

    def test_non_trading_day_uses_previous_close(self):
        got = as_of("2026-10-31T10:00:00+08:00")  # 周六
        self.assertEqual(datetime(2026, 10, 30, 15, 0, tzinfo=TZ_SHANGHAI), got)

    def test_no_server_time_stays_unknown(self):
        self.assertIsNone(as_of(None))


if __name__ == "__main__":
    unittest.main()
