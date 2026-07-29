"""Explicitly skipped legacy live checks; use ``ym-data smoke --live``."""

import unittest


@unittest.skip("live integration moved to: ym-data smoke --live")
class IWenCaiLiveIntegrationTests(unittest.TestCase):
    def test_query(self):
        from ym_stock_data.sources.iwencai import query

        result = query("涨停 非st", limit=3)
        self.assertNotIn("error", result)
        self.assertTrue(result.get("datas"))

    def test_query_stocks(self):
        from ym_stock_data.sources.iwencai import query_stocks

        result = query_stocks(["信维通信"])
        self.assertTrue(any(result.values()))

    def test_query_rank(self):
        from ym_stock_data.sources.iwencai import query_rank

        self.assertNotIn("error", query_rank("信维通信"))
