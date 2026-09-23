"""Promotion is a transition between complete limit-up stock sets, not a rise rate."""

import unittest
from unittest.mock import patch

from ym_stock_data import query
from ym_stock_data.sources.limit_state import derive_limit_promotion, fetch_limit_promotion


def state(day, rows):
    return {
        "date": day,
        "zt_count": len(rows),
        "zb_count": 0,
        "dt_count": 0,
        "break_rate": 0.0,
        "max_board": max((row["limit_days"] for row in rows), default=0),
        "pools": {"zt": rows, "zb": [], "dt": [], "yzt": []},
        "source": "eastmoney_limit_pool",
    }


def row(code, days, name="测试股份"):
    return {"code": code, "name": name, "limit_days": days}


class LimitPromotionTests(unittest.TestCase):
    def setUp(self):
        self.previous = state("20260922", [
            row("000001", 1), row("000002", 1), row("000003", 2),
            row("000004", 3), row("000005", 4),
        ])
        self.current = state("20260923", [
            row("000001", 2), row("000003", 3), row("000004", 4),
            row("000005", 5), row("000006", 1),
        ])

    def test_full_ladder_uses_successful_seals_and_counts_high_board(self):
        result = derive_limit_promotion(self.previous, self.current)
        rates = result["rates"]
        self.assertEqual((rates["one_to_two"]["numerator"], rates["one_to_two"]["denominator"], rates["one_to_two"]["pct"]), (1, 2, 50.0))
        self.assertEqual(rates["two_to_three"]["pct"], 100.0)
        self.assertEqual(rates["three_to_four"]["pct"], 100.0)
        self.assertEqual((rates["overall"]["numerator"], rates["overall"]["denominator"], rates["overall"]["pct"]), (4, 5, 80.0))
        self.assertEqual(result["current_consecutive_count"], 4)
        self.assertEqual(result["highest_board"], 5)

    def test_non_st_universe_and_missing_denominator_are_explicit(self):
        previous = state("20260922", [row("000001", 2), row("000002", 1, "*ST测试")])
        current = state("20260923", [row("000001", 3), row("000002", 2, "*ST测试")])
        result = derive_limit_promotion(previous, current)
        self.assertEqual(result["previous_non_st_count"], 1)
        self.assertEqual(result["excluded_st_previous"], 1)
        self.assertIsNone(result["rates"]["one_to_two"]["pct"])
        self.assertEqual(result["rates"]["overall"]["pct"], 100.0)

    def test_incomplete_or_inconsistent_pools_fail_closed(self):
        for bad in (
            state("20260922", []),
            {**self.previous, "zt_count": 99},
            state("20260922", [row("000001", 1), row("000001", 2)]),
            state("20260921", self.previous["pools"]["zt"]),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    derive_limit_promotion(bad, self.current, previous_date="20260922", current_date="20260923")

    def test_public_query_returns_derived_rates_from_two_dates(self):
        answers = {"20260922": self.previous, "20260923": self.current}
        with patch("ym_stock_data.sources.limit_state.fetch_limit_state", side_effect=lambda date: answers[date]):
            result = query("market_limit_state", date="20260923", previous_date="20260922")
        self.assertEqual(result["_meta"]["provider_used"], "eastmoney_limit_pool")
        self.assertIn(result["_meta"]["status"], {"success", "degraded"})
        self.assertEqual(result["data"]["promotion"]["rates"]["overall"]["pct"], 80.0)

    def test_public_query_can_resolve_previous_exchange_day(self):
        answers = {"20260922": self.previous, "20260923": self.current}
        with patch("ym_stock_data.sources.limit_state.fetch_limit_state", side_effect=lambda date: answers[date]) as fetch:
            result = query("market_limit_state", date="20260923", include_promotion=True)
        self.assertEqual(result["data"]["promotion"]["previous_date"], "20260922")
        self.assertEqual(fetch.call_count, 2)

    def test_previous_source_failure_does_not_return_bogus_zero(self):
        answers = {"20260922": {"error": "upstream", "error_type": "network_error"}, "20260923": self.current}
        with patch("ym_stock_data.sources.limit_state.fetch_limit_state", side_effect=lambda date: answers[date]):
            result = query("market_limit_state", date="20260923", previous_date="20260922")
        self.assertEqual(result["_meta"]["status"], "error")
        self.assertIsNone(result["data"])

    def test_previous_day_must_be_actual_exchange_predecessor(self):
        with self.assertRaises(ValueError):
            query("market_limit_state", date="20260923", previous_date="20260921")


if __name__ == "__main__":
    unittest.main()
