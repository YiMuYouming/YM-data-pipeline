import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from ym_stock_data.sources import northbound


class NorthboundSemanticsTests(unittest.TestCase):
    @patch("ym_stock_data.sources.northbound.requests.get")
    def test_sgt_is_retained_but_marked_reference_only(self, get):
        response = Mock()
        response.json.return_value = {
            "time": ["09:31"],
            "hgt": [1.2],
            "sgt": [2.3],
        }
        response.raise_for_status.return_value = None
        get.return_value = response

        result = northbound.fetch_realtime()

        self.assertEqual(2.3, result["sgt_current_yi"])
        self.assertEqual("reference_only", result.get("sgt_reliability"))
        self.assertEqual("intraday_reference", result.get("data_scope"))
        self.assertEqual("hkex_daily", result.get("authoritative_source"))

    def test_fields_policy_records_intraday_reference_scope(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "ym_stock_data"
            / "v2"
            / "policies"
            / "fields.json"
        )
        policies = json.loads(path.read_text(encoding="utf-8"))
        rows = [
            row for row in policies
            if row.get("intent") == "northbound_intraday"
        ]

        self.assertTrue(rows)
        self.assertTrue(
            all(row.get("data_scope") == "intraday_reference" for row in rows)
        )
        self.assertTrue(
            all(row.get("authoritative_source") == "hkex_daily" for row in rows)
        )


if __name__ == "__main__":
    unittest.main()
