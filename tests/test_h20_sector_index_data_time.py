"""HTTP Date is retained as server time; missing quote clocks stay unknown (11-01 #10)."""
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
        self.assertEqual("http_date_header", result["server_time_basis"])
        self.assertEqual('DATA_TIME_UNKNOWN', result['data_time_reason_code'])
        self.assertEqual(["881175"], list(result["by_code"]))

    def test_missing_header_gives_no_server_time(self):
        result = fetch(None)
        self.assertIsNone(result.get("server_time"))

    def test_garbage_header_gives_no_server_time(self):
        self.assertIsNone(fetch("not a date").get("server_time"))


def as_of(server_time):
    return api._data_as_of("sector_index", {}, {"items": [], "server_time": server_time})


class DataAsOf(unittest.TestCase):
    def test_in_session_server_time_is_not_quote_time(self):
        self.assertIsNone(as_of("2026-10-29T10:30:00+08:00"))

    def test_after_close_is_capped_at_1500(self):
        self.assertIsNone(as_of("2026-10-29T20:47:17+08:00"))

    def test_lunch_break_is_capped_at_1130(self):
        self.assertIsNone(as_of("2026-10-29T12:10:00+08:00"))

    def test_non_trading_day_uses_previous_close(self):
        got = as_of("2026-10-31T10:00:00+08:00")  # 周六
        self.assertIsNone(got)

    def test_no_server_time_stays_unknown(self):
        self.assertIsNone(as_of(None))


if __name__ == "__main__":
    unittest.main()
