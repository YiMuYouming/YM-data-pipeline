"""test_st_codes_source.py — W1 S8（N3）：ST 名单不来自"当天涨没涨停"

以前 ST 名单是从 ``limit_events`` 的名字里挑的，于是**从没涨跌停过的 ST 剔不掉**——
它根本没进过 limit_events。改用全市场快照自带的名称。
"""

import unittest

from ym_stock_data.market_facts import st_codes_for_cohort


class StCodesSourceTest(unittest.TestCase):
    def test_quiet_st_is_still_excluded(self):
        snapshot = {"600001": {"name": "浦发银行"}, "600002": {"name": "*ST 东旭"}}
        limit_events = {"600002": "*ST 东旭"}     # 当天它没涨跌停：根本没进 limit_events
        codes = st_codes_for_cohort(snapshot, limit_events=limit_events)
        self.assertEqual(codes, {"600002"})

    def test_limit_events_is_only_a_fallback(self):
        snapshot = {"600001": {"name": "浦发银行"}}
        codes = st_codes_for_cohort(snapshot, limit_events={"600002": "*ST 东旭"})
        # 快照有名字时以快照为准，不把 limit_events 里的 600002 混进来
        self.assertEqual(codes, set())

    def test_snapshot_wins_when_both_agree(self):
        snapshot = {"600002": {"name": "*ST 东旭"}}
        codes = st_codes_for_cohort(snapshot, limit_events={"600002": "*ST 东旭"})
        self.assertEqual(codes, {"600002"})

    def test_missing_names_fall_back_to_limit_events(self):
        """快照没带名称时退回 limit_events，并在来源上标明这是次优来源。"""
        snapshot = {"600002": {}}
        codes, source = st_codes_for_cohort(snapshot, limit_events={"600002": "*ST 东旭"},
                                            with_source=True)
        self.assertEqual(codes, {"600002"})
        self.assertEqual(source, "limit_events_fallback")

    def test_source_is_the_snapshot_when_it_has_names(self):
        snapshot = {"600002": {"name": "*ST 东旭"}}
        _codes, source = st_codes_for_cohort(snapshot, with_source=True)
        self.assertEqual(source, "stock_snapshot_names")

    def test_no_source_at_all_is_reported(self):
        codes, source = st_codes_for_cohort({}, with_source=True)
        self.assertEqual(codes, set())
        self.assertEqual(source, "none")


if __name__ == "__main__":
    unittest.main()
