"""test_board_strength.py — W4 S1：逐板块强度字段（K5 形态）

字段按开工单 W4 第二节第 2 点：limit_up_count / limit_up_2plus_count /
index_position_vs_ma5 / net_inflow_3d / midcap_above_ma20_pct / rank_change，
每个都是 {value, source, data_as_of, status}。（seal_time_distribution 已按
审计回复 11 二.3 删除：封存表没有这一列，没有生产者。）

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
# W4 返工：行必须带板块身份（ts_code/industry）才会被认领——按板块分别取资金流，
# 不再“同一份 flows 传给每个板块取尾巴三行”（审计阻断 1）。
FLOWS = [{"trade_date": "2026-09-28", "net_inflow_yi": 12.0, "ts_code": "881157.TI"},
         {"trade_date": "2026-09-29", "net_inflow_yi": -3.0, "ts_code": "881157.TI"},
         {"trade_date": "2026-09-30", "net_inflow_yi": 8.0, "ts_code": "881157.TI"}]
MEMBERS = [f"600{index:03d}" for index in range(10)]


def _pool():
    return [
        {"code": "600001", "board": 3, "industry": "881157"},
        {"code": "600002", "board": 1, "industry": "881157"},
        {"code": "600003", "board": 2, "industry": "881157"},
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


class PerBoardFlowTests(unittest.TestCase):
    """审计阻断 1：net_inflow_3d 必须按板块分别取，不许全市场榜单尾巴冒充。

    旧行为：provider 把同一份全市场 flows 传给每个板块，内核取 flows[-3:]
    ——103 个板块拿到同一个数（线上实测全是 −212.32，status 还是 ok）。
    """

    FLOWS = [
        {"trade_date": "20260928", "industry": "银行", "ts_code": "881155.TI",
         "net_amount": 10.0, "pct_change": 1.0},
        {"trade_date": "20260929", "industry": "银行", "ts_code": "881155.TI",
         "net_amount": 20.0, "pct_change": 0.5},
        {"trade_date": "20260930", "industry": "银行", "ts_code": "881155.TI",
         "net_amount": 30.0, "pct_change": 2.0},
        {"trade_date": "20260928", "industry": "半导体", "ts_code": "881121.TI",
         "net_amount": -1.0, "pct_change": -0.5},
        {"trade_date": "20260929", "industry": "半导体", "ts_code": "881121.TI",
         "net_amount": -2.0, "pct_change": -1.0},
        {"trade_date": "20260930", "industry": "半导体", "ts_code": "881121.TI",
         "net_amount": -3.0, "pct_change": -2.0},
        # 全市场榜单里排在尾巴的无关行业——旧 bug 把它当成了每个板块的"三日合计"
        {"trade_date": "20260930", "industry": "美容护理", "ts_code": "881182.TI",
         "net_amount": -212.32, "pct_change": 0.1},
    ]

    def test_each_board_sums_its_own_three_days(self):
        from ym_stock_data.board_strength import board_strength

        bank = board_strength("881155", board_name="银行", flows=self.FLOWS,
                              coverage=1.0)
        semi = board_strength("881121", board_name="半导体", flows=self.FLOWS,
                              coverage=1.0)
        self.assertEqual(60.0, bank["fields"]["net_inflow_3d"]["value"])
        self.assertEqual("ok", bank["fields"]["net_inflow_3d"]["status"])
        self.assertEqual(-6.0, semi["fields"]["net_inflow_3d"]["value"])
        self.assertNotEqual(
            bank["fields"]["net_inflow_3d"]["value"],
            semi["fields"]["net_inflow_3d"]["value"],
            "不同板块拿到了同一个资金数——全市场榜单尾巴又混进来了",
        )

    def test_board_without_flow_rows_gets_null_and_typed_gap_not_ok(self):
        """概念板块不在行业资金表里：置 null + 记 typed gap，status 不许 ok。"""
        from ym_stock_data.board_strength import board_strength

        concept = board_strength("885957", board_name="东数西算(算力)",
                                 flows=self.FLOWS, coverage=1.0)
        field = concept["fields"]["net_inflow_3d"]
        self.assertIsNone(field["value"])
        self.assertNotEqual("ok", field["status"])
        codes = [gap["gap_code"] for gap in concept["source_gaps"]]
        self.assertTrue(
            any(code.startswith("board_field_unavailable:net_inflow_3d")
                for code in codes),
            f"取不到资金流必须记 typed gap，实际 gaps={codes}",
        )

    def test_matches_by_ts_code_when_name_differs(self):
        from ym_stock_data.board_strength import board_strength

        flows = [{"trade_date": "20260930", "industry": "某个别名",
                  "ts_code": "881155.TI", "net_amount": 7.0}]
        out = board_strength("881155", board_name="银行", flows=flows,
                             coverage=1.0)
        self.assertEqual(7.0, out["fields"]["net_inflow_3d"]["value"])

    def test_flow_rows_are_ordered_by_date_before_window_sum(self):
        from ym_stock_data.board_strength import board_strength

        shuffled = list(reversed(self.FLOWS))
        out = board_strength("881155", board_name="银行", flows=shuffled,
                             coverage=1.0)
        self.assertEqual(60.0, out["fields"]["net_inflow_3d"]["value"])


class FieldGapTests(unittest.TestCase):
    """审计阻断 4：覆盖足够但字段取不到值时，置 null 且记 typed gap。"""

    def test_missing_field_records_typed_gap(self):
        from ym_stock_data.board_strength import board_strength

        # 只给 limit 池：其余字段无输入 → null + gap（一个都不许静默 ok）
        out = board_strength("881155", board_name="银行",
                             pool=[{"code": "600000", "industry": "银行",
                                    "board": 1}],
                             coverage=1.0)
        gaps = {gap["gap_code"].split(":")[1]: gap
                for gap in out["source_gaps"]
                if gap["gap_code"].startswith("board_field_unavailable:")}
        for field in ("net_inflow_3d", "index_position_vs_ma5",
                      "midcap_above_ma20_pct", "rank_change"):
            self.assertIn(field, gaps, f"{field} 取不到值但没记 typed gap")
            self.assertEqual("advisory", gaps[field]["scope"])
            self.assertIn("affected_review_cells", gaps[field])
