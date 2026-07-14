import unittest
from unittest.mock import patch

from ym_stock_data import fetch
from ym_stock_data.sources import stock_events


class StockEventsTests(unittest.TestCase):
    @patch("ym_stock_data.sources.stock_events.eastmoney_datacenter")
    def test_fetch_lockup_maps_only_stable_fields(self, query):
        query.return_value = [
            {
                "SECURITY_CODE": "600519",
                "FREE_DATE": "2026-08-01",
                "FREE_SHARES_TYPE": "首发原股东限售股份",
                "FREE_SHARES": 100,
                "ABLE_FREE_SHARES": 80,
                "UNSTABLE_FIELD": "must not leak",
            }
        ]

        result = stock_events.fetch_stock_event("lockup", "600519")

        self.assertEqual("2026-08-01", result["items"][0]["date"])
        self.assertEqual(80, result["items"][0]["able_shares"])
        self.assertEqual(
            {"date", "type", "shares", "able_shares"},
            set(result["items"][0]),
        )
        self.assertEqual("eastmoney_datacenter", result["source"])

    @patch("ym_stock_data.sources.stock_events.eastmoney_datacenter")
    def test_all_event_subtypes_use_explicit_normalizers(self, query):
        fixtures = {
            "margin": ({"DATE": "2026-07-14", "RZYE": 1}, "rzye"),
            "block_trade": (
                {"TRADE_DATE": "2026-07-14", "DEAL_PRICE": 2},
                "price",
            ),
            "holder_num": (
                {"END_DATE": "2026-06-30", "HOLDER_NUM": 3},
                "holder_num",
            ),
            "dividend": (
                {"EX_DIVIDEND_DATE": "2026-07-01", "PRETAX_BONUS_RMB": 4},
                "bonus_rmb",
            ),
        }
        for event, (row, expected_field) in fixtures.items():
            with self.subTest(event=event):
                query.return_value = [{**row, "UNSTABLE_FIELD": "must not leak"}]
                result = stock_events.fetch_stock_event(event, "600519")
                self.assertIn(expected_field, result["items"][0])
                self.assertNotIn("UNSTABLE_FIELD", result["items"][0])

    @patch("ym_stock_data.sources.stock_events.eastmoney_datacenter")
    def test_empty_result_is_explicit_success(self, query):
        query.return_value = []

        result = stock_events.fetch_stock_event("dividend", "600519")

        self.assertEqual(0, result["total"])
        self.assertEqual([], result["items"])
        self.assertNotIn("error", result)

    @patch("ym_stock_data.sources.stock_events.eastmoney_datacenter")
    def test_timeout_is_exposed_instead_of_becoming_empty(self, query):
        query.side_effect = TimeoutError("timed out")

        result = stock_events.fetch_stock_event("lockup", "600519")

        self.assertEqual("timed out", result["error"])
        self.assertEqual("TimeoutError", result["error_type"])
        self.assertEqual([], result["items"])

    def test_unknown_event_is_rejected(self):
        with self.assertRaises(ValueError):
            stock_events.fetch_stock_event("unknown", "600519")

    @patch("ym_stock_data.sources.stock_events.eastmoney_datacenter")
    def test_v1_route_adds_meta_without_changing_source_payload(self, query):
        query.return_value = []

        result = fetch("stock_event", event="lockup", code="600519")

        self.assertEqual("eastmoney_datacenter", result["source"])
        self.assertEqual("stock_event", result["_meta"]["data_type"])


if __name__ == "__main__":
    unittest.main()
