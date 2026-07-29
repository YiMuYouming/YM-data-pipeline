"""Explicitly skipped legacy live checks; use ``ym-data smoke --live``."""

import unittest


@unittest.skip("live integration moved to: ym-data smoke --live")
class LegacySourceLiveIntegrationTests(unittest.TestCase):
    def test_zero_auth_source_matrix(self):
        from ym_stock_data import fetch

        self.assertGreater(fetch("ths_hot").get("total", 0), 0)
        self.assertTrue(fetch("tencent", codes=["688017"]))
        self.assertGreater(fetch("northbound").get("minute_count", 0), 0)
        self.assertTrue(fetch("sector_inflow", top_n=5).get("top"))
        self.assertIsInstance(fetch("dragon_tiger"), dict)
        self.assertGreater(fetch("news", limit=5).get("total", 0), 0)
        self.assertGreater(
            fetch("filings", code="600519", days=30, max_pages=1).get("total", 0),
            0,
        )
        self.assertIsInstance(
            fetch("research", code="600519", days=30, max_pages=15), dict
        )
