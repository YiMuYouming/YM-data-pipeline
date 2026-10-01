"""test_board_strength.py — W4 S1：逐板块强度字段（K5 形态）

字段按开工单 W4 第二节第 2 点：limit_up_count / limit_up_2plus_count /
seal_time_distribution / index_position_vs_ma5 / net_inflow_3d /
midcap_above_ma20_pct / rank_change，每个都是 {value, source, data_as_of, status}。

覆盖率不足（成员样本 <0.8）时数值置 null 并记 typed gap，**不给 0**。
"""

import unittest

from ym_stock_data.board_strength import (
    MIN_COVERAGE,
    board_strength,
    load_board_definitions,
)

INDUSTRY_BARS = [
    {"trade_date": "2026-09-23", "close": 100.0},
    {"trade_date": "2026-09-24", "close": 101.0},
    {"trade_date": "2026-09-25", "close": 102.0},
    {"trade_date": "2026-09-28", "close": 103.0},
    {"trade_date": "2026-09-29", "close": 104.0},
    {"trade_date": "2026-09-30", "close": 106.0},
]
FLOWS = [{"trade_date": "2026-09-28", "net_inflow_yi": 12.0},
         {"trade_date": "2026-09-29", "net_inflow_yi": -3.0},
         {"trade_date": "2026-09-30", "net_inflow_yi": 8.0}]
MEMBERS = [f"600{index:03d}" for index in range(10)]


def _pool():
    return [
        {"code": "600001", "board": 3, "seal_time": "09:35", "first_seal_time": "09:31",
         "industry": "881157"},
        {"code": "600002", "board": 1, "seal_time": "13:12", "first_seal_time": "13:12",
         "industry": "881157"},
        {"code": "600003", "board": 2, "seal_time": "10:01", "first_seal_time": "09:58",
         "industry": "881157"},
    ]


class CoverageTest(unittest.TestCase):
    def test_low_member_coverage_nulls_the_numbers_and_records_a_typed_gap(self):
        result = board_strength(
            "881157", pool=_pool(), industry_bars=INDUSTRY_BARS, flows=FLOWS,
            members=MEMBERS, coverage=0.5,
        )
        for name, entry in result["fields"].items():
            self.assertIsNone(entry["value"], f"{name} 覆盖率不足时不该给数")
        self.assertTrue(result["source_gaps"])
        gap = result["source_gaps"][0]
        self.assertEqual(gap["scope"], "advisory")
        self.assertNotIn(gap["scope"], ("side_hard", "candidate_hard"))
        self.assertIn("affected_review_cells", gap)

    def test_good_coverage_keeps_the_numbers(self):
        result = board_strength(
            "881157", pool=_pool(), industry_bars=INDUSTRY_BARS, flows=FLOWS,
            members=MEMBERS, coverage=0.9, midcap_pct=35.2941,
        )
        self.assertEqual(result["fields"]["limit_up_count"]["value"], 3)
        self.assertEqual(result["fields"]["limit_up_2plus_count"]["value"], 2)
        self.assertIsNotNone(result["fields"]["index_position_vs_ma5"]["value"])


