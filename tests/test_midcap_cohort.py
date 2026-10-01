"""test_midcap_cohort.py — W1 S8（N2/N3）：中军样本的剔除与缺数据要分开

以前 `missing_codes` 一栏里同时装着"按规则剔除的"（北交所、ST、停牌新股、除权日）
和"真缺数据的"。看覆盖率时两者混在一起，算出来的比例说不好代表什么。
"""

import unittest

from ym_stock_data.indicators import (
    MIN_COVERAGE_FOR_PCT,
    midcap_above_ma20_pct,
)


def bars(closes, pre=None):
    if pre is None:
        pre = [closes[0]] + closes[:-1]
    return [(c, p) for c, p in zip(closes, pre)]


def twenty_above():
    return [10.0] * 19 + [11.0]


class ExclusionReasonsTest(unittest.TestCase):
    def test_each_rule_states_its_reason(self):
        cohort = {
            "600001": bars(twenty_above()),
            "830001": bars(twenty_above()),          # 北交所
            "600002": bars(twenty_above()),          # ST
            "600003": bars([10.0] * 5),              # 停牌/新股
        }
        result = midcap_above_ma20_pct(cohort, st_codes={"600002"})
        excluded = result["excluded_codes"]
        self.assertEqual(excluded["830001"], "beijing")
        self.assertEqual(excluded["600002"], "st")
        self.assertIn("600003", excluded)
        self.assertEqual(excluded["600003"], "suspended_or_new")

    def test_excluded_and_missing_are_different_sets(self):
        """按规则剔除的进 excluded_codes；真没有数据的才叫 missing。"""
        cohort = {
            "600001": bars(twenty_above()),
            "830001": bars(twenty_above()),
            "600002": bars(twenty_above()),
        }
        result = midcap_above_ma20_pct(cohort, st_codes={"600002"})
        self.assertEqual(result["missing_codes"], [])
        self.assertEqual(set(result["excluded_codes"]), {"830001", "600002"})
        self.assertNotIn("830001", result["missing_codes"])

    def test_legacy_missing_codes_key_is_gone(self):
        cohort = {"830001": bars(twenty_above())}
        result = midcap_above_ma20_pct(cohort)
        self.assertNotIn("missing_codes_with_reason", result)


class CoverageFloorTest(unittest.TestCase):
    def test_low_coverage_makes_pct_null_and_records_a_typed_gap(self):
        """覆盖率 < 0.8 时比例不可用：宁可给 null + typed gap，也不给一个没意义的数。"""
        cohort = {"600001": bars(twenty_above())}
        for index in range(4):
            cohort[f"83000{index}"] = bars(twenty_above())   # 都被剔除
        result = midcap_above_ma20_pct(cohort)
        self.assertLess(result["coverage"], MIN_COVERAGE_FOR_PCT)
        self.assertIsNone(result["pct"])
        self.assertIsNone(result["score"])
        codes = [gap["gap_code"] for gap in result["source_gaps"]]
        self.assertTrue(any("midcap_coverage_below_floor" in code for code in codes), codes)

    def test_typed_gap_shape(self):
        cohort = {"600001": bars(twenty_above()), "830001": bars(twenty_above())}
        result = midcap_above_ma20_pct(cohort)
        gap = next(gap for gap in result["source_gaps"]
                   if gap["gap_code"].startswith("midcap_coverage_below_floor"))
        self.assertEqual(gap["scope"], "advisory")
        self.assertNotIn(gap["scope"], ("side_hard", "candidate_hard"))
        self.assertIn("affected_review_cells", gap)
        self.assertIn("coverage", gap)

    def test_good_coverage_keeps_the_number_and_has_no_gap(self):
        cohort = {f"6000{index:02d}": bars(twenty_above()) for index in range(10)}
        result = midcap_above_ma20_pct(cohort)
        self.assertEqual(result["pct"], 100.0)
        self.assertEqual(result["score"], 10)
        self.assertEqual(result["source_gaps"], [])


class NoLimitEventDependencyTest(unittest.TestCase):
    def test_st_comes_from_the_supplied_name_set_not_from_limit_events(self):
        """N3：ST 名单由调用方给的股票基础信息决定，与当日有没有涨跌停无关。"""
        cohort = {"600001": bars(twenty_above()), "600002": bars(twenty_above())}
        quiet_day = midcap_above_ma20_pct(cohort, st_codes={"600002"})
        self.assertEqual(quiet_day["excluded_codes"]["600002"], "st")
        self.assertEqual(quiet_day["above_count"], 1)


if __name__ == "__main__":
    unittest.main()
