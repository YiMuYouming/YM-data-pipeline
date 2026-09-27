from __future__ import annotations

import unittest
import json

from ym_stock_data.promotion_comparison import (
    build_comparison_report,
    build_consumer_input,
    compare_candidate_day,
    find_sample_session_gaps,
)


def _rate(numerator_codes, denominator_codes):
    numerator_codes = sorted(numerator_codes)
    denominator_codes = sorted(denominator_codes)
    numerator = len(numerator_codes)
    denominator = len(denominator_codes)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "pct": round(numerator / denominator * 100, 6) if denominator else None,
        "numerator_codes": numerator_codes,
        "promoted_codes": numerator_codes,
        "denominator_codes": denominator_codes,
    }


def _candidate_day():
    previous_boards = {
        "1": ["000001", "000002"],
        "2": ["000003"],
        "3": [],
        "4": ["000004"],
    }
    all_codes = sorted(code for codes in previous_boards.values() for code in codes)
    promoted = ["000001", "000003", "000004"]
    rates = {
        "one_to_two": _rate(["000001"], previous_boards["1"]),
        "two_to_three": _rate(["000003"], previous_boards["2"]),
        "three_to_four_plus": _rate([], previous_boards["3"]),
        "overall_candidate_all_tiers_over_previous_nonst_limitups": _rate(promoted, all_codes),
        "overall_current_market_watch_fill_subset": _rate(["000001", "000003"], all_codes),
        "overall_literal_consecutive_over_previous_consecutive": _rate(
            promoted, previous_boards["2"] + previous_boards["3"] + previous_boards["4"]
        ),
    }
    return {
        "trade_date": "2026-08-05",
        "previous_trade_date": "2026-08-04",
        "source": {
            "provider": "stocktoday",
            "target_fill_promotion_rates_provider_match": False,
            "previous_ingest_payload_sha256": "a" * 64,
            "current_ingest_payload_sha256": "b" * 64,
            "previous_source_time_status": "unavailable",
            "current_source_time_status": "unavailable",
            "external_provider_conflict": "not_assessed",
        },
        "row_coverage": {
            "previous_limit_up_rows": 4,
            "current_limit_up_rows": 3,
            "previous_unique_codes": 4,
            "current_unique_codes": 3,
            "missing_exchange_day_inside_pair": False,
        },
        "st": {
            "previous_excluded_count_by_name": 0,
            "current_excluded_count_by_name": 0,
            "previous_codes": [],
            "current_codes": [],
        },
        "high_board": {
            "previous_board4plus_count": 1,
            "previous_board4plus_promoted_count": 1,
            "previous_board4plus_codes": ["000004"],
            "previous_board4plus_promoted_codes": ["000004"],
        },
        "previous_nonst_codes_by_board": previous_boards,
        "rates": rates,
        "empty_denominators": ["three_to_four_plus"],
        "source_conflicts": {
            "within_stocktoday_ladder_crosscheck": "none marked",
            "between_stocktoday_and_fill_script_sources": "not_assessed",
        },
    }


def _fact_report():
    promotion = {
        "rates": {
            "one_to_two": _rate(["000001"], ["000001", "000002"]),
            "two_to_three": _rate(["000003"], ["000003"]),
            "three_to_four": _rate([], []),
            "overall": _rate(["000001", "000003", "000004"],
                             ["000001", "000002", "000003", "000004"]),
        },
        "excluded_st_previous": 0,
        "excluded_st_current": 0,
        "excluded_st_previous_codes": [],
        "excluded_st_current_codes": [],
        "previous_high_board_codes": ["000004"],
        "promoted_high_board_codes": ["000004"],
        "previous_non_st_count": 4,
        "current_non_st_count": 3,
        "current_consecutive_count": 2,
        "current_consecutive_codes": ["000003", "000004"],
        "previous_consecutive_codes": ["000003", "000004"],
    }
    return {
        "trade_date": "20260805",
        "previous_trade_date": "20260804",
        "promotion": promotion,
        "source_gaps": [],
        "limit_evidence": {
            "previous": {"payload_sha256": "a" * 64, "source_time_status": "unavailable",
                         "provider": "stocktoday"},
            "current": {"payload_sha256": "b" * 64, "source_time_status": "unavailable",
                        "provider": "stocktoday"},
        },
    }


