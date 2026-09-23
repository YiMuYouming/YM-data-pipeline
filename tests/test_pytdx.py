"""Offline mapping check plus explicitly skipped PyTDX live probes."""

import unittest

from ym_stock_data.sources.pytdx import to_tdx_code


class PytdxOfflineMappingTests(unittest.TestCase):
    def test_to_tdx_code(self):
        self.assertEqual((1, "688017"), to_tdx_code("688017"))
        self.assertEqual((0, "300476"), to_tdx_code("300476"))
        self.assertEqual((0, "000001"), to_tdx_code("000001"))


@unittest.skip("live integration moved to: ym-data smoke --live")
class PytdxLiveIntegrationTests(unittest.TestCase):
    def test_connection_and_read_matrix(self):
        from ym_stock_data.sources.pytdx import (
            _get_api,
            fetch_index,
            fetch_kline,
            fetch_quotes,
            fetch_sector,
        )

        self.assertIsNotNone(_get_api())
        self.assertIn("上证指数", fetch_index())
        self.assertTrue(fetch_quotes(["688017"]))
        self.assertTrue(fetch_sector(["半导体"]))
        self.assertGreater(fetch_kline("688017", period="daily")["total_bars"], 0)
