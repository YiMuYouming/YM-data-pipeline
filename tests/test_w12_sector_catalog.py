import unittest
from unittest.mock import Mock
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.providers.stocktoday import StockTodayProvider


class SectorCatalogTest(unittest.TestCase):
    def test_industry_rows_keep_vendor_time_and_reject_other_types(self):
        provider = StockTodayProvider.__new__(StockTodayProvider)
        provider._request_table = Mock(return_value=ProviderOutcome("stocktoday", "success", data={"items": [
            {"ts_code": "881001.TI", "type": "I", "name": "行业", "trade_time": "2026-10-09T15:00:00+08:00"},
            {"ts_code": "885001.TI", "type": "N", "name": "概念"}]}))
        result = provider._call_sector_index({"codes": ["881001", "885001"]})
        self.assertEqual([row["name"] for row in result.data["items"]], ["行业"])
        self.assertEqual(result.data["field_evidence"]["881001"]["data_as_of"], "2026-10-09T15:00:00+08:00")
        self.assertIn("885001", result.data["missing"])