class FieldShapeTest(unittest.TestCase):
    def result(self):
        return board_strength(
            "881157", pool=_pool(), industry_bars=INDUSTRY_BARS, flows=FLOWS,
            members=MEMBERS, coverage=0.9, midcap_pct=35.2941,
        )

    def test_every_field_carries_k5_attributes(self):
        for name, entry in self.result()["fields"].items():
            self.assertEqual(set(entry), {"value", "source", "data_as_of", "status"}, name)

    def test_sources_are_the_declared_producers(self):
        sources = {name: entry["source"] for name, entry in self.result()["fields"].items()}
        self.assertEqual(sources["limit_up_count"], "market_facts")
        self.assertEqual(sources["index_position_vs_ma5"], "sector_index")
        self.assertEqual(sources["net_inflow_3d"], "industry_flow")

    def test_limit_up_counts_come_from_the_pool(self):
        fields = self.result()["fields"]
        self.assertEqual(fields["limit_up_count"]["value"], 3)
        self.assertEqual(fields["limit_up_2plus_count"]["value"], 2)

    def test_seal_time_distribution_reports_both_ends(self):
        entry = self.result()["fields"]["seal_time_distribution"]
        self.assertEqual(entry["value"]["earliest"], "09:31")
        self.assertEqual(entry["value"]["latest"], "13:12")

    def test_index_position_vs_ma5_uses_the_last_close_over_5day_average(self):
        entry = self.result()["fields"]["index_position_vs_ma5"]
        expected = round((106.0 - (104.0 + 106.0 + 103.0 + 102.0 + 101.0) / 5)
                         / ((104.0 + 106.0 + 103.0 + 102.0 + 101.0) / 5) * 100, 4)
        self.assertAlmostEqual(entry["value"], expected, places=3)

    def test_net_inflow_3d_sums_three_days(self):
        self.assertEqual(self.result()["fields"]["net_inflow_3d"]["value"], 17.0)

    def test_midcap_reuses_the_n1_implementation(self):
        """中军比例只有一处实现（管道 indicators）；这里只透传并标来源。"""
        entry = self.result()["fields"]["midcap_above_ma20_pct"]
        self.assertEqual(entry["source"], "indicators.midcap_above_ma20_pct")
        self.assertEqual(entry["value"], 35.2941)

    def test_rank_change_comes_from_the_caller(self):
        result = board_strength(
            "881157", pool=_pool(), industry_bars=INDUSTRY_BARS, flows=FLOWS,
            members=MEMBERS, coverage=0.9, rank_change=3,
        )
        self.assertEqual(result["fields"]["rank_change"]["value"], 3)

    def test_missing_bars_leaves_the_field_null_not_zero(self):
        result = board_strength(
            "881157", pool=_pool(), industry_bars=INDUSTRY_BARS[:3], flows=FLOWS,
            members=MEMBERS, coverage=0.9,
        )
        self.assertIsNone(result["fields"]["index_position_vs_ma5"]["value"])
        self.assertEqual(result["fields"]["index_position_vs_ma5"]["status"], "optional_missing")


class DefinitionsTest(unittest.TestCase):
    def test_taxonomy_declares_industry_prefix_and_no_truncation(self):
        payload = load_board_definitions(_write({
            "taxonomy": {"industry": {"index_prefix": "881", "truncate": False}},
            "boards": [{"board_id": "881157", "type": "industry",
                        "member_source": {"intent": "ths_industry_members"}}],
        }))
        self.assertEqual(payload["taxonomy"]["industry"]["index_prefix"], "881")
        self.assertFalse(payload["taxonomy"]["industry"]["truncate"])

    def test_concepts_are_a_separate_type_and_not_merged_into_industry(self):
        payload = load_board_definitions(_write({
            "taxonomy": {"industry": {"index_prefix": "881", "truncate": False}},
            "boards": [
                {"board_id": "881157", "type": "industry",
                 "member_source": {"intent": "ths_industry_members"}},
                {"board_id": "885571", "name": "半导体", "type": "concept",
                 "member_source": {"intent": "concept_index"}},
            ],
        }))
        industries = {b["board_id"] for b in payload["boards"]
                      if b.get("type") == "industry"}
        concepts = {b["board_id"] for b in payload["boards"]
                    if b.get("type") == "concept"}
        self.assertEqual(concepts, {"885571"})
        self.assertFalse(industries & concepts, "概念不能并进行业")

    def test_definition_needs_a_member_source(self):
        with self.assertRaises(ValueError):
            load_board_definitions(_write({
                "boards": [{"board_id": "881157", "type": "industry"}],
            }))


def _write(payload: dict) -> str:
    import json
    import tempfile
    from pathlib import Path

    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(payload, handle, ensure_ascii=False)
    handle.close()
    return handle.name


if __name__ == "__main__":
    unittest.main()
