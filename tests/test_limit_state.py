import unittest
from unittest.mock import patch

from ym_stock_data import fetch
from ym_stock_data.sources import limit_state


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class LimitStateTests(unittest.TestCase):
    @patch("ym_stock_data.sources.limit_state.CLIENT.get")
    def test_fetch_limit_state_calculates_break_rate_and_height(self, get):
        payloads = [
            {"data": {"pool": [{"c": "600001", "n": "甲", "lbc": 3}]}},
            {"data": {"pool": [{"c": "600002", "n": "乙"}]}},
            {"data": {"pool": [{"c": "600003", "n": "丙"}]}},
            {"data": {"pool": []}},
        ]
        get.side_effect = [_Response(payload) for payload in payloads]

        result = limit_state.fetch_limit_state("20260714")

        self.assertEqual(1, result["zt_count"])
        self.assertEqual(1, result["zb_count"])
        self.assertEqual(50.0, result["break_rate"])
        self.assertEqual(3, result["max_board"])
        self.assertEqual("eastmoney_limit_pool", result["source"])

    @patch("ym_stock_data.sources.limit_state.CLIENT.get")
    def test_empty_pools_are_explicit_success_not_an_error(self, get):
        get.side_effect = [
            _Response({"data": {"pool": []}}) for _ in range(4)
        ]

        result = limit_state.fetch_limit_state("20260713")

        self.assertEqual(0, result["zt_count"])
        self.assertEqual(0.0, result["break_rate"])
        self.assertNotIn("error", result)

    @patch("ym_stock_data.sources.limit_state.CLIENT.get")
    def test_source_exception_is_not_disguised_as_empty_pools(self, get):
        get.side_effect = TimeoutError("timed out")

        result = limit_state.fetch_limit_state("20260714")

        self.assertEqual("timed out", result["error"])
        self.assertEqual("TimeoutError", result["error_type"])
        self.assertEqual("eastmoney_limit_pool", result["source"])
        self.assertNotIn("zt_count", result)

    @patch("ym_stock_data.sources.limit_state.CLIENT.get")
    def test_v1_routes_expose_both_planned_names_with_meta(self, get):
        get.side_effect = [
            _Response({"data": {"pool": []}}) for _ in range(8)
        ]

        short_name = fetch("limit_state", date="20260713")
        full_name = fetch("market_limit_state", date="20260713")

        self.assertEqual("limit_state", short_name["_meta"]["data_type"])
        self.assertEqual(
            "market_limit_state", full_name["_meta"]["data_type"]
        )


if __name__ == "__main__":
    unittest.main()
