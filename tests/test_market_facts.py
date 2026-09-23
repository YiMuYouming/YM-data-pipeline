"""The daily fact store only derives from dated, complete provider snapshots."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ym_stock_data.market_facts import MarketFactStore, normalize_stocktoday_limits


def limit_result(day, up, down=(), broken=()):
    pools = {"zt": list(up), "dt": list(down), "zb": list(broken), "yzt": []}
    return {
        "data": {
            "date": day,
            "zt_count": len(up),
            "dt_count": len(down),
            "zb_count": len(broken),
            "break_rate": round(len(broken) / (len(up) + len(broken)) * 100, 2) if up or broken else 0,
            "max_board": max((x["limit_days"] for x in up), default=0),
            "pools": pools,
        },
        "_meta": {
            "status": "success", "provider_used": "eastmoney_limit_pool",
            "fetched_at": "2026-09-23T16:30:00+08:00",
            "quality": {"status": "normal"},
        },
    }


def row(code, days, name="测试股份"):
    return {"code": code, "name": name, "limit_days": days, "price": 10.0, "pct": 10.0}


class MarketFactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MarketFactStore(Path(self.temp.name) / "facts.sqlite3")

    def test_read_only_report_uses_existing_database(self):
        self.store.ingest_limits("20260923", limit_result("20260923", [row("000001", 1)]))
        reader = MarketFactStore(self.store.path, read_only=True)
        self.assertEqual(reader.report("20260923")["counts"]["up"], 1)
        with self.assertRaises(sqlite3.OperationalError):
            reader.ingest_limits("20260923", limit_result("20260923", [row("000002", 1)]))

    def test_read_only_report_does_not_create_missing_database(self):
        missing = Path(self.temp.name) / "missing" / "facts.sqlite3"
        with self.assertRaises(FileNotFoundError):
            MarketFactStore(missing, read_only=True)
        self.assertFalse(missing.parent.exists())

    def test_daily_sets_are_immutable_runs_and_report_promotions(self):
        self.store.ingest_limits("20260922", limit_result("20260922", [
            row("000001", 1), row("000002", 2), row("000003", 3), row("000004", 4),
        ]))
        self.store.ingest_limits("20260923", limit_result("20260923", [
            row("000001", 2), row("000002", 3), row("000003", 4), row("000004", 5),
            row("000005", 1),
        ], down=[row("000006", 1)], broken=[row("000007", 1)]))
        report = self.store.report("20260923")
        self.assertEqual(report["counts"], {"up": 5, "down": 1, "broken": 1})
        self.assertEqual(report["promotion"]["rates"]["overall"]["pct"], 100.0)
        self.assertEqual(report["promotion"]["rates"]["three_to_four"]["pct"], 100.0)
        self.assertEqual(report["promotion"]["highest_board"], 5)
        self.assertIsNone(report["yesterday_limit_up_return_pct"])
        self.assertIn("yesterday_limit_up_return_cohort_incomplete", report["source_gaps"])

    def test_invalid_or_empty_pool_does_not_create_a_run(self):
        good = limit_result("20260923", [row("000001", 1)])
        for bad in (
            {**good, "data": {**good["data"], "date": "20260922"}},
            {**good, "data": {**good["data"], "zt_count": 7}},
            limit_result("20260923", []),
        ):
            with self.assertRaises(ValueError):
                self.store.ingest_limits("20260923", bad)
        self.assertIsNone(self.store.latest_limit_run("20260923"))

    def test_quotes_require_every_prior_stock_and_original_same_day_time(self):
        self.store.ingest_limits("20260922", limit_result("20260922", [row("000001", 1), row("000002", 2)], broken=[row("000003", 1)]))
        self.store.ingest_limits("20260923", limit_result("20260923", [row("000001", 2)]))
        result = {
            "data": {
                "000001": {"price": 11.0, "change_pct": 1.0, "quote_time": "2026-09-23T15:00:00+08:00"},
                "000002": {"price": 9.0, "change_pct": -1.0, "quote_time": "2026-09-23T15:00:00+08:00"},
                "000003": {"price": 8.0, "change_pct": -4.0, "quote_time": "2026-09-23T15:00:00+08:00"},
            },
            "_meta": {"status": "success", "provider_used": "stocktoday", "fetched_at": "2026-09-23T16:30:00+08:00"},
        }
        self.store.ingest_quotes("20260923", result)
        report = self.store.report("20260923")
        self.assertEqual(report["yesterday_limit_up_return_pct"], 0.0)
        self.assertEqual(report["yesterday_consecutive_return_pct"], -1.0)
        self.assertEqual(report["yesterday_broken_return_pct"], -4.0)
        self.assertEqual(report["return_cohort_counts"], {"yesterday_limit_up": 2, "yesterday_consecutive": 1, "yesterday_broken": 1})
        bad = {**result, "data": {"000001": result["data"]["000001"]}}
        with self.assertRaises(ValueError):
            self.store.ingest_quotes("20260923", bad)
        stale = {**result, "data": {**result["data"], "000002": {**result["data"]["000002"], "quote_time": "2026-09-22T15:00:00+08:00"}}}
        with self.assertRaises(ValueError):
            self.store.ingest_quotes("20260923", stale)
        self.assertEqual(self.store.report("20260923")["yesterday_limit_up_return_pct"], 0.0)

    def test_final_run_wins_over_later_provisional_input(self):
        final = limit_result("20260923", [row("000001", 1)])
        self.store.ingest_limits("20260923", final)
        provisional = limit_result("20260923", [row("000002", 1)])
        provisional["_meta"]["fetched_at"] = "2026-09-23T14:00:00+08:00"
        self.store.ingest_limits("20260923", provisional)
        self.assertEqual(self.store.report("20260923")["limit_evidence"]["current"]["phase"], "post_close_observed")

    def test_history_requires_all_three_complete_dated_pools(self):
        def result(kind, rows):
            return {"data": {"items": rows, "truncated": False, "total_present": False},
                    "_meta": {"status": "success" if rows else "empty", "provider_used": "stocktoday",
                              "fetched_at": "2026-09-23T16:30:00+08:00"}}

        results = {
            "U": result("U", [{"ts_code": "000001.SZ", "trade_date": "20260922", "limit_type": "U", "limit_times": 2}]),
            "D": result("D", []),
            "Z": result("Z", [{"ts_code": "000002.SZ", "trade_date": "20260922", "limit_type": "Z", "limit_times": None}]),
            "STEP": result("STEP", [{"ts_code": "000001.SZ", "trade_date": "20260922", "nums": "2"}]),
        }
        normalized = normalize_stocktoday_limits("20260922", results)
        receipt = self.store.ingest_limits("20260922", normalized)
        self.assertEqual(receipt["counts"], {"up": 1, "down": 0, "broken": 1})
        broken = self.store._events(receipt["run_id"], "broken")
        self.assertEqual(broken[0]["board_count"], 0)
        broken_result = {**results["Z"], "_meta": {**results["Z"]["_meta"], "status": "error"}}
        with self.assertRaisesRegex(ValueError, "unavailable"):
            normalize_stocktoday_limits("20260922", {**results, "Z": broken_result})
        with self.assertRaisesRegex(ValueError, "date or type mismatch"):
            normalize_stocktoday_limits("20260922", {**results, "U": result("U", [{**results["U"]["data"]["items"][0], "trade_date": "20260923"}])})

    def test_shifted_history_recovers_board_from_ladder_without_bad_prices(self):
        def result(rows):
            return {"data": {"items": rows, "truncated": False, "total_present": False},
                    "_meta": {"status": "success" if rows else "empty", "provider_used": "stocktoday",
                              "fetched_at": "2026-09-23T17:00:00+08:00"}}

        up = {"ts_code": "000001.SZ", "trade_date": "20260922", "limit_type": "U",
              "name": "测试股份", "close": 0, "pct_chg": 0, "open_times": 111333, "limit_times": 0}
        inputs = {"U": result([up]), "D": result([]), "Z": result([]),
                  "STEP": result([{"ts_code": "000001.SZ", "trade_date": "20260922", "nums": "3"},
                                  {"ts_code": "000002.SZ", "trade_date": "20260922", "nums": "2", "name": "*ST测试"}])}
        normalized = normalize_stocktoday_limits("20260922", inputs)
        self.assertEqual(normalized["data"]["pools"]["zt"][0]["limit_days"], 3)
        self.assertIsNone(normalized["data"]["pools"]["zt"][0]["price"])
        self.assertEqual(normalized["data"]["_board_source"], "limit_step_reference_unverified")
        clean = {**up, "close": 10.0, "pct_chg": 10.0, "limit_times": 1}
        disagreement = normalize_stocktoday_limits("20260922", {**inputs, "U": result([clean])})
        self.assertEqual(disagreement["data"]["_board_source"], "limit_list_d_ladder_disagreement")
        self.assertEqual(disagreement["data"]["pools"]["zt"][0]["limit_days"], 1)
        self.store.ingest_limits("20260922", normalized)
        current = limit_result("20260923", [row("000001", 4)])
        current["_meta"]["provider_used"] = "stocktoday"
        self.store.ingest_limits("20260923", current)
        report = self.store.report("20260923")
        self.assertIsNone(report["promotion"])
        self.assertEqual(report["promotion_overall_by_code"]["numerator"], 1)
        self.assertIn("promotion_tier_board_counts_unverified", report["source_gaps"])

    def test_daily_emotion_uses_up_over_all_traded_including_flat(self):
        items = [
            {"ts_code": f"{i:06d}.SZ", "trade_date": "20260923",
             "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0,
             "pre_close": 10.0, "pct_chg": (1.0 if i < 2000 else 0.0 if i < 3000 else -1.0),
             "vol": 100.0, "amount": 1000.0}
            for i in range(4000)
        ]
        result = {"data": {"items": items, "truncated": False, "total_present": False},
                  "_meta": {"status": "success", "provider_used": "stocktoday",
                            "fetched_at": "2026-09-23T17:00:00+08:00"}}
        receipt = self.store.ingest_daily("20260923", result)
        self.assertEqual(receipt["row_count"], 4000)
        report = self.store.report("20260923")
        self.assertEqual(report["yimu_emotion"]["score"], 50.0)
        self.assertEqual(report["yimu_emotion"]["denominator"], 4000)
        self.assertEqual(report["yimu_emotion"]["flat"], 1000)
        self.assertIsNone(report["ths_emotion_equivalent"])
        self.assertIn("emotion_all_listed_denominator_unverified", report["source_gaps"])
        self.assertIsNone(report["consecutive_break_risk"])
        bad = {**result, "data": {**result["data"], "truncated": True}}
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.store.ingest_daily("20260923", bad)
        self.assertEqual(self.store.report("20260923")["yimu_emotion"]["run_id"], receipt["run_id"])

    def test_missing_today_up_stock_does_not_hide_complete_broken_return(self):
        self.store.ingest_limits("20260922", limit_result("20260922", [row("999999", 2)], broken=[row("000001", 0)]))
        items = [
            {"ts_code": f"{i:06d}.SZ", "trade_date": "20260923",
             "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0,
             "pre_close": 10.0, "pct_chg": -2.0 if i == 1 else 0.0,
             "vol": 100.0, "amount": 1000.0}
            for i in range(4000)
        ]
        self.store.ingest_daily("20260923", {
            "data": {"items": items, "truncated": False},
            "_meta": {"status": "success", "provider_used": "stocktoday",
                      "fetched_at": "2026-09-23T17:00:00+08:00"},
        })
        report = self.store.report("20260923")
        self.assertIsNone(report["yesterday_limit_up_return_pct"])
        self.assertIsNone(report["yesterday_consecutive_return_pct"])
        self.assertEqual(report["yesterday_broken_return_pct"], -2.0)
        self.assertIn("yesterday_limit_up_return_cohort_incomplete", report["source_gaps"])
        self.assertEqual(report["return_evidence"]["yesterday_broken"]["provider"], "stocktoday")

    def test_limit_stock_missing_from_same_day_daily_blocks_promotion(self):
        self.store.ingest_limits("20260922", limit_result("20260922", [row("000001", 1)]))
        self.store.ingest_limits("20260923", limit_result("20260923", [row("999999", 2)]))
        items = [
            {"ts_code": f"{i:06d}.SZ", "trade_date": "20260923",
             "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0,
             "pre_close": 10.0, "pct_chg": 1.0,
             "vol": 100.0, "amount": 1000.0}
            for i in range(4000)
        ]
        self.store.ingest_daily("20260923", {
            "data": {"items": items, "truncated": False},
            "_meta": {"status": "success", "provider_used": "stocktoday",
                      "fetched_at": "2026-09-23T17:00:00+08:00"},
        })
        report = self.store.report("20260923")
        self.assertEqual(report["limit_daily_quality"]["current"]["missing_codes"], ["999999"])
        self.assertIsNone(report["promotion"])
        self.assertIsNone(report["promotion_overall_by_code"])
        self.assertIn("current_limit_daily_universe_conflict", report["source_gaps"])


if __name__ == "__main__":
    unittest.main()