class PromotionComparisonTests(unittest.TestCase):
    def test_compares_stock_sets_and_reports_source_quality_gate_separately(self):
        result = compare_candidate_day(_candidate_day(), _fact_report())

        self.assertTrue(result["source_hashes_match"])
        self.assertTrue(result["rates"]["one_to_two"]["matched"])
        self.assertTrue(result["rates"]["three_to_four_plus"]["matched"])
        self.assertEqual(result["rates"]["three_to_four_plus"]["pipeline"]["pct"], None)
        self.assertFalse(result["reliability"]["reliable_day"])
        self.assertIn("source_time_unavailable", result["reliability"]["blockers"])
        self.assertIn("external_provider_conflict_not_assessed", result["reliability"]["blockers"])
        self.assertEqual(
            result["source_provenance"]["current"]["pipeline"]["source_time_status"],
            "unavailable",
        )

    def test_explains_high_board_omission_and_flags_unresolved_literal_overall(self):
        result = compare_candidate_day(_candidate_day(), _fact_report())

        subset = result["overall_candidates"]["market_watch_fill_subset"]
        self.assertEqual(subset["omitted_high_board_promoted_codes"], ["000004"])
        literal = result["overall_candidates"]["literal_consecutive"]
        self.assertEqual(literal["definition_status"], "unresolved")
        self.assertEqual(literal["candidate_numerator_outside_denominator_codes"], ["000001"])
        self.assertGreater(literal["candidate"]["pct"], 100.0)

    def test_reports_per_metric_code_differences_without_turning_empty_into_zero(self):
        candidate = _candidate_day()
        candidate["rates"]["one_to_two"]["numerator_codes"] = ["000002"]
        result = compare_candidate_day(candidate, _fact_report())

        one_to_two = result["rates"]["one_to_two"]
        self.assertFalse(one_to_two["matched"])
        self.assertEqual(one_to_two["code_differences"]["numerator_candidate_only"], ["000002"])
        self.assertEqual(one_to_two["code_differences"]["numerator_pipeline_only"], ["000001"])
        self.assertFalse(result["rates"]["one_to_two"]["matched"])
        self.assertIsNone(result["rates"]["three_to_four_plus"]["candidate"]["pct"])

    def test_lists_exchange_sessions_omitted_between_candidate_windows(self):
        result = find_sample_session_gaps(["2026-08-06", "2026-08-13"])

        self.assertEqual(result["status"], "verified")
        self.assertEqual(
            result["gaps"][0]["omitted_sample_sessions"],
            ["2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12"],
        )

    def test_hash_match_does_not_clear_history_quality_or_overall_definition_gates(self):
        candidate = _candidate_day()
        report = build_comparison_report(
            {"schema_version": "market-watch.p4-metric-baseline.v1",
             "candidate_days": [candidate]},
            {candidate["trade_date"]: _fact_report()},
        )

        self.assertEqual(report["p4_gate"]["source_hash_recomputed_days"], 1)
        self.assertEqual(report["p4_gate"]["reliable_history_days"], 0)
        self.assertFalse(report["p4_gate"]["authority_switch_allowed"])
        self.assertIn("overall_definition_unresolved", report["p4_gate"]["blockers"])

    def test_consumer_projection_is_shadow_only_and_contains_no_stock_codes(self):
        result = build_consumer_input(_candidate_day(), _fact_report())

        self.assertEqual(result["schema_version"], "market-watch.promotion-metrics.v1")
        self.assertEqual(result["authority"], {"mode": "shadow_only", "active": False})
        self.assertEqual(result["metrics"]["three_to_four_plus"]["pct"], None)
        self.assertEqual(result["metrics"]["overall_candidates"][0]["definition_status"], "unresolved")
        self.assertIn("external_provider_conflict_not_assessed", result["source_conflicts"])
        self.assertNotIn("000001", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
